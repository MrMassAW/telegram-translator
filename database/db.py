from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./bot.db")
if DATABASE_URL.startswith("sqlite://"):
    DATABASE_URL = DATABASE_URL.replace("sqlite://", "sqlite+aiosqlite://")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

class Base(DeclarativeBase):
    pass

async def init_db():
    from database import models  # noqa: F401 - register models with Base.metadata
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL;"))
        await conn.execute(text("PRAGMA synchronous=NORMAL;"))
        await conn.run_sync(Base.metadata.create_all)
        # Add new columns if missing (idempotent)
        for col_sql in (
            "ALTER TABLE routing_rules ADD COLUMN translate_images INTEGER DEFAULT 0",
            "ALTER TABLE routing_rules ADD COLUMN translate_poll INTEGER DEFAULT 0",
            "ALTER TABLE routing_rules ADD COLUMN owner_telegram_id INTEGER",
            "ALTER TABLE routing_rules ADD COLUMN enabled INTEGER DEFAULT 1",
            "ALTER TABLE user_credits ADD COLUMN receive_reports_telegram INTEGER DEFAULT 1",
            "ALTER TABLE user_credits ADD COLUMN reserved_balance INTEGER DEFAULT 0",
            "ALTER TABLE user_credits ADD COLUMN spam_protection_enabled INTEGER DEFAULT 0",
            "ALTER TABLE user_credits ADD COLUMN spam_max_messages INTEGER DEFAULT 50",
            "ALTER TABLE user_credits ADD COLUMN spam_window_minutes INTEGER DEFAULT 5",
            "ALTER TABLE translation_log ADD COLUMN cost_usd_cents INTEGER",
            "ALTER TABLE user_credits ADD COLUMN blocked INTEGER DEFAULT 0",
            "ALTER TABLE user_credits ADD COLUMN free_balance_cents INTEGER DEFAULT 0",
            "ALTER TABLE user_credits ADD COLUMN paid_balance_cents INTEGER DEFAULT 0",
            "ALTER TABLE system_settings ADD COLUMN manual_provider_balance_cents INTEGER",
            "ALTER TABLE system_settings ADD COLUMN manual_monthly_provider_cost_cents INTEGER",
            "ALTER TABLE system_settings ADD COLUMN cents_per_text_translation INTEGER DEFAULT 1",
            "ALTER TABLE system_settings ADD COLUMN cents_per_image_translation INTEGER DEFAULT 10",
        ):
            try:
                await conn.execute(text(col_sql))
            except Exception:
                pass  # column already exists
        # Composite index for spam count query (idempotent)
        try:
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_spam_message_log_source_owner_created "
                "ON spam_message_log (source_id, owner_telegram_id, created_at)"
            ))
        except Exception:
            pass
        try:
            await conn.execute(
                text(
                    "UPDATE system_settings SET cents_per_text_translation = 1 "
                    "WHERE cents_per_text_translation IS NULL"
                )
            )
            await conn.execute(
                text(
                    "UPDATE system_settings SET cents_per_image_translation = 10 "
                    "WHERE cents_per_image_translation IS NULL"
                )
            )
        except Exception:
            pass
        # One-time split: legacy balance → free bucket (paid stays 0 until Stripe)
        try:
            await conn.execute(
                text(
                    "UPDATE user_credits SET free_balance_cents = balance, paid_balance_cents = 0 "
                    "WHERE COALESCE(free_balance_cents, 0) + COALESCE(paid_balance_cents, 0) = 0 "
                    "AND COALESCE(balance, 0) > 0"
                )
            )
        except Exception:
            pass

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
