import google.generativeai as genai
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from database.models import ChatCache
from shared.config import GEMINI_API_KEY, GEMINI_IMAGE_MODEL, GEMINI_TEXT_MODEL
from shared.prompt_loader import (
    get_prompt_image_has_text,
    get_prompt_image_native_translate,
    get_prompt_image_ocr_extract,
)
import logging
import json

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)

logger = logging.getLogger(__name__)

# Max dimension (width or height) for AI-generated image output to limit cost
IMAGE_MAX_OUTPUT_PX = 1024

# ISO 639-1 codes → English names for LLM prompts (avoids e.g. "it" read as pronoun "it").
_ISO639_1_TO_ENGLISH_NAME = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "ru": "Russian",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "uk": "Ukrainian",
    "tr": "Turkish",
    "ar": "Arabic",
    "hi": "Hindi",
}


def _target_language_prompt_label(code: str) -> str:
    key = (code or "").strip().lower()
    if not key:
        return "English"
    if key in _ISO639_1_TO_ENGLISH_NAME:
        return _ISO639_1_TO_ENGLISH_NAME[key]
    return (code or "").strip()


def _excluded_terms_instruction(terms: list[str] | None) -> str:
    if not terms:
        return ""
    joined = "; ".join(terms)
    return (
        f"If any of these terms already appear in the source text, keep them exactly unchanged: {joined}. "
        "Do not add these terms if they are not present in the original image text. "
        "If a term appears with different casing, preserve the source casing. "
    )


def _mime_type_for_image_bytes(image_bytes: bytes) -> str:
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if image_bytes[:2] == b"\xff\xd8":
        return "image/jpeg"
    return "image/jpeg"


async def download_telegram_photo_bytes(bot, file_id: str) -> bytes | None:
    """Download Telegram photo bytes by file_id. Returns None if missing or empty."""
    try:
        file = await bot.get_file(file_id)
        data = await bot.download_file(file.file_path)
        if hasattr(data, "read"):
            image_bytes = data.read()
        elif hasattr(data, "getvalue"):
            image_bytes = data.getvalue()
        else:
            image_bytes = bytes(data) if data else b""
        if not image_bytes:
            return None
        return image_bytes
    except Exception as e:
        logger.warning("download_telegram_photo_bytes failed: %s", e)
        return None


def _native_image_excluded_instruction_block(excluded_terms: list[str] | None) -> str:
    """Middle fragment for native image prompt: background clause + optional glossary handling."""
    ex = _excluded_terms_instruction(excluded_terms)
    if ex:
        return (
            ex
            + "Keep the background and non-text graphics identical. For those excluded terms, keep the original text unchanged. "
        )
    return "Keep the background and non-text graphics identical. "


def _parse_yes_no_vision_response(text: str | None) -> bool:
    """Strict YES/NO: default False on empty or ambiguous (conservative: skip image translation)."""
    if not text:
        return False
    parts = text.strip().upper().split()
    if not parts:
        return False
    first = parts[0]
    if first.startswith("YES"):
        return True
    if first.startswith("NO"):
        return False
    return False


def _resize_image_to_max(image_bytes: bytes, max_px: int = IMAGE_MAX_OUTPUT_PX) -> bytes:
    """Resize image so the longest side is at most max_px; return bytes as PNG. Returns original bytes on failure."""
    import io
    try:
        import PIL.Image
        img = PIL.Image.open(io.BytesIO(image_bytes))
        if img.mode == "P":
            img = img.convert("RGBA")
        w, h = img.size
        if w <= max_px and h <= max_px:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        if w >= h:
            new_w, new_h = max_px, max(1, int(h * max_px / w))
        else:
            new_w, new_h = max(1, int(w * max_px / h)), max_px
        resized = img.resize((new_w, new_h), PIL.Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        logger.warning("Resize image failed, returning original: %s", e)
        return image_bytes


class TranslationService:
    @staticmethod
    async def translate_text(text: str, target_lang: str, excluded_terms: list[str] | None = None) -> str:
        if not text:
            return ""
        lang = _target_language_prompt_label(target_lang)
        ex = _excluded_terms_instruction(excluded_terms)
        prompt = (
            f"Translate the following text into {lang}. "
            "Preserve all formatting (bold, italic, links, etc.) using standard Markdown. "
            "Do not translate the content inside links [text](url) unless it is visible text. "
            f"{ex}"
            "Output ONLY the translated text in Markdown format.\n\n"
            f"Text: {text}"
        )
        try:
            model = genai.GenerativeModel(GEMINI_TEXT_MODEL)
            response = await model.generate_content_async(prompt)
            return (response.text or "").strip() or text
        except Exception as e:
            logger.error("Translation failed: %s", e)
            return text

    @staticmethod
    async def translate_html(html_content: str, target_lang: str, excluded_terms: list[str] | None = None) -> str:
        """Translate HTML content to target_lang; preserve all HTML tags. Used for Telegram HTML pipeline."""
        if not html_content:
            return ""
        lang = _target_language_prompt_label(target_lang)
        ex = _excluded_terms_instruction(excluded_terms)
        prompt = (
            f"Translate the following HTML content into {lang}. "
            "Preserve all HTML tags exactly. Only translate the text content between tags and the visible link text inside <a>; "
            "do not translate href attribute values. "
            f"{ex}"
            "Output only the translated HTML, no explanation.\n\n"
            f"HTML:\n{html_content}"
        )
        try:
            model = genai.GenerativeModel(GEMINI_TEXT_MODEL)
            response = await model.generate_content_async(prompt)
            return (response.text or "").strip() or html_content
        except Exception as e:
            logger.error("translate_html failed: %s", e)
            return html_content

    @staticmethod
    async def image_has_readable_text(image_bytes: bytes) -> bool:
        """Vision YES/NO: whether the image contains readable text. False on API/open failure (forward original)."""
        import io
        import PIL.Image
        prompt = get_prompt_image_has_text()
        try:
            img = PIL.Image.open(io.BytesIO(image_bytes))
        except Exception as e:
            logger.warning("image_has_readable_text: failed to open image: %s", e)
            return False
        try:
            model = genai.GenerativeModel(GEMINI_TEXT_MODEL)
            response = await model.generate_content_async([prompt, img])
            raw = (response.text or "").strip() if response else ""
            return _parse_yes_no_vision_response(raw)
        except Exception as e:
            logger.warning("image_has_readable_text failed: %s", e)
            return False

    @staticmethod
    async def extract_text_from_image(image_bytes: bytes) -> str:
        """Use Gemini vision to extract all visible text from an image. Returns empty string on failure."""
        import io
        import PIL.Image
        prompt = get_prompt_image_ocr_extract()
        try:
            img = PIL.Image.open(io.BytesIO(image_bytes))
        except Exception as e:
            logger.warning(f"Failed to open image: {e}")
            return ""
        try:
            model = genai.GenerativeModel(GEMINI_TEXT_MODEL)
            response = await model.generate_content_async([prompt, img])
            if response and response.text:
                return response.text.strip()
        except Exception as e:
            logger.warning("Image extraction failed: %s", e)
        return ""

    @staticmethod
    async def extract_and_translate_image(bot, file_id: str, target_lang: str) -> str:
        """Download photo from Telegram, extract text via vision, translate to target_lang."""
        try:
            image_bytes = await download_telegram_photo_bytes(bot, file_id)
            if not image_bytes:
                return ""
            extracted = await TranslationService.extract_text_from_image(image_bytes)
            if not extracted:
                return ""
            return await TranslationService.translate_text(extracted, target_lang)
        except Exception as e:
            logger.warning(f"extract_and_translate_image failed: {e}")
            return ""

    @staticmethod
    async def translate_image_native(
        bot, file_id: str, target_lang: str, excluded_terms: list[str] | None = None
    ) -> bytes | None:
        """
        Send image to Gemini image model; return regenerated image bytes with text translated to target_lang.
        Uses gemini-3.1-flash-image-preview (or GEMINI_IMAGE_MODEL) with response_modalities IMAGE.
        Returns None if there is no readable text (forward original), on API failure, or if no image in response.
        """
        try:
            image_bytes = await download_telegram_photo_bytes(bot, file_id)
            if not image_bytes:
                return None
            if not await TranslationService.image_has_readable_text(image_bytes):
                return None
            mime_type = _mime_type_for_image_bytes(image_bytes)
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=GEMINI_API_KEY)
            lang = _target_language_prompt_label(target_lang)
            excluded_instruction = _native_image_excluded_instruction_block(excluded_terms)
            template = get_prompt_image_native_translate()
            prompt = (
                template.replace("{target_language_label}", lang).replace(
                    "{excluded_instruction}", excluded_instruction
                )
            )
            contents = [
                prompt,
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            ]
            config = types.GenerateContentConfig(response_modalities=["IMAGE"])
            response = await client.aio.models.generate_content(
                model=GEMINI_IMAGE_MODEL,
                contents=contents,
                config=config,
            )
            if not response or not response.candidates:
                return None
            for part in response.candidates[0].content.parts:
                if part.inline_data and part.inline_data.data:
                    out_bytes = bytes(part.inline_data.data)
                    return _resize_image_to_max(out_bytes)
            return None
        except Exception as e:
            logger.warning(f"translate_image_native failed: {e}")
            return None


class ChatService:
    @staticmethod
    async def update_chat_cache(session: AsyncSession, chat_id: int, title: str, chat_type: str):
        try:
            stmt = select(ChatCache).where(ChatCache.id == chat_id)
            result = await session.execute(stmt)
            chat = result.scalar_one_or_none()

            if chat:
                chat.title = title
                chat.type = chat_type
            else:
                chat = ChatCache(id=chat_id, title=title, type=chat_type)
                session.add(chat)
            
            await session.commit()
        except Exception as e:
            logger.error(f"Failed to update chat cache: {e}")
            await session.rollback()

    @staticmethod
    async def get_all_chats(session: AsyncSession):
        stmt = select(ChatCache).order_by(ChatCache.title)
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def handle_migration(session: AsyncSession, old_id: int, new_id: int):
        import json
        from sqlalchemy import delete
        from database.models import RoutingRule, SourceExcludedTerms
        from shared.utils import normalize_excluded_terms

        # 1. Handle ChatCache
        # Check if new_id exists
        stmt = select(ChatCache).where(ChatCache.id == new_id)
        result = await session.execute(stmt)
        existing_new = result.scalar_one_or_none()
        
        if existing_new:
            # New ID already exists, just delete the old one
            await session.execute(delete(ChatCache).where(ChatCache.id == old_id))
        else:
            # Update old to new
            await session.execute(
                update(ChatCache)
                .where(ChatCache.id == old_id)
                .values(id=new_id)
            )
            
        # 2. Handle RoutingRule (Source)
        # Update Source IDs - if conflict, we might have duplicates. 
        # Ideally we check, but for now let's try update and ignore integrity error if it happens (meaning rules already exist)
        try:
             await session.execute(
                update(RoutingRule)
                .where(RoutingRule.source_id == old_id)
                .values(source_id=new_id)
            )
        except Exception:
             pass # Ignore if rules for new_id already exist

        # 3. Handle RoutingRule (Destination)
        try:
            await session.execute(
                update(RoutingRule)
                .where(RoutingRule.destination_group_id == old_id)
                .values(destination_group_id=new_id)
            )
        except Exception:
            pass

        # 4. SourceExcludedTerms (per-source glossary)
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

        await session.commit()

class ReportService:
    @staticmethod
    async def send_summary_report(bot, source_id: int, source_name: str, results: list, source_link: str = None, recipient_owner_ids: set = None):
        """
        Sends a consolidated summary report only to rule owners who set up the translation pair and have receive_reports_telegram True.
        results: list of dicts with keys: dest_id, dest_name, status, link, error
        recipient_owner_ids: optional set of telegram_id to send the report to (if they have receive_reports_telegram).
        """
        from database.models import UserCredits
        from database.db import get_db
        from sqlalchemy import select
        import html

        # Build Report (HTML) once
        text = f"📝 <b>Translation Report</b>\n"
        source_display = html.escape(source_name)
        if source_link:
            source_display = f"<a href='{source_link}'>{source_display}</a>"
        text += f"<b>Source</b>: {source_display} (<code>{source_id}</code>)\n\n"
        total_cents = 0
        for res in results:
            status_icon = "✅" if res["status"] == "Success" else "❌"
            dest_name = html.escape(res.get("dest_name", str(res["dest_id"])))
            link = res.get("link")
            cost_cents = res.get("cost_usd_cents") or 0
            total_cents += cost_cents
            cost_str = f" ${cost_cents / 100:.2f}" if cost_cents else ""
            row = f"{status_icon} <b>{dest_name}</b>{cost_str}"
            if res["status"] in ("Failed", "Skipped"):
                error_msg = html.escape(str(res.get("error", "Unknown")))
                row += f"\n   <code>{error_msg}</code>"
            elif link:
                row += f" | <a href='{link}'>View</a>"
            else:
                row += " | N/A"
            if res.get("caption_too_long"):
                row += "\n   Translation was too long to send as caption with the media; sent as separate message."
            text += row + "\n"
        if total_cents > 0:
            text += f"\n<b>Total cost</b>: ${total_cents / 100:.2f}"

        async def send_to(
            chat_id: int,
            balance_cents: int | None = None,
            *,
            free_cents: int | None = None,
            paid_cents: int | None = None,
        ):
            msg = text
            if balance_cents is not None:
                msg += f"\n\n💳 <b>Your balance</b>: ${balance_cents / 100:.2f} USD"
                if free_cents is not None and paid_cents is not None:
                    msg += (
                        f"\n   Free ${free_cents / 100:.2f} · Paid ${paid_cents / 100:.2f} (free used first)"
                    )
            try:
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML", disable_web_page_preview=True)
            except Exception as e:
                logger.error(f"Failed to send report to {chat_id}: {e}")

        # Send only to pair owners who opted in; include their remaining balance so they see it go down after charges
        if recipient_owner_ids:
            async for session in get_db():
                for oid in recipient_owner_ids:
                    if oid is None:
                        continue
                    stmt = select(UserCredits).where(UserCredits.telegram_id == oid)
                    r = await session.execute(stmt)
                    uc = r.scalar_one_or_none()
                    if uc and getattr(uc, "receive_reports_telegram", True):
                        balance_cents = getattr(uc, "balance", 0) or 0
                        fc = int(getattr(uc, "free_balance_cents", 0) or 0)
                        pc = int(getattr(uc, "paid_balance_cents", 0) or 0)
                        await send_to(oid, balance_cents, free_cents=fc, paid_cents=pc)
                break
