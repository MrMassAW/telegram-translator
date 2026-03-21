"""
Credit system (USD). free_balance_cents + paid_balance_cents = balance (total).
Consumption: free first, then paid. Reserved applies to total.
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database.models import UserCredits

# USD pricing defaults when rates not passed (stored in cents)
CENTS_PER_TEXT = 1  # $0.01 per text translation
CENTS_PER_IMAGE = 10  # $0.10 per image translation

# New users get $1.00 free credit (100 cents) — free bucket only
NEW_USER_FREE_CENTS = 100


def _free_cents(uc: UserCredits) -> int:
    return int(getattr(uc, "free_balance_cents", 0) or 0)


def _paid_cents(uc: UserCredits) -> int:
    return int(getattr(uc, "paid_balance_cents", 0) or 0)


def sync_balance_total(uc: UserCredits) -> None:
    """Keep balance column = free + paid."""
    uc.balance = _free_cents(uc) + _paid_cents(uc)


def deduct_free_first(uc: UserCredits, amount_cents: int) -> None:
    """Reduce balances by amount_cents, taking from free wallet first, then paid."""
    if amount_cents <= 0:
        return
    free = _free_cents(uc)
    paid = _paid_cents(uc)
    take_free = min(free, amount_cents)
    rest = amount_cents - take_free
    take_paid = min(paid, rest)
    uc.free_balance_cents = free - take_free
    uc.paid_balance_cents = paid - take_paid
    sync_balance_total(uc)


def deduct_paid_first(uc: UserCredits, amount_cents: int) -> None:
    """Reduce balances by amount_cents, taking from paid first (Stripe refunds)."""
    if amount_cents <= 0:
        return
    free = _free_cents(uc)
    paid = _paid_cents(uc)
    take_paid = min(paid, amount_cents)
    rest = amount_cents - take_paid
    take_free = min(free, rest)
    uc.paid_balance_cents = paid - take_paid
    uc.free_balance_cents = free - take_free
    sync_balance_total(uc)


async def ensure_user(session: AsyncSession, telegram_id: int, username: str | None = None) -> UserCredits:
    """Get or create user credits row. New users receive $1.00 (100 cents) in free balance only."""
    stmt = select(UserCredits).where(UserCredits.telegram_id == telegram_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        user = UserCredits(
            telegram_id=telegram_id,
            username=username,
            balance=NEW_USER_FREE_CENTS,
            free_balance_cents=NEW_USER_FREE_CENTS,
            paid_balance_cents=0,
            currency="USD",
            receive_reports_telegram=True,
            blocked=False,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    elif username and user.username != username:
        user.username = username
        await session.commit()
        await session.refresh(user)
    return user


async def get_balance(session: AsyncSession, telegram_id: int) -> int:
    """Return total balance (cents) for user, or 0 if no row."""
    stmt = select(UserCredits).where(UserCredits.telegram_id == telegram_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        return 0
    return _free_cents(user) + _paid_cents(user)


async def get_available_balance(session: AsyncSession, telegram_id: int) -> int:
    """Return available balance (total - reserved) in cents."""
    stmt = select(UserCredits).where(UserCredits.telegram_id == telegram_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        return 0
    reserved = getattr(user, "reserved_balance", 0) or 0
    total = _free_cents(user) + _paid_cents(user)
    return max(0, total - reserved)


async def has_sufficient_balance(session: AsyncSession, telegram_id: int, amount_cents: int) -> bool:
    """Check if user has at least amount_cents available (total - reserved)."""
    return await get_available_balance(session, telegram_id) >= amount_cents


async def reserve(session: AsyncSession, telegram_id: int, amount_cents: int) -> bool:
    """Reserve amount_cents from total available. Returns True if successful."""
    if amount_cents <= 0:
        return True
    stmt = select(UserCredits).where(UserCredits.telegram_id == telegram_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        return False
    reserved = getattr(user, "reserved_balance", 0) or 0
    total = _free_cents(user) + _paid_cents(user)
    available = total - reserved
    if available < amount_cents:
        return False
    user.reserved_balance = reserved + amount_cents
    await session.commit()
    return True


async def commit_reservation(session: AsyncSession, telegram_id: int, amount_cents: int) -> None:
    """Commit reserved amount: deduct from free first, then paid; reduce reserved."""
    if amount_cents <= 0:
        return
    stmt = select(UserCredits).where(UserCredits.telegram_id == telegram_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        return
    deduct_free_first(user, amount_cents)
    user.reserved_balance = max(0, (getattr(user, "reserved_balance", 0) or 0) - amount_cents)
    await session.commit()


async def release_reservation(session: AsyncSession, telegram_id: int, amount_cents: int) -> None:
    """Release reserved amount: subtract from reserved_balance only."""
    if amount_cents <= 0:
        return
    stmt = select(UserCredits).where(UserCredits.telegram_id == telegram_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        return
    user.reserved_balance = max(0, (getattr(user, "reserved_balance", 0) or 0) - amount_cents)
    await session.commit()


def compute_credit_cost_cents(
    has_text: bool,
    num_images: int,
    *,
    cents_per_text: int | None = None,
    cents_per_image: int | None = None,
) -> int:
    """Cost for one rule in cents. Uses module defaults if rates omitted."""
    ct = CENTS_PER_TEXT if cents_per_text is None else cents_per_text
    ci = CENTS_PER_IMAGE if cents_per_image is None else cents_per_image
    cost = ct if has_text else 0
    cost += num_images * ci
    return cost


async def deduct(session: AsyncSession, telegram_id: int, amount: int) -> bool:
    """Legacy: direct deduct. Prefer reserve/commit_reservation for translation flow."""
    if amount <= 0:
        return True
    stmt = select(UserCredits).where(UserCredits.telegram_id == telegram_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        return False
    total = _free_cents(user) + _paid_cents(user)
    if total < amount:
        return False
    deduct_free_first(user, amount)
    await session.commit()
    return True
