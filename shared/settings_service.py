"""Single-row system settings (id=1). Used by web API and bot."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import SystemSettings
from shared.config import (
    CENTS_PER_IMAGE_DEFAULT,
    CENTS_PER_TEXT_DEFAULT,
    MAX_DESTINATIONS_PER_SOURCE_DEFAULT,
    MAX_MESSAGE_LENGTH_DEFAULT,
    MAX_PAIRS_DEFAULT,
)


async def get_system_settings(session: AsyncSession) -> SystemSettings:
    """Return the single system settings row; create with defaults if missing."""
    r = await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
    row = r.scalar_one_or_none()
    if row is None:
        row = SystemSettings(
            id=1,
            max_pairs=MAX_PAIRS_DEFAULT,
            max_destinations_per_source=MAX_DESTINATIONS_PER_SOURCE_DEFAULT,
            max_message_length=MAX_MESSAGE_LENGTH_DEFAULT,
            cents_per_text_translation=CENTS_PER_TEXT_DEFAULT,
            cents_per_image_translation=CENTS_PER_IMAGE_DEFAULT,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row
