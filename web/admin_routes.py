"""
Operator console API: session auth only (no Telegram admin).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import defaultdict
from typing import Any, AsyncIterator, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.db import get_db
from database.models import (
    AuthorizedUser,
    ChatCache,
    RoutingRule,
    SystemSettings,
    TranslationLog,
    UserCredits,
)
from shared.billing_ledger import apply_admin_adjustment
from shared.config import ADMIN_PASSWORD, ADMIN_PASSWORD_HASH, CENTS_PER_IMAGE_DEFAULT, CENTS_PER_TEXT_DEFAULT
from shared.credit_service import ensure_user as credit_ensure_user
from shared.settings_service import get_system_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

_login_failures: dict[str, list[float]] = {}
_LOGIN_WINDOW_SEC = 900
_LOGIN_MAX_ATTEMPTS = 10


def _client_ip(request: Request) -> str:
    if request.client:
        return request.client.host
    return "unknown"


def _check_failed_login_rate(ip: str) -> None:
    now = time.time()
    window_start = now - _LOGIN_WINDOW_SEC
    fails = [t for t in _login_failures.get(ip, []) if t > window_start]
    if len(fails) >= _LOGIN_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many login attempts; try later")


def _record_login_failure(ip: str) -> None:
    now = time.time()
    _login_failures.setdefault(ip, []).append(now)


async def require_admin_session(request: Request) -> None:
    if not request.session.get("admin"):
        raise HTTPException(status_code=401, detail="Not authenticated")


class AdminLoginBody(BaseModel):
    password: str = ""


class AdminSettingsResponse(BaseModel):
    max_pairs: int
    max_destinations_per_source: int
    max_message_length: int
    cents_per_text_translation: int
    cents_per_image_translation: int
    manual_provider_balance_cents: Optional[int] = None
    manual_monthly_provider_cost_cents: Optional[int] = None


class AdminSettingsUpdate(BaseModel):
    max_pairs: Optional[int] = None
    max_destinations_per_source: Optional[int] = None
    max_message_length: Optional[int] = None
    cents_per_text_translation: Optional[int] = None
    cents_per_image_translation: Optional[int] = None
    manual_provider_balance_cents: Optional[int] = None
    manual_monthly_provider_cost_cents: Optional[int] = None


class AuthorizedUserCreate(BaseModel):
    telegram_id: int
    username: Optional[str] = None


class AuthorizedUserResponse(AuthorizedUserCreate):
    id: int

    class Config:
        from_attributes = True


class CreditAdjustBody(BaseModel):
    telegram_id: int
    delta_cents: int
    reason: str = Field(..., min_length=1, max_length=64)
    note: Optional[str] = Field(None, max_length=500)


class ClientPatchBody(BaseModel):
    blocked: Optional[bool] = None
    receive_reports_telegram: Optional[bool] = None
    spam_protection_enabled: Optional[bool] = None
    spam_max_messages: Optional[int] = None
    spam_window_minutes: Optional[int] = None


def _verify_admin_password(plain: str) -> bool:
    """Prefer bcrypt hash if set; otherwise plain ADMIN_PASSWORD (constant-time compare)."""
    if ADMIN_PASSWORD_HASH:
        try:
            from passlib.hash import bcrypt

            return bcrypt.verify(plain, ADMIN_PASSWORD_HASH)
        except Exception as e:
            logger.warning("Password verify error: %s", e)
            return False
    if ADMIN_PASSWORD:
        return hmac.compare_digest(
            hashlib.sha256(plain.encode("utf-8")).digest(),
            hashlib.sha256(ADMIN_PASSWORD.encode("utf-8")).digest(),
        )
    return False


@router.post("/login")
async def admin_login(request: Request, body: AdminLoginBody):
    if not ADMIN_PASSWORD_HASH and not ADMIN_PASSWORD:
        raise HTTPException(
            status_code=503,
            detail="Admin password not configured (set ADMIN_PASSWORD or ADMIN_PASSWORD_HASH in .env)",
        )
    ip = _client_ip(request)
    _check_failed_login_rate(ip)
    ok = _verify_admin_password(body.password)
    if not ok:
        _record_login_failure(ip)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    request.session["admin"] = True
    return {"ok": True}


@router.post("/logout")
async def admin_logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/me")
async def admin_me(request: Request, _: None = Depends(require_admin_session)):
    return {"ok": True, "authenticated": True}


@router.get("/settings", response_model=AdminSettingsResponse)
async def get_admin_settings(
    request: Request,
    _: None = Depends(require_admin_session),
    session: AsyncSession = Depends(get_db),
):
    s = await get_system_settings(session)
    _ct = getattr(s, "cents_per_text_translation", None)
    _ci = getattr(s, "cents_per_image_translation", None)
    return AdminSettingsResponse(
        max_pairs=s.max_pairs,
        max_destinations_per_source=s.max_destinations_per_source,
        max_message_length=s.max_message_length,
        cents_per_text_translation=CENTS_PER_TEXT_DEFAULT if _ct is None else int(_ct),
        cents_per_image_translation=CENTS_PER_IMAGE_DEFAULT if _ci is None else int(_ci),
        manual_provider_balance_cents=getattr(s, "manual_provider_balance_cents", None),
        manual_monthly_provider_cost_cents=getattr(s, "manual_monthly_provider_cost_cents", None),
    )


@router.patch("/settings", response_model=AdminSettingsResponse)
async def patch_admin_settings(
    request: Request,
    body: AdminSettingsUpdate,
    _: None = Depends(require_admin_session),
    session: AsyncSession = Depends(get_db),
):
    settings = await get_system_settings(session)
    if body.max_pairs is not None:
        if not (1 <= body.max_pairs <= 100):
            raise HTTPException(status_code=400, detail="max_pairs must be between 1 and 100")
        settings.max_pairs = body.max_pairs
    if body.max_destinations_per_source is not None:
        if not (1 <= body.max_destinations_per_source <= 100):
            raise HTTPException(status_code=400, detail="max_destinations_per_source must be between 1 and 100")
        settings.max_destinations_per_source = body.max_destinations_per_source
    if body.max_message_length is not None:
        if not (256 <= body.max_message_length <= 4096):
            raise HTTPException(status_code=400, detail="max_message_length must be between 256 and 4096")
        settings.max_message_length = body.max_message_length
    if body.cents_per_text_translation is not None:
        if not (0 <= body.cents_per_text_translation <= 10000):
            raise HTTPException(
                status_code=400,
                detail="cents_per_text_translation must be between 0 and 10000",
            )
        settings.cents_per_text_translation = body.cents_per_text_translation
    if body.cents_per_image_translation is not None:
        if not (0 <= body.cents_per_image_translation <= 10000):
            raise HTTPException(
                status_code=400,
                detail="cents_per_image_translation must be between 0 and 10000",
            )
        settings.cents_per_image_translation = body.cents_per_image_translation
    if body.manual_provider_balance_cents is not None:
        settings.manual_provider_balance_cents = body.manual_provider_balance_cents
    if body.manual_monthly_provider_cost_cents is not None:
        settings.manual_monthly_provider_cost_cents = body.manual_monthly_provider_cost_cents
    await session.commit()
    await session.refresh(settings)
    _ct = getattr(settings, "cents_per_text_translation", None)
    _ci = getattr(settings, "cents_per_image_translation", None)
    return AdminSettingsResponse(
        max_pairs=settings.max_pairs,
        max_destinations_per_source=settings.max_destinations_per_source,
        max_message_length=settings.max_message_length,
        cents_per_text_translation=CENTS_PER_TEXT_DEFAULT if _ct is None else int(_ct),
        cents_per_image_translation=CENTS_PER_IMAGE_DEFAULT if _ci is None else int(_ci),
        manual_provider_balance_cents=getattr(settings, "manual_provider_balance_cents", None),
        manual_monthly_provider_cost_cents=getattr(settings, "manual_monthly_provider_cost_cents", None),
    )


@router.get("/whitelist", response_model=List[AuthorizedUserResponse])
async def get_whitelist(
    _: None = Depends(require_admin_session),
    session: AsyncSession = Depends(get_db),
):
    result = await session.execute(select(AuthorizedUser))
    return result.scalars().all()


@router.post("/whitelist", response_model=AuthorizedUserResponse)
async def create_whitelist_entry(
    user: AuthorizedUserCreate,
    _: None = Depends(require_admin_session),
    session: AsyncSession = Depends(get_db),
):
    db_user = AuthorizedUser(**user.model_dump())
    session.add(db_user)
    await session.commit()
    await session.refresh(db_user)
    return db_user


@router.delete("/whitelist/{user_id}")
async def delete_whitelist_entry(
    user_id: int,
    _: None = Depends(require_admin_session),
    session: AsyncSession = Depends(get_db),
):
    from sqlalchemy import delete

    await session.execute(delete(AuthorizedUser).where(AuthorizedUser.id == user_id))
    await session.commit()
    return {"status": "deleted"}


@router.get("/clients")
async def list_clients(
    _: None = Depends(require_admin_session),
    session: AsyncSession = Depends(get_db),
    offset: int = 0,
    limit: int = 50,
    q: str = "",
):
    offset = max(0, offset)
    limit = min(max(1, limit), 200)
    base = select(UserCredits)
    if q.strip():
        term = f"%{q.strip()}%"
        try:
            tid = int(q.strip())
            base = base.where((UserCredits.telegram_id == tid) | (UserCredits.username.ilike(term)))
        except ValueError:
            base = base.where(UserCredits.username.ilike(term))
    count_stmt = select(func.count()).select_from(base.subquery())
    total = (await session.execute(count_stmt)).scalar_one()
    stmt = base.order_by(UserCredits.updated_at.desc()).offset(offset).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()

    owner_ids = [r.telegram_id for r in rows]
    rule_counts: dict[int, int] = defaultdict(int)
    last_activity: dict[int, Any] = {}
    if owner_ids:
        rc = await session.execute(
            select(RoutingRule.owner_telegram_id, func.count(RoutingRule.id))
            .where(RoutingRule.owner_telegram_id.in_(owner_ids))
            .group_by(RoutingRule.owner_telegram_id)
        )
        for oid, c in rc.all():
            if oid is not None:
                rule_counts[int(oid)] = int(c)
        la = await session.execute(
            select(TranslationLog.owner_telegram_id, func.max(TranslationLog.created_at))
            .where(TranslationLog.owner_telegram_id.in_(owner_ids))
            .group_by(TranslationLog.owner_telegram_id)
        )
        for oid, ts in la.all():
            if oid is not None:
                last_activity[int(oid)] = ts.isoformat() if ts else None

    out = []
    for uc in rows:
        tid = int(uc.telegram_id)
        fc = int(getattr(uc, "free_balance_cents", 0) or 0)
        pc = int(getattr(uc, "paid_balance_cents", 0) or 0)
        out.append(
            {
                "telegram_id": tid,
                "username": uc.username,
                "balance": uc.balance,
                "free_balance_cents": fc,
                "paid_balance_cents": pc,
                "reserved_balance": getattr(uc, "reserved_balance", 0) or 0,
                "balance_usd": round(uc.balance / 100, 2),
                "free_balance_usd": round(fc / 100, 2),
                "paid_balance_usd": round(pc / 100, 2),
                "blocked": bool(getattr(uc, "blocked", False)),
                "receive_reports_telegram": getattr(uc, "receive_reports_telegram", True),
                "spam_protection_enabled": getattr(uc, "spam_protection_enabled", False),
                "spam_max_messages": getattr(uc, "spam_max_messages", 50),
                "spam_window_minutes": getattr(uc, "spam_window_minutes", 5),
                "updated_at": uc.updated_at.isoformat() if uc.updated_at else None,
                "rule_count": rule_counts.get(tid, 0),
                "last_translation_at": last_activity.get(tid),
            }
        )
    return {"total": total, "offset": offset, "limit": limit, "clients": out}


@router.patch("/clients/{telegram_id}")
async def patch_client(
    telegram_id: int,
    body: ClientPatchBody,
    _: None = Depends(require_admin_session),
    session: AsyncSession = Depends(get_db),
):
    uc = await credit_ensure_user(session, telegram_id, None)
    if body.blocked is not None:
        uc.blocked = body.blocked
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
    return {"ok": True, "telegram_id": telegram_id, "blocked": bool(getattr(uc, "blocked", False))}


@router.post("/credits/adjust")
async def admin_credit_adjust(
    body: CreditAdjustBody,
    _: None = Depends(require_admin_session),
    session: AsyncSession = Depends(get_db),
):
    try:
        new_balance, ledger_id = await apply_admin_adjustment(
            session,
            body.telegram_id,
            body.delta_cents,
            reason=body.reason,
            note=body.note,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    stmt = select(UserCredits).where(UserCredits.telegram_id == body.telegram_id)
    ruc = await session.execute(stmt)
    urow = ruc.scalar_one_or_none()
    fc = int(getattr(urow, "free_balance_cents", 0) or 0) if urow else 0
    pc = int(getattr(urow, "paid_balance_cents", 0) or 0) if urow else 0
    return {
        "ok": True,
        "telegram_id": body.telegram_id,
        "balance_cents": new_balance,
        "balance_usd": round(new_balance / 100, 2),
        "free_balance_cents": fc,
        "paid_balance_cents": pc,
        "ledger_id": ledger_id,
        "note": "Admin adjustments apply to free credit only (positive adds free; negative takes free first, then paid).",
    }


async def _stats_payload(session: AsyncSession) -> dict[str, Any]:
    users_total = (await session.execute(select(func.count()).select_from(UserCredits))).scalar_one()
    blocked_n = (
        await session.execute(
            select(func.count()).select_from(UserCredits).where(UserCredits.blocked == True)  # noqa: E712
        )
    ).scalar_one()
    liability = (await session.execute(select(func.coalesce(func.sum(UserCredits.balance), 0)))).scalar_one()
    reserved = (await session.execute(select(func.coalesce(func.sum(UserCredits.reserved_balance), 0)))).scalar_one()
    revenue_row = await session.execute(
        select(func.coalesce(func.sum(TranslationLog.cost_usd_cents), 0)).where(TranslationLog.status == "success")
    )
    revenue_cents = revenue_row.scalar_one() or 0
    by_status = await session.execute(
        select(TranslationLog.status, func.count(TranslationLog.id)).group_by(TranslationLog.status)
    )
    status_counts = {row[0]: row[1] for row in by_status.all()}
    from datetime import datetime, timedelta, timezone

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    recent24 = (
        await session.execute(
            select(func.count()).select_from(TranslationLog).where(TranslationLog.created_at >= since)
        )
    ).scalar_one()
    return {
        "users_total": int(users_total or 0),
        "users_blocked": int(blocked_n or 0),
        "liability_cents": int(liability or 0),
        "reserved_cents": int(reserved or 0),
        "revenue_usd_cents": int(revenue_cents),
        "translation_by_status": status_counts,
        "translations_24h": int(recent24 or 0),
        "ts": time.time(),
    }


@router.get("/stats")
async def admin_stats(
    _: None = Depends(require_admin_session),
    session: AsyncSession = Depends(get_db),
):
    return await _stats_payload(session)


async def _translation_logs_to_dicts(session: AsyncSession, logs: list) -> list[dict[str, Any]]:
    chat_ids: set[int] = set()
    for log in logs:
        chat_ids.add(log.source_id)
        chat_ids.add(log.destination_group_id)
    chat_map: dict[int, str] = {}
    if chat_ids:
        cr = await session.execute(select(ChatCache).where(ChatCache.id.in_(chat_ids)))
        for c in cr.scalars().all():
            chat_map[c.id] = c.title or str(c.id)
    out: list[dict[str, Any]] = []
    for log in logs:
        cc = getattr(log, "cost_usd_cents", None)
        out.append(
            {
                "id": log.id,
                "owner_telegram_id": log.owner_telegram_id,
                "source_id": log.source_id,
                "destination_group_id": log.destination_group_id,
                "source_name": chat_map.get(log.source_id, str(log.source_id)),
                "dest_name": chat_map.get(log.destination_group_id, str(log.destination_group_id)),
                "source_link": getattr(log, "source_link", None),
                "destination_link": getattr(log, "destination_link", None),
                "status": log.status,
                "error_message": log.error_message,
                "cost_usd_cents": cc,
                "cost_usd": round(cc / 100, 4) if cc is not None else None,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
        )
    return out


@router.get("/telemetry/drill")
async def telemetry_drill(
    metric: str,
    status: Optional[str] = None,
    limit: int = 150,
    _: None = Depends(require_admin_session),
    session: AsyncSession = Depends(get_db),
):
    """
    Drill-down rows for a telemetry aggregate. metric:
    users_total, users_blocked, liability, reserved, revenue, translations_24h, by_status (requires status=).
    """
    from datetime import datetime, timedelta, timezone

    limit = min(max(1, limit), 500)
    metric = (metric or "").strip().lower()

    if metric == "users_total":
        stmt = select(UserCredits).order_by(UserCredits.updated_at.desc()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
        total = (await session.execute(select(func.count()).select_from(UserCredits))).scalar_one()
        out = [
            {
                "telegram_id": int(uc.telegram_id),
                "username": uc.username,
                "balance_cents": uc.balance,
                "reserved_cents": getattr(uc, "reserved_balance", 0) or 0,
                "blocked": bool(getattr(uc, "blocked", False)),
                "updated_at": uc.updated_at.isoformat() if uc.updated_at else None,
            }
            for uc in rows
        ]
        return {
            "metric": metric,
            "title": "User accounts",
            "description": f"user_credits rows, newest activity first (showing {len(out)} of {int(total or 0)})",
            "rows": out,
            "truncated": len(rows) >= limit and int(total or 0) > limit,
        }

    if metric == "users_blocked":
        stmt = (
            select(UserCredits)
            .where(UserCredits.blocked == True)  # noqa: E712
            .order_by(UserCredits.updated_at.desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        out = [
            {
                "telegram_id": int(uc.telegram_id),
                "username": uc.username,
                "balance_cents": uc.balance,
                "reserved_cents": getattr(uc, "reserved_balance", 0) or 0,
                "updated_at": uc.updated_at.isoformat() if uc.updated_at else None,
            }
            for uc in rows
        ]
        return {
            "metric": metric,
            "title": "Blocked users",
            "description": "Accounts with blocked=true",
            "rows": out,
            "truncated": len(rows) >= limit,
        }

    if metric == "liability":
        stmt = select(UserCredits).order_by(UserCredits.balance.desc()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
        total_liab = (await session.execute(select(func.coalesce(func.sum(UserCredits.balance), 0)))).scalar_one()
        out = [
            {
                "telegram_id": int(uc.telegram_id),
                "username": uc.username,
                "balance_cents": uc.balance,
                "balance_usd": round(uc.balance / 100, 2),
                "reserved_cents": getattr(uc, "reserved_balance", 0) or 0,
            }
            for uc in rows
        ]
        return {
            "metric": metric,
            "title": "Liability — top balances",
            "description": f"Sum of all balances = {int(total_liab or 0)}¢ (${round((total_liab or 0) / 100, 2)}). Showing highest balances first.",
            "rows": out,
            "truncated": len(rows) >= limit,
        }

    if metric == "reserved":
        stmt = (
            select(UserCredits)
            .where(UserCredits.reserved_balance > 0)
            .order_by(UserCredits.reserved_balance.desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        total_r = (await session.execute(select(func.coalesce(func.sum(UserCredits.reserved_balance), 0)))).scalar_one()
        out = [
            {
                "telegram_id": int(uc.telegram_id),
                "username": uc.username,
                "balance_cents": uc.balance,
                "reserved_cents": getattr(uc, "reserved_balance", 0) or 0,
            }
            for uc in rows
        ]
        return {
            "metric": metric,
            "title": "Reserved credits (in-flight)",
            "description": f"Total reserved = {int(total_r or 0)}¢. Open requests holding balance.",
            "rows": out,
            "truncated": len(rows) >= limit,
        }

    if metric == "revenue":
        stmt = (
            select(TranslationLog)
            .where(TranslationLog.status == "success")
            .order_by(TranslationLog.created_at.desc())
            .limit(limit)
        )
        logs = (await session.execute(stmt)).scalars().all()
        rows = await _translation_logs_to_dicts(session, list(logs))
        rev = (await session.execute(select(func.coalesce(func.sum(TranslationLog.cost_usd_cents), 0)).where(TranslationLog.status == "success"))).scalar_one()
        return {
            "metric": metric,
            "title": "Revenue events (successful translations)",
            "description": f"Lifetime revenue from charges = {int(rev or 0)}¢. Latest successes first.",
            "rows": rows,
            "truncated": len(logs) >= limit,
        }

    if metric == "translations_24h":
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        stmt = (
            select(TranslationLog)
            .where(TranslationLog.created_at >= since)
            .order_by(TranslationLog.created_at.desc())
            .limit(limit)
        )
        logs = (await session.execute(stmt)).scalars().all()
        rows = await _translation_logs_to_dicts(session, list(logs))
        cnt = (
            await session.execute(
                select(func.count()).select_from(TranslationLog).where(TranslationLog.created_at >= since)
            )
        ).scalar_one()
        return {
            "metric": metric,
            "title": "Translations (last 24h)",
            "description": f"Total rows in window = {int(cnt or 0)}. Newest first.",
            "rows": rows,
            "truncated": len(logs) >= limit,
        }

    if metric == "by_status":
        if not status or not str(status).strip():
            raise HTTPException(status_code=400, detail="by_status requires status= query parameter")
        st = str(status).strip()
        stmt = (
            select(TranslationLog)
            .where(TranslationLog.status == st)
            .order_by(TranslationLog.created_at.desc())
            .limit(limit)
        )
        logs = (await session.execute(stmt)).scalars().all()
        rows = await _translation_logs_to_dicts(session, list(logs))
        cnt = (
            await session.execute(
                select(func.count()).select_from(TranslationLog).where(TranslationLog.status == st)
            )
        ).scalar_one()
        return {
            "metric": metric,
            "title": f"Translations — status “{st}”",
            "description": f"Total count with this status = {int(cnt or 0)}. Newest first.",
            "rows": rows,
            "truncated": len(logs) >= limit,
        }

    raise HTTPException(
        status_code=400,
        detail="Unknown metric. Use: users_total, users_blocked, liability, reserved, revenue, translations_24h, by_status",
    )


@router.get("/financial")
async def admin_financial(
    _: None = Depends(require_admin_session),
    session: AsyncSession = Depends(get_db),
):
    stats = await _stats_payload(session)
    settings = await get_system_settings(session)
    manual_bal = getattr(settings, "manual_provider_balance_cents", None)
    manual_cost = getattr(settings, "manual_monthly_provider_cost_cents", None)
    revenue = stats["revenue_usd_cents"]
    liability = stats["liability_cents"]
    est_cost = manual_cost if manual_cost is not None else 0
    margin = revenue - est_cost
    return {
        "revenue_usd_cents": revenue,
        "revenue_usd": round(revenue / 100, 2),
        "user_liability_usd_cents": liability,
        "user_liability_usd": round(liability / 100, 2),
        "manual_provider_balance_cents": manual_bal,
        "manual_provider_balance_usd": round(manual_bal / 100, 2) if manual_bal is not None else None,
        "manual_monthly_provider_cost_cents": manual_cost,
        "manual_monthly_provider_cost_usd": round(manual_cost / 100, 2) if manual_cost is not None else None,
        "gross_margin_usd_cents": margin,
        "gross_margin_usd": round(margin / 100, 2),
        "note": "Revenue is sum of successful translation charges; provider cost is manual until usage logging is added.",
    }


async def _recent_logs(session: AsyncSession, limit: int = 50) -> list[dict[str, Any]]:
    limit = min(max(1, limit), 100)
    stmt = select(TranslationLog).order_by(TranslationLog.created_at.desc()).limit(limit)
    logs = (await session.execute(stmt)).scalars().all()
    return await _translation_logs_to_dicts(session, list(logs))


@router.get("/stream")
async def admin_stream(
    request: Request,
    _: None = Depends(require_admin_session),
):
    from database.db import AsyncSessionLocal

    async def gen() -> AsyncIterator[str]:
        while True:
            if await request.is_disconnected():
                break
            try:
                async with AsyncSessionLocal() as session:
                    stats = await _stats_payload(session)
                    logs = await _recent_logs(session, 40)
                    payload = json.dumps({"stats": stats, "recent_logs": logs})
                    yield f"data: {payload}\n\n"
            except Exception as e:
                logger.exception("admin stream: %s", e)
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            await asyncio.sleep(2.5)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/sources/{source_id}/pause")
async def admin_pause_source(
    source_id: int,
    _: None = Depends(require_admin_session),
    session: AsyncSession = Depends(get_db),
):
    from sqlalchemy import update

    await session.execute(update(RoutingRule).where(RoutingRule.source_id == source_id).values(enabled=False))
    await session.commit()
    return {"status": "ok", "message": "Source paused"}


@router.post("/sources/{source_id}/unpause")
async def admin_unpause_source(
    source_id: int,
    _: None = Depends(require_admin_session),
    session: AsyncSession = Depends(get_db),
):
    from sqlalchemy import update

    await session.execute(update(RoutingRule).where(RoutingRule.source_id == source_id).values(enabled=True))
    await session.commit()
    return {"status": "ok", "message": "Source unpaused"}


@router.delete("/rules/{rule_id}")
async def admin_delete_rule(
    rule_id: int,
    _: None = Depends(require_admin_session),
    session: AsyncSession = Depends(get_db),
):
    from sqlalchemy import delete

    await session.execute(delete(RoutingRule).where(RoutingRule.id == rule_id))
    await session.commit()
    return {"status": "deleted"}
