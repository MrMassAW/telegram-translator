from sqlalchemy import Column, Integer, String, BigInteger, DateTime, UniqueConstraint, Boolean, Index, Text
from sqlalchemy.sql import func
from .db import Base

class CreditLedger(Base):
    """Append-only ledger for balance changes (Stripe, future crypto). idempotency_key prevents duplicate webhook application."""

    __tablename__ = "credit_ledger"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    delta_cents = Column(Integer, nullable=False)
    idempotency_key = Column(String(255), unique=True, nullable=False)
    source = Column(String(32), nullable=False)  # stripe, crypto, ...
    external_ref = Column(String(255), nullable=True)
    meta_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class UserCredits(Base):
    __tablename__ = "user_credits"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String, nullable=True)
    balance = Column(Integer, nullable=False, default=0)  # USD cents total (= free + paid)
    free_balance_cents = Column(Integer, nullable=False, default=0)  # promo / admin grants; spent first
    paid_balance_cents = Column(Integer, nullable=False, default=0)  # Stripe top-ups; spent after free
    reserved_balance = Column(Integer, nullable=False, default=0)  # USD cents held during request
    currency = Column(String, nullable=False, default="USD")
    receive_reports_telegram = Column(Boolean, nullable=False, default=True)
    spam_protection_enabled = Column(Boolean, nullable=False, default=False)
    spam_max_messages = Column(Integer, nullable=False, default=50)
    spam_window_minutes = Column(Integer, nullable=False, default=5)
    blocked = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class AuthorizedUser(Base):
    __tablename__ = "authorized_users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String, nullable=True)
    added_at = Column(DateTime(timezone=True), server_default=func.now())

class RoutingRule(Base):
    __tablename__ = "routing_rules"

    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(BigInteger, nullable=False, index=True)  # User or Channel ID
    destination_group_id = Column(BigInteger, nullable=False)
    destination_language = Column(String, nullable=False)
    translate_images = Column(Boolean, nullable=False, default=False)
    translate_poll = Column(Boolean, nullable=False, default=False)
    owner_telegram_id = Column(BigInteger, nullable=True, index=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint('source_id', 'destination_group_id', name='_source_dest_uc'),
    )


class TranslationLog(Base):
    __tablename__ = "translation_log"

    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(BigInteger, nullable=False, index=True)
    destination_group_id = Column(BigInteger, nullable=False)
    rule_id = Column(Integer, nullable=True)
    owner_telegram_id = Column(BigInteger, nullable=True, index=True)
    status = Column(String, nullable=False)  # success, failed, skipped
    source_link = Column(String, nullable=True)
    destination_link = Column(String, nullable=True)
    error_message = Column(String, nullable=True)
    cost_usd_cents = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class SpamMessageLog(Base):
    """One row per message processed for (source_id, owner); used to count messages in time window for spam protection."""
    __tablename__ = "spam_message_log"

    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(BigInteger, nullable=False, index=True)
    owner_telegram_id = Column(BigInteger, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_spam_message_log_source_owner_created", "source_id", "owner_telegram_id", "created_at"),)


class SourceChannel(Base):
    """Standalone source (no destinations yet). source_id = Telegram chat/channel id."""
    __tablename__ = "source_channels"

    source_id = Column(BigInteger, primary_key=True, index=True)
    title = Column(String, nullable=True)
    owner_telegram_id = Column(BigInteger, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SourceExcludedTerms(Base):
    """Per-source terms that must not be translated (e.g. brand names). terms_json is a JSON array of strings."""
    __tablename__ = "source_excluded_terms"

    source_id = Column(BigInteger, primary_key=True, index=True)
    terms_json = Column(Text, nullable=False, default="[]")


class ChatCache(Base):
    __tablename__ = "chat_cache"

    id = Column(BigInteger, primary_key=True, index=True) # Chat ID
    title = Column(String, nullable=True)
    type = Column(String, nullable=False) # private, group, supergroup, channel
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SystemSettings(Base):
    """Admin-only settings (single row, id=1)."""
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, default=1)
    max_pairs = Column(Integer, nullable=False, default=10)
    max_destinations_per_source = Column(Integer, nullable=False, default=10)
    max_message_length = Column(Integer, nullable=False, default=4096)
    # Per-translation pricing (USD cents); admin-configurable
    cents_per_text_translation = Column(Integer, nullable=False, default=1)
    cents_per_image_translation = Column(Integer, nullable=False, default=10)
    # Manual financial inputs (USD cents); used until provider usage is logged automatically
    manual_provider_balance_cents = Column(Integer, nullable=True)
    manual_monthly_provider_cost_cents = Column(Integer, nullable=True)
