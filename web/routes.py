from fastapi import APIRouter, Depends, HTTPException, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update, func
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import asyncio
import hashlib
import json
import logging
import hmac
from urllib.parse import unquote

from aiogram import Bot

from database.db import get_db
from database.models import (
    RoutingRule,
    ChatCache,
    UserCredits,
    CreditLedger,
    TranslationLog,
    SystemSettings,
    SourceChannel,
    SourceExcludedTerms,
)
from bot.services import ChatService
from shared.config import (
    BOT_TOKEN,
    WEB_APP_URL,
    STRIPE_SECRET_KEY,
    STRIPE_WEBHOOK_SECRET,
    stripe_checkout_price_map,
)
from shared.settings_service import get_system_settings
from shared.credit_service import ensure_user as credit_ensure_user, get_balance
from shared.billing_ledger import apply_ledger_entry, apply_refund_deduction

import stripe

logger = logging.getLogger(__name__)
from shared.utils import normalize_excluded_terms

router = APIRouter(prefix="/api")

_bot_username_cache: Optional[str] = None


async def get_cached_bot_username() -> Optional[str]:
    """Resolve @username for deep links (e.g. t.me/bot?start=pickdest). Cached after first success."""
    global _bot_username_cache
    if _bot_username_cache:
        return _bot_username_cache
    bot = Bot(token=BOT_TOKEN)
    try:
        me = await bot.get_me()
        if me.username:
            _bot_username_cache = me.username
        return _bot_username_cache
    except Exception:
        logger.exception("get_me failed while resolving bot username")
        return None
    finally:
        await bot.session.close()

# Pydantic Models
class RoutingRuleCreate(BaseModel):
    source_id: int
    destination_group_id: int
    destination_language: str
    translate_images: bool = False
    translate_poll: bool = False
    owner_telegram_id: Optional[int] = None
    enabled: bool = True


class RoutingRuleResponse(RoutingRuleCreate):
    id: int
    class Config:
        from_attributes = True

class ChatResponse(BaseModel):
    id: int
    title: Optional[str]
    type: str
    class Config:
        from_attributes = True


class ChatWithAccessResponse(BaseModel):
    id: int
    title: Optional[str]
    type: str
    access: bool
    class Config:
        from_attributes = True

class InitData(BaseModel):
    initData: str


class SourceCreate(BaseModel):
    source_id: int
    title: Optional[str] = None


class SourceResponse(BaseModel):
    source_id: int
    title: Optional[str] = None
    owner_telegram_id: Optional[int] = None
    created_at: Optional[str] = None
    class Config:
        from_attributes = True


class SourceUpdate(BaseModel):
    source_id: int  # new channel id after re-verify
    title: Optional[str] = None


class ExcludedTermsPut(BaseModel):
    terms: List[str]


class ExcludedTermsBatchResponse(BaseModel):
    terms_by_source: Dict[str, List[str]]


# Auth Validation
def validate_telegram_data(init_data: str) -> bool:
    """Validate Telegram WebApp initData hash. Returns True if signature is valid (any Telegram user)."""
    try:
        parsed_data = dict(item.split("=", 1) for item in unquote(init_data).split("&"))
        hash_val = parsed_data.pop("hash", None)
        if not hash_val:
            return False
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest() != hash_val:
            return False
        return True
    except Exception:
        return False


def parse_telegram_user(init_data: str) -> Optional[dict]:
    """Parse initData and return { telegram_id, username } if valid."""
    try:
        parsed = dict(item.split("=", 1) for item in unquote(init_data).split("&"))
        hash_val = parsed.pop("hash", None)
        if not hash_val:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest() != hash_val:
            return None
        user = json.loads(parsed.get("user", "{}"))
        return {"telegram_id": user.get("id"), "username": user.get("username")}
    except Exception:
        return None


async def require_user(
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
):
    """Dependency: require valid Telegram init data and return user dict."""
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="Missing X-Telegram-Init-Data header")
    user = parse_telegram_user(x_telegram_init_data)
    if not user or user.get("telegram_id") is None:
        raise HTTPException(status_code=403, detail="Invalid init data")
    return user


async def user_can_access_source(
    session: AsyncSession,
    uid: int,
    source_id: int,
    x_telegram_init_data: Optional[str],
) -> bool:
    """True if user owns a routing rule or SourceChannel for this source_id."""
    r = await session.execute(
        select(RoutingRule.id)
        .where(RoutingRule.source_id == source_id, RoutingRule.owner_telegram_id == uid)
        .limit(1)
    )
    if r.scalar_one_or_none() is not None:
        return True
    r = await session.execute(
        select(SourceChannel.source_id)
        .where(SourceChannel.source_id == source_id, SourceChannel.owner_telegram_id == uid)
        .limit(1)
    )
    return r.scalar_one_or_none() is not None


# Routes
@router.post("/validate-init-data")
async def validate_init_data(data: InitData):
    if validate_telegram_data(data.initData):
        return {"status": "ok"}
    raise HTTPException(status_code=403, detail="Unauthorized")

@router.get("/rules", response_model=List[RoutingRuleResponse])
async def get_rules(
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_db),
):
    uid = user["telegram_id"]
    result = await session.execute(
        select(RoutingRule).where(RoutingRule.owner_telegram_id == uid)
    )
    return result.scalars().all()

@router.post("/rules", response_model=RoutingRuleResponse)
async def create_rule(
    rule: RoutingRuleCreate,
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_db),
):
    settings = await get_system_settings(session)
    uid = user["telegram_id"]
    total = await session.execute(select(func.count()).select_from(RoutingRule).where(RoutingRule.owner_telegram_id == uid))
    if total.scalar() >= settings.max_pairs:
        raise HTTPException(status_code=400, detail=f"Maximum number of pairs ({settings.max_pairs}) reached.")
    per_source = await session.execute(
        select(func.count()).select_from(RoutingRule).where(RoutingRule.source_id == rule.source_id, RoutingRule.owner_telegram_id == uid)
    )
    if per_source.scalar() >= settings.max_destinations_per_source:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum destinations per source ({settings.max_destinations_per_source}) reached for this source.",
        )
    data = rule.dict()
    if data.get("owner_telegram_id") is None:
        data["owner_telegram_id"] = uid
    db_rule = RoutingRule(**data)
    session.add(db_rule)
    # Remove standalone source row when first rule is created for this source_id
    await session.execute(delete(SourceChannel).where(SourceChannel.source_id == rule.source_id))
    await session.commit()
    await session.refresh(db_rule)
    return db_rule

class RuleEnabledUpdate(BaseModel):
    enabled: bool


@router.patch("/rules/{rule_id}", response_model=RoutingRuleResponse)
async def patch_rule_enabled(
    rule_id: int,
    body: RuleEnabledUpdate,
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_db),
):
    db_rule = await session.get(RoutingRule, rule_id)
    if not db_rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    if db_rule.owner_telegram_id != user["telegram_id"]:
        raise HTTPException(status_code=403, detail="Forbidden")
    db_rule.enabled = body.enabled
    await session.commit()
    await session.refresh(db_rule)
    return db_rule


@router.post("/sources/{source_id}/pause")
async def pause_source(
    source_id: int,
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_db),
):
    uid = user["telegram_id"]
    check = await session.execute(select(RoutingRule).where(RoutingRule.source_id == source_id, RoutingRule.owner_telegram_id == uid).limit(1))
    if check.scalar_one_or_none() is None:
        raise HTTPException(status_code=403, detail="Forbidden")
    await session.execute(update(RoutingRule).where(RoutingRule.source_id == source_id).values(enabled=False))
    await session.commit()
    return {"status": "ok", "message": "Source paused"}


@router.post("/sources/{source_id}/unpause")
async def unpause_source(
    source_id: int,
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_db),
):
    uid = user["telegram_id"]
    check = await session.execute(select(RoutingRule).where(RoutingRule.source_id == source_id, RoutingRule.owner_telegram_id == uid).limit(1))
    if check.scalar_one_or_none() is None:
        raise HTTPException(status_code=403, detail="Forbidden")
    await session.execute(update(RoutingRule).where(RoutingRule.source_id == source_id).values(enabled=True))
    await session.commit()
    return {"status": "ok", "message": "Source unpaused"}


@router.delete("/rules/{rule_id}")
async def delete_rule(
    rule_id: int,
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_db),
):
    db_rule = await session.get(RoutingRule, rule_id)
    if not db_rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    if db_rule.owner_telegram_id != user["telegram_id"]:
        raise HTTPException(status_code=403, detail="Forbidden")
    await session.execute(delete(RoutingRule).where(RoutingRule.id == rule_id))
    await session.commit()
    return {"status": "deleted"}


@router.get("/sources", response_model=List[SourceResponse])
async def get_sources(
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_db),
):
    uid = user["telegram_id"]
    result = await session.execute(select(SourceChannel).where(SourceChannel.owner_telegram_id == uid))
    rows = result.scalars().all()
    return [
        SourceResponse(
            source_id=r.source_id,
            title=r.title,
            owner_telegram_id=r.owner_telegram_id,
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in rows
    ]


@router.get("/excluded-terms", response_model=ExcludedTermsBatchResponse)
async def get_excluded_terms_batch(
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_db),
):
    uid = user["telegram_id"]
    rule_ids = await session.execute(
        select(RoutingRule.source_id).where(RoutingRule.owner_telegram_id == uid).distinct()
    )
    sc_ids = await session.execute(
        select(SourceChannel.source_id).where(SourceChannel.owner_telegram_id == uid)
    )
    all_ids = {row[0] for row in rule_ids.all()} | {row[0] for row in sc_ids.all()}
    if not all_ids:
        return ExcludedTermsBatchResponse(terms_by_source={})
    rows = await session.execute(
        select(SourceExcludedTerms).where(SourceExcludedTerms.source_id.in_(all_ids))
    )
    terms_by_source: Dict[str, List[str]] = {str(sid): [] for sid in all_ids}
    for ex in rows.scalars().all():
        try:
            arr = json.loads(ex.terms_json or "[]")
        except Exception:
            arr = []
        if not isinstance(arr, list):
            arr = []
        terms_by_source[str(ex.source_id)] = [x for x in arr if isinstance(x, str)]
    return ExcludedTermsBatchResponse(terms_by_source=terms_by_source)


@router.put("/sources/{source_id}/excluded-terms")
async def put_excluded_terms(
    source_id: int,
    body: ExcludedTermsPut,
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_db),
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
):
    uid = user["telegram_id"]
    if not await user_can_access_source(session, uid, source_id, x_telegram_init_data):
        raise HTTPException(status_code=403, detail="Forbidden")
    norm = normalize_excluded_terms(body.terms)
    row = await session.get(SourceExcludedTerms, source_id)
    if not norm:
        if row:
            await session.delete(row)
    else:
        payload = json.dumps(norm)
        if row:
            row.terms_json = payload
        else:
            session.add(SourceExcludedTerms(source_id=source_id, terms_json=payload))
    await session.commit()
    return {"terms": norm}


@router.post("/sources", response_model=SourceResponse)
async def create_source(
    body: SourceCreate,
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_db),
):
    settings = await get_system_settings(session)
    uid = user["telegram_id"]
    rule_ids = await session.execute(select(RoutingRule.source_id).where(RoutingRule.owner_telegram_id == uid).distinct())
    rule_id_set = set(row[0] for row in rule_ids.all())
    source_rows = await session.execute(select(SourceChannel.source_id).where(SourceChannel.owner_telegram_id == uid))
    source_id_set = set(row[0] for row in source_rows.all())
    distinct_count = len(rule_id_set | source_id_set)
    if body.source_id in (rule_id_set | source_id_set):
        raise HTTPException(status_code=400, detail="This source already exists.")
    if distinct_count >= settings.max_pairs:
        raise HTTPException(status_code=400, detail=f"Maximum number of sources ({settings.max_pairs}) reached.")
    sc = SourceChannel(source_id=body.source_id, title=body.title or None, owner_telegram_id=uid)
    session.add(sc)
    await session.commit()
    await session.refresh(sc)
    return SourceResponse(
        source_id=sc.source_id,
        title=sc.title,
        owner_telegram_id=sc.owner_telegram_id,
        created_at=sc.created_at.isoformat() if sc.created_at else None,
    )


@router.patch("/sources/{source_id}", response_model=SourceResponse)
async def update_source(
    source_id: int,
    body: SourceUpdate,
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_db),
):
    old_id = source_id
    new_id = body.source_id
    uid = user["telegram_id"]
    ex_old = await session.execute(select(SourceChannel).where(SourceChannel.source_id == old_id))
    row = ex_old.scalar_one_or_none()
    if not row or row.owner_telegram_id != uid:
        raise HTTPException(status_code=403, detail="Forbidden")
    # Update all rules that point to old_id to new_id
    await session.execute(update(RoutingRule).where(RoutingRule.source_id == old_id).values(source_id=new_id))
    # Migrate excluded terms row when source id changes
    if old_id != new_id:
        old_ex = await session.get(SourceExcludedTerms, old_id)
        new_ex = await session.get(SourceExcludedTerms, new_id)
        if old_ex and new_ex:
            ta = json.loads(old_ex.terms_json or "[]")
            tb = json.loads(new_ex.terms_json or "[]")
            if not isinstance(ta, list):
                ta = []
            if not isinstance(tb, list):
                tb = []
            merged = normalize_excluded_terms(ta + tb)
            new_ex.terms_json = json.dumps(merged)
            await session.delete(old_ex)
        elif old_ex:
            await session.delete(old_ex)
            session.add(SourceExcludedTerms(source_id=new_id, terms_json=old_ex.terms_json))
    # Replace source_channels: remove old row; upsert new_id row
    await session.execute(delete(SourceChannel).where(SourceChannel.source_id == old_id))
    existing = await session.execute(select(SourceChannel).where(SourceChannel.source_id == new_id))
    ex = existing.scalar_one_or_none()
    if ex:
        ex.title = body.title if body.title is not None else ex.title
        ex.owner_telegram_id = uid
    else:
        session.add(SourceChannel(source_id=new_id, title=body.title or None, owner_telegram_id=uid))
    await session.commit()
    r = await session.execute(select(SourceChannel).where(SourceChannel.source_id == new_id))
    row = r.scalar_one_or_none()
    return SourceResponse(
        source_id=new_id,
        title=row.title if row else body.title,
        owner_telegram_id=row.owner_telegram_id if row else uid,
        created_at=row.created_at.isoformat() if row and row.created_at else None,
    )


@router.delete("/sources/{source_id}")
async def delete_source(
    source_id: int,
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_db),
):
    ex = await session.execute(select(SourceChannel).where(SourceChannel.source_id == source_id))
    row = ex.scalar_one_or_none()
    if not row or row.owner_telegram_id != user["telegram_id"]:
        raise HTTPException(status_code=403, detail="Forbidden")
    await session.execute(delete(SourceChannel).where(SourceChannel.source_id == source_id))
    await session.commit()
    return {"status": "deleted"}


@router.put("/rules/{rule_id}", response_model=RoutingRuleResponse)
async def update_rule(
    rule_id: int,
    rule: RoutingRuleCreate,
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_db),
):
    db_rule = await session.get(RoutingRule, rule_id)
    if not db_rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    if db_rule.owner_telegram_id != user["telegram_id"]:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = rule.dict()
    db_rule.source_id = data["source_id"]
    db_rule.destination_group_id = data["destination_group_id"]
    db_rule.destination_language = data["destination_language"]
    db_rule.translate_images = data.get("translate_images", False)
    db_rule.translate_poll = data.get("translate_poll", False)
    if "enabled" in data:
        db_rule.enabled = data["enabled"]
    if data.get("owner_telegram_id") is not None:
        db_rule.owner_telegram_id = data["owner_telegram_id"]
    else:
        db_rule.owner_telegram_id = user["telegram_id"]
    await session.commit()
    await session.refresh(db_rule)
    return db_rule

@router.get("/chats", response_model=List[ChatResponse])
async def get_chats(
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_db),
):
    result = await session.execute(select(ChatCache).order_by(ChatCache.title))
    return result.scalars().all()


class ResolveChatBody(BaseModel):
    username: str


@router.post("/chats/resolve", response_model=ChatWithAccessResponse)
async def resolve_chat_by_username(body: ResolveChatBody, session: AsyncSession = Depends(get_db)):
    """Resolve a channel/group by @username and add to cache. Fails if bot has no access."""
    username = (body.username or "").strip().lstrip("@")
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    bot = Bot(token=BOT_TOKEN)
    try:
        chat = await bot.get_chat(f"@{username}")
    except Exception:
        raise HTTPException(status_code=404, detail="Chat not found or bot has no access")
    finally:
        await bot.session.close()
    title = chat.title or getattr(chat, "username", None) or username
    chat_type = getattr(chat.type, "value", None) or str(chat.type)
    await ChatService.update_chat_cache(session, chat.id, title, chat_type)
    return ChatWithAccessResponse(id=chat.id, title=title, type=chat_type, access=True)


@router.get("/chats/with-access", response_model=List[ChatWithAccessResponse])
async def get_chats_with_access(
    session: AsyncSession = Depends(get_db),
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
):
    """Return only chats where BOTH the bot and the current user have access (per-user contained)."""
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="Missing X-Telegram-Init-Data header")
    user = parse_telegram_user(x_telegram_init_data)
    if not user or user.get("telegram_id") is None:
        raise HTTPException(status_code=403, detail="Invalid init data")
    user_id = user["telegram_id"]
    result = await session.execute(select(ChatCache).order_by(ChatCache.title))
    chats = result.scalars().all()
    bot = Bot(token=BOT_TOKEN)

    async def check_one(chat):
        bot_ok, user_ok = False, False
        try:
            await bot.get_chat(chat.id)
            bot_ok = True
        except Exception:
            pass
        if bot_ok:
            try:
                await bot.get_chat_member(chat_id=chat.id, user_id=user_id)
                user_ok = True
            except Exception:
                pass
        return (chat, bot_ok and user_ok)

    try:
        batch_size = 10
        out = []
        for i in range(0, len(chats), batch_size):
            batch = chats[i : i + batch_size]
            results = await asyncio.gather(*[check_one(c) for c in batch], return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    continue
                chat, ok = r
                if ok:
                    out.append(ChatWithAccessResponse(id=chat.id, title=chat.title, type=chat.type, access=True))
        return out
    finally:
        await bot.session.close()


@router.get("/credits")
async def get_credits(
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
    session: AsyncSession = Depends(get_db),
):
    """Return balance for the current Mini App user. Requires valid initData."""
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="Missing X-Telegram-Init-Data header")
    user = parse_telegram_user(x_telegram_init_data)
    if not user or user.get("telegram_id") is None:
        raise HTTPException(status_code=403, detail="Invalid init data")
    await credit_ensure_user(session, user["telegram_id"], user.get("username"))
    stmt = select(UserCredits).where(UserCredits.telegram_id == user["telegram_id"])
    r = await session.execute(stmt)
    uc = r.scalar_one_or_none()
    balance = await get_balance(session, user["telegram_id"])
    free_c = int(getattr(uc, "free_balance_cents", 0) or 0) if uc else 0
    paid_c = int(getattr(uc, "paid_balance_cents", 0) or 0) if uc else 0
    return {
        "balance": balance,
        "free_balance_cents": free_c,
        "paid_balance_cents": paid_c,
        "currency": "USD",
        "balance_usd": round(balance / 100, 2),
        "free_balance_usd": round(free_c / 100, 2),
        "paid_balance_usd": round(paid_c / 100, 2),
    }


@router.get("/logs")
async def get_logs(
    limit: int = 50,
    offset: int = 0,
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
    session: AsyncSession = Depends(get_db),
):
    """Return translation logs for the current user (owner_telegram_id). Requires valid initData."""
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="Missing X-Telegram-Init-Data header")
    user = parse_telegram_user(x_telegram_init_data)
    if not user or user.get("telegram_id") is None:
        raise HTTPException(status_code=403, detail="Invalid init data")
    uid = user["telegram_id"]
    limit = min(max(1, limit), 200)
    offset = max(0, offset)
    stmt = (
        select(TranslationLog)
        .where(TranslationLog.owner_telegram_id == uid)
        .order_by(TranslationLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    logs = result.scalars().all()
    # Resolve source/dest names from ChatCache
    chat_ids = set()
    for log in logs:
        chat_ids.add(log.source_id)
        chat_ids.add(log.destination_group_id)
    chat_map = {}
    if chat_ids:
        chat_result = await session.execute(select(ChatCache).where(ChatCache.id.in_(chat_ids)))
        for c in chat_result.scalars().all():
            chat_map[c.id] = c.title or str(c.id)
    out = []
    for log in logs:
        cost_cents = getattr(log, "cost_usd_cents", None)
        out.append({
            "id": log.id,
            "source_id": log.source_id,
            "destination_group_id": log.destination_group_id,
            "source_name": chat_map.get(log.source_id, str(log.source_id)),
            "dest_name": chat_map.get(log.destination_group_id, str(log.destination_group_id)),
            "status": log.status,
            "source_link": log.source_link,
            "destination_link": log.destination_link,
            "error_message": log.error_message,
            "cost_usd_cents": cost_cents,
            "cost_usd": round(cost_cents / 100, 2) if cost_cents is not None else None,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        })
    return out


@router.get("/settings/limits")
async def get_settings_limits(
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_db),
):
    """Return limits for UI (max pairs, max destinations per source, max message length). Requires auth."""
    settings = await get_system_settings(session)
    bot_username = await get_cached_bot_username()
    return {
        "max_pairs": settings.max_pairs,
        "max_destinations_per_source": settings.max_destinations_per_source,
        "max_message_length": settings.max_message_length,
        "bot_username": bot_username or "",
    }


class MeSettingsResponse(BaseModel):
    receive_reports_telegram: bool
    spam_protection_enabled: bool = False
    spam_max_messages: int = 50
    spam_window_minutes: int = 5


class MeSettingsUpdate(BaseModel):
    receive_reports_telegram: Optional[bool] = None
    spam_protection_enabled: Optional[bool] = None
    spam_max_messages: Optional[int] = None
    spam_window_minutes: Optional[int] = None


@router.get("/me/settings", response_model=MeSettingsResponse)
async def get_me_settings(
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
    session: AsyncSession = Depends(get_db),
):
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="Missing X-Telegram-Init-Data header")
    user = parse_telegram_user(x_telegram_init_data)
    if not user or user.get("telegram_id") is None:
        raise HTTPException(status_code=403, detail="Invalid init data")
    await credit_ensure_user(session, user["telegram_id"], user.get("username"))
    stmt = select(UserCredits).where(UserCredits.telegram_id == user["telegram_id"])
    r = await session.execute(stmt)
    uc = r.scalar_one_or_none()
    if not uc:
        return MeSettingsResponse(receive_reports_telegram=True)
    return MeSettingsResponse(
        receive_reports_telegram=getattr(uc, "receive_reports_telegram", True),
        spam_protection_enabled=getattr(uc, "spam_protection_enabled", False),
        spam_max_messages=getattr(uc, "spam_max_messages", 50),
        spam_window_minutes=getattr(uc, "spam_window_minutes", 5),
    )


@router.patch("/me/settings", response_model=MeSettingsResponse)
async def patch_me_settings(
    body: MeSettingsUpdate,
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
    session: AsyncSession = Depends(get_db),
):
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="Missing X-Telegram-Init-Data header")
    user = parse_telegram_user(x_telegram_init_data)
    if not user or user.get("telegram_id") is None:
        raise HTTPException(status_code=403, detail="Invalid init data")
    uid = user["telegram_id"]
    await credit_ensure_user(session, uid, user.get("username"))
    stmt = select(UserCredits).where(UserCredits.telegram_id == uid)
    result = await session.execute(stmt)
    uc = result.scalar_one_or_none()
    if not uc:
        raise HTTPException(status_code=404, detail="User not found")
    if body.receive_reports_telegram is not None:
        uc.receive_reports_telegram = body.receive_reports_telegram
    if body.spam_protection_enabled is not None:
        uc.spam_protection_enabled = body.spam_protection_enabled
    if body.spam_max_messages is not None:
        if not (1 <= body.spam_max_messages <= 1000):
            raise HTTPException(status_code=400, detail="spam_max_messages must be between 1 and 1000")
        uc.spam_max_messages = body.spam_max_messages
    if body.spam_window_minutes is not None:
        if not (1 <= body.spam_window_minutes <= 1440):
            raise HTTPException(status_code=400, detail="spam_window_minutes must be between 1 and 1440")
        uc.spam_window_minutes = body.spam_window_minutes
    await session.commit()
    await session.refresh(uc)
    return MeSettingsResponse(
        receive_reports_telegram=uc.receive_reports_telegram,
        spam_protection_enabled=getattr(uc, "spam_protection_enabled", False),
        spam_max_messages=getattr(uc, "spam_max_messages", 50),
        spam_window_minutes=getattr(uc, "spam_window_minutes", 5),
    )


# --- Stripe billing (USD credits + ledger) ---


class BillingCheckoutBody(BaseModel):
    price_key: str


class BillingLedgerRow(BaseModel):
    delta_cents: int
    source: str
    external_ref: Optional[str] = None
    meta_json: Optional[str] = None
    created_at: Optional[str] = None

    class Config:
        from_attributes = True


async def _stripe_handle_checkout_completed(session: AsyncSession, event: Any) -> None:
    obj = event.data.object
    if getattr(obj, "payment_status", None) != "paid":
        return
    if getattr(obj, "mode", None) != "payment":
        return
    md = getattr(obj, "metadata", None) or {}
    tid_raw = md.get("telegram_id") if hasattr(md, "get") else None
    if not tid_raw:
        logger.warning("checkout.session.completed: missing telegram_id in metadata")
        return
    try:
        telegram_id = int(tid_raw)
    except (TypeError, ValueError):
        logger.warning("checkout.session.completed: invalid telegram_id %s", tid_raw)
        return
    amount_total = int(getattr(obj, "amount_total", None) or 0)
    if amount_total <= 0:
        return
    session_id = getattr(obj, "id", None)
    applied, _ = await apply_ledger_entry(
        session,
        telegram_id,
        amount_total,
        event.id,
        "stripe",
        external_ref=str(session_id) if session_id else None,
        meta={
            "stripe_event_type": event.type,
            "checkout_session_id": session_id,
        },
    )
    if not applied:
        logger.debug("checkout.session.completed duplicate event %s", event.id)


async def _stripe_handle_refund_created(session: AsyncSession, event: Any) -> None:
    if not STRIPE_SECRET_KEY:
        logger.error("refund.created: STRIPE_SECRET_KEY not set; cannot load charge")
        return
    refund = event.data.object
    charge_id = getattr(refund, "charge", None)
    amount = int(getattr(refund, "amount", None) or 0)
    refund_id = getattr(refund, "id", None)
    if not charge_id or amount <= 0:
        return
    stripe.api_key = STRIPE_SECRET_KEY

    def _retrieve_charge():
        return stripe.Charge.retrieve(str(charge_id))

    charge = await asyncio.to_thread(_retrieve_charge)
    md = getattr(charge, "metadata", None) or {}
    tid_raw = md.get("telegram_id") if hasattr(md, "get") else None
    if not tid_raw:
        logger.warning("refund.created: charge %s missing telegram_id metadata", charge_id)
        return
    try:
        telegram_id = int(tid_raw)
    except (TypeError, ValueError):
        return
    applied, _ = await apply_refund_deduction(
        session,
        telegram_id,
        amount,
        event.id,
        external_ref=str(refund_id) if refund_id else str(charge_id),
    )
    if not applied:
        logger.debug("refund.created duplicate event %s", event.id)


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, session: AsyncSession = Depends(get_db)):
    """Stripe webhook: verify signature; credit on checkout; debit on refund. Idempotent by event id."""
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Billing not configured")
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing stripe-signature header")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid payload") from e
    except stripe.error.SignatureVerificationError as e:
        raise HTTPException(status_code=400, detail="Invalid signature") from e

    if event.type == "checkout.session.completed":
        await _stripe_handle_checkout_completed(session, event)
    elif event.type == "refund.created":
        await _stripe_handle_refund_created(session, event)

    return {"received": True}


@router.post("/billing/checkout-session")
async def create_billing_checkout_session(
    body: BillingCheckoutBody,
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
):
    """Start Stripe Checkout for a fixed price pack. telegram_id is set only server-side from initData."""
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Billing not configured")
    if not WEB_APP_URL:
        raise HTTPException(status_code=503, detail="WEB_APP_URL not configured")
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="Missing X-Telegram-Init-Data header")
    user = parse_telegram_user(x_telegram_init_data)
    if not user or user.get("telegram_id") is None:
        raise HTTPException(status_code=403, detail="Invalid init data")
    price_map = stripe_checkout_price_map()
    price_id = price_map.get(body.price_key)
    if not price_id:
        raise HTTPException(
            status_code=400,
            detail="Unknown price_key or price not configured on server",
        )
    base = WEB_APP_URL.rstrip("/")
    tid = user["telegram_id"]
    uname = user.get("username")
    stripe.api_key = STRIPE_SECRET_KEY

    def _create_session():
        meta = {"telegram_id": str(tid)}
        if uname:
            meta["telegram_username"] = str(uname)
        return stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{base}/?billing=success",
            cancel_url=f"{base}/?billing=cancel",
            client_reference_id=str(tid),
            metadata=meta,
            payment_intent_data={"metadata": {"telegram_id": str(tid)}},
        )

    checkout = await asyncio.to_thread(_create_session)
    if not checkout.url:
        raise HTTPException(status_code=502, detail="Stripe did not return a checkout URL")
    return {"url": checkout.url}


@router.get("/billing/ledger", response_model=List[BillingLedgerRow])
async def get_billing_ledger(
    limit: int = 20,
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
    session: AsyncSession = Depends(get_db),
):
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="Missing X-Telegram-Init-Data header")
    user = parse_telegram_user(x_telegram_init_data)
    if not user or user.get("telegram_id") is None:
        raise HTTPException(status_code=403, detail="Invalid init data")
    uid = user["telegram_id"]
    limit = min(max(1, limit), 100)
    stmt = (
        select(CreditLedger)
        .where(CreditLedger.telegram_id == uid)
        .order_by(CreditLedger.id.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    out: List[BillingLedgerRow] = []
    for row in rows:
        ca = getattr(row, "created_at", None)
        out.append(
            BillingLedgerRow(
                delta_cents=row.delta_cents,
                source=row.source,
                external_ref=row.external_ref,
                meta_json=row.meta_json,
                created_at=ca.isoformat() if ca else None,
            )
        )
    return out
