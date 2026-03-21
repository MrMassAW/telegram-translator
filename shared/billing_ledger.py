"""
Apply credit ledger entries atomically with UserCredits.balance (cents).
"""
import json
import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import CreditLedger, UserCredits
from shared.credit_service import (
    deduct_free_first,
    deduct_paid_first,
    ensure_user as credit_ensure_user,
    sync_balance_total,
)

logger = logging.getLogger(__name__)


async def apply_ledger_entry(
    session: AsyncSession,
    telegram_id: int,
    delta_cents: int,
    idempotency_key: str,
    source: str,
    *,
    username: Optional[str] = None,
    external_ref: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
) -> tuple[bool, Optional[int]]:
    """
    Insert one ledger row and update balance in one transaction (positive top-ups only).
    Returns (applied, new_balance). If idempotency_key already exists, returns (False, None).
    """
    if delta_cents <= 0:
        raise ValueError("apply_ledger_entry expects delta_cents > 0; use apply_refund_deduction for refunds")

    meta_json = json.dumps(meta) if meta else None
    await credit_ensure_user(session, telegram_id, username)
    stmt_uc = select(UserCredits).where(UserCredits.telegram_id == telegram_id)
    r = await session.execute(stmt_uc)
    uc = r.scalar_one_or_none()
    if not uc:
        return False, None

    entry = CreditLedger(
        telegram_id=telegram_id,
        delta_cents=delta_cents,
        idempotency_key=idempotency_key,
        source=source,
        external_ref=external_ref,
        meta_json=meta_json,
    )
    session.add(entry)
    uc.paid_balance_cents = (getattr(uc, "paid_balance_cents", 0) or 0) + delta_cents
    sync_balance_total(uc)

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return False, None

    await session.refresh(uc)
    return True, uc.balance


async def apply_refund_deduction(
    session: AsyncSession,
    telegram_id: int,
    refund_cents: int,
    idempotency_key: str,
    *,
    username: Optional[str] = None,
    external_ref: Optional[str] = None,
) -> tuple[bool, Optional[int]]:
    """
    Deduct up to min(balance, refund_cents). Ledger records the actual negative delta.
    """
    await credit_ensure_user(session, telegram_id, username)
    stmt_uc = select(UserCredits).where(UserCredits.telegram_id == telegram_id)
    r = await session.execute(stmt_uc)
    uc = r.scalar_one_or_none()
    if not uc:
        return False, None

    sync_balance_total(uc)
    actual = min(uc.balance, refund_cents)
    meta: dict[str, Any] = {
        "requested_refund_cents": refund_cents,
        "actual_deduct_cents": actual,
    }
    if refund_cents > actual:
        logger.warning(
            "Refund exceeds remaining balance: telegram_id=%s requested=%s balance=%s",
            telegram_id,
            refund_cents,
            uc.balance,
        )

    delta = -actual

    entry = CreditLedger(
        telegram_id=telegram_id,
        delta_cents=delta,
        idempotency_key=idempotency_key,
        source="stripe",
        external_ref=external_ref,
        meta_json=json.dumps(meta),
    )
    session.add(entry)
    deduct_paid_first(uc, actual)

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return False, None

    await session.refresh(uc)
    return True, uc.balance


async def apply_admin_adjustment(
    session: AsyncSession,
    telegram_id: int,
    delta_cents: int,
    *,
    reason: str,
    note: str | None = None,
    username: str | None = None,
) -> tuple[int, int]:
    """
    Apply a signed balance change from the operator console. Ledger source='admin'.
    Returns (new_balance_cents, ledger_row_id).
    """
    import uuid

    if delta_cents == 0:
        raise ValueError("delta_cents must be non-zero")

    await credit_ensure_user(session, telegram_id, username)
    stmt_uc = select(UserCredits).where(UserCredits.telegram_id == telegram_id)
    r = await session.execute(stmt_uc)
    uc = r.scalar_one_or_none()
    if not uc:
        raise RuntimeError("User credits missing after ensure_user")

    # Admin adjustments affect free credit only (positive adds free; negative takes free first, then paid)
    if delta_cents > 0:
        uc.free_balance_cents = (getattr(uc, "free_balance_cents", 0) or 0) + delta_cents
    else:
        deduct_free_first(uc, -delta_cents)
    sync_balance_total(uc)
    reserved = getattr(uc, "reserved_balance", 0) or 0
    if uc.balance < reserved:
        uc.reserved_balance = uc.balance

    meta: dict[str, Any] = {
        "reason": reason,
        "note": note,
        "balance_after": uc.balance,
        "free_balance_after": getattr(uc, "free_balance_cents", 0) or 0,
        "paid_balance_after": getattr(uc, "paid_balance_cents", 0) or 0,
    }
    idempotency_key = f"admin:{uuid.uuid4().hex}"
    entry = CreditLedger(
        telegram_id=telegram_id,
        delta_cents=delta_cents,
        idempotency_key=idempotency_key,
        source="admin",
        external_ref=None,
        meta_json=json.dumps(meta),
    )
    session.add(entry)
    await session.commit()
    await session.refresh(uc)
    await session.refresh(entry)
    return uc.balance, entry.id
