#!/usr/bin/env python3
"""
Send a set of test messages to a Telegram group to exercise all content types
(plain text, formatting, links, media, albums, polls). Use for translator testing.

Use a *separate* bot (SENDER_BOT_TOKEN) to send so the translator bot (BOT_TOKEN)
receives these messages as normal updates and triggers translation. If you use
the same bot for both, Telegram does not deliver its own messages to it.

Usage:
  Set SENDER_BOT_TOKEN (or BOT_TOKEN) and TEST_GROUP_ID in .env. Optionally:
    python scripts/send_test_messages.py --group-id -1001234567890
    python scripts/send_test_messages.py --dry-run

Requires: aiogram, python-dotenv
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# Add project root so we can use shared config if needed
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from aiogram import Bot
from aiogram.types import (
    InputMediaPhoto,
    InputMediaVideo,
)

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
# Prefer a separate sender bot so the translator bot (BOT_TOKEN) receives these as updates
SENDER_BOT_TOKEN = os.getenv("SENDER_BOT_TOKEN") or os.getenv("BOT_TOKEN")
TEST_GROUP_ID = os.getenv("TEST_GROUP_ID")  # e.g. -1001234567890

# Public sample URLs for media (no local files required)
SAMPLE_IMAGE_URL = "https://picsum.photos/400/300"
SAMPLE_IMAGE_URL_2 = "https://picsum.photos/400/301"
SAMPLE_IMAGE_URL_3 = "https://picsum.photos/400/302"
SAMPLE_VIDEO_URL = "https://www.w3schools.com/html/mov_bbb.mp4"
SAMPLE_DOCUMENT_URL = "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"
SAMPLE_GIF_URL = "https://media.giphy.com/media/3o7TKsQ8MJHyTASOry/giphy.gif"

DELAY_BETWEEN_MESSAGES = 1.0  # seconds, to avoid rate limits


# -----------------------------------------------------------------------------
# Test cases: (name, coroutine that sends one or more messages)
# -----------------------------------------------------------------------------

async def send_plain_text(bot: Bot, chat_id: int) -> None:
    await bot.send_message(chat_id, "Test 1: Plain text. This is a simple message with no formatting.")

async def send_formatted_text(bot: Bot, chat_id: int) -> None:
    await bot.send_message(
        chat_id,
        "Test 2: <b>Bold</b>, <i>italic</i>, <code>code</code>, <u>underline</u>, <s>strikethrough</s>.",
        parse_mode="HTML",
    )

async def send_links(bot: Bot, chat_id: int) -> None:
    await bot.send_message(
        chat_id,
        'Test 3: Inline link: <a href="https://example.com">click here</a>. Another: <a href="https://telegram.org">Telegram</a>.',
        parse_mode="HTML",
    )

async def send_complex_formatting(bot: Bot, chat_id: int) -> None:
    await bot.send_message(
        chat_id,
        "Test 4: Complex formatting.\n"
        "• <b>Bold with <a href='https://example.com'>link</a></b>\n"
        "• <tg-spoiler>Spoiler text</tg-spoiler>\n"
        "• <pre>Preformatted block</pre>\n"
        "• List line 1\n• List line 2",
        parse_mode="HTML",
    )

async def send_markdown_v2(bot: Bot, chat_id: int) -> None:
    # MarkdownV2 requires escaping: _ * [ ] ( ) ~ ` > # + - = | { } . !
    await bot.send_message(
        chat_id,
        r"Test 5: *Bold* and _italic_ and \`code\` in MarkdownV2\.",
        parse_mode="MarkdownV2",
    )

async def send_mention_hashtag(bot: Bot, chat_id: int) -> None:
    await bot.send_message(
        chat_id,
        "Test 6: Mention @username and hashtag #test. Plain text with entities.",
    )

async def send_multiline(bot: Bot, chat_id: int) -> None:
    await bot.send_message(
        chat_id,
        "Test 7: Multi-line text.\n\nLine one.\nLine two.\n\nParagraph two.",
    )

async def send_photo(bot: Bot, chat_id: int) -> None:
    await bot.send_photo(chat_id, photo=SAMPLE_IMAGE_URL)

async def send_photo_caption_plain(bot: Bot, chat_id: int) -> None:
    await bot.send_photo(
        chat_id,
        photo=SAMPLE_IMAGE_URL,
        caption="Test 9: Photo with plain caption.",
    )

async def send_photo_caption_formatted(bot: Bot, chat_id: int) -> None:
    await bot.send_photo(
        chat_id,
        photo=SAMPLE_IMAGE_URL,
        caption="Test 10: Photo with <b>formatted</b> caption and <a href='https://example.com'>link</a>.",
        parse_mode="HTML",
    )

async def send_video(bot: Bot, chat_id: int) -> None:
    await bot.send_video(chat_id, video=SAMPLE_VIDEO_URL)

async def send_video_caption(bot: Bot, chat_id: int) -> None:
    await bot.send_video(
        chat_id,
        video=SAMPLE_VIDEO_URL,
        caption="Test 12: Video with caption.",
    )

async def send_document(bot: Bot, chat_id: int) -> None:
    await bot.send_document(
        chat_id,
        document=SAMPLE_DOCUMENT_URL,
        caption="Test 13: Document (PDF) with caption.",
    )

async def send_animation(bot: Bot, chat_id: int) -> None:
    await bot.send_animation(
        chat_id,
        animation=SAMPLE_GIF_URL,
        caption="Test 16: Animation (GIF) with caption.",
    )

async def send_media_group(bot: Bot, chat_id: int) -> None:
    await bot.send_media_group(
        chat_id,
        media=[
            InputMediaPhoto(media=SAMPLE_IMAGE_URL),
            InputMediaPhoto(media=SAMPLE_IMAGE_URL_2),
            InputMediaPhoto(media=SAMPLE_IMAGE_URL_3),
        ],
    )

async def send_media_group_with_caption(bot: Bot, chat_id: int) -> None:
    # Only first photo can have caption in a media group
    await bot.send_media_group(
        chat_id,
        media=[
            InputMediaPhoto(media=SAMPLE_IMAGE_URL, caption="Test 19: Album with caption on first photo."),
            InputMediaPhoto(media=SAMPLE_IMAGE_URL_2),
            InputMediaPhoto(media=SAMPLE_IMAGE_URL_3),
        ],
    )

async def send_poll(bot: Bot, chat_id: int) -> None:
    await bot.send_poll(
        chat_id,
        question="Test 20: Regular poll — What is 2+2?",
        options=["3", "4", "5", "6"],
        is_anonymous=False,
    )

async def send_quiz(bot: Bot, chat_id: int) -> None:
    await bot.send_poll(
        chat_id,
        question="Test 21: Quiz — Capital of France?",
        options=["London", "Paris", "Berlin", "Madrid"],
        is_anonymous=False,
        type="quiz",
        correct_option_id=1,
        explanation="Paris is the capital of France.",
    )

async def send_long_caption(bot: Bot, chat_id: int) -> None:
    long_caption = "Test 24: Long caption. " + ("Lorem ipsum dolor sit amet. " * 40)
    long_caption = long_caption[:1020] + "…"
    await bot.send_photo(
        chat_id,
        photo=SAMPLE_IMAGE_URL,
        caption=long_caption,
    )

async def send_long_text(bot: Bot, chat_id: int) -> None:
    long_text = "Test 25: Long text. " + ("The quick brown fox jumps. " * 200)
    long_text = long_text[:4080] + "…"
    await bot.send_message(chat_id, long_text)


# Ordered list of (name, coroutine)
TEST_CASES = [
    ("1_plain_text", send_plain_text),
    ("2_formatted_text", send_formatted_text),
    ("3_links", send_links),
    ("4_complex_formatting", send_complex_formatting),
    ("5_markdown_v2", send_markdown_v2),
    ("6_mention_hashtag", send_mention_hashtag),
    ("7_multiline", send_multiline),
    ("8_photo", send_photo),
    ("9_photo_caption_plain", send_photo_caption_plain),
    ("10_photo_caption_formatted", send_photo_caption_formatted),
    ("11_video", send_video),
    ("12_video_caption", send_video_caption),
    ("13_document", send_document),
    ("16_animation", send_animation),
    ("18_media_group", send_media_group),
    ("19_media_group_caption", send_media_group_with_caption),
    ("20_poll", send_poll),
    ("21_quiz", send_quiz),
    ("24_long_caption", send_long_caption),
    ("25_long_text", send_long_text),
]


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Send test messages to a Telegram group.")
    p.add_argument(
        "--group-id",
        type=str,
        default=os.getenv("TEST_GROUP_ID"),
        help="Target group chat ID (e.g. -1001234567890). Else use env TEST_GROUP_ID.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print what would be sent, do not call the API.",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=DELAY_BETWEEN_MESSAGES,
        help=f"Seconds between messages (default {DELAY_BETWEEN_MESSAGES}).",
    )
    p.add_argument(
        "--test",
        type=str,
        nargs="*",
        metavar="N",
        help="Run only these test(s). By number (e.g. 4) or name (e.g. 4_complex_formatting). Multiple: --test 1 4 20. Without --test, run all.",
    )
    return p.parse_args()


def filter_test_cases(cases: list, test_specs: list[str] | None) -> list:
    """Return cases that match any of the given specs. If test_specs is None or empty, return all."""
    if not test_specs:
        return cases
    selected = []
    for name, coro in cases:
        for t in test_specs:
            t = t.strip()
            if t.isdigit():
                if name.startswith(t + "_") or name == t:
                    selected.append((name, coro))
                    break
            else:
                if name == t or t in name:
                    selected.append((name, coro))
                    break
    return selected


async def run(
    bot: Bot | None,
    chat_id: int,
    dry_run: bool,
    delay: float,
    test_specs: list[str] | None = None,
) -> None:
    chat_id = int(chat_id)
    cases = filter_test_cases(TEST_CASES, test_specs)
    if test_specs and not cases:
        print("No tests matched. Use --test with a number (e.g. 4) or name (e.g. 4_complex_formatting).", file=sys.stderr)
        return
    for name, coro in cases:
        if dry_run:
            print(f"[DRY-RUN] Would send: {name}")
            continue
        try:
            await coro(bot, chat_id)
            print(f"Sent: {name}")
        except Exception as e:
            print(f"FAILED {name}: {e}", file=sys.stderr)
        await asyncio.sleep(delay)


def main() -> None:
    args = parse_args()
    if not SENDER_BOT_TOKEN and not args.dry_run:
        print("Error: SENDER_BOT_TOKEN or BOT_TOKEN not set. Set in .env or environment.", file=sys.stderr)
        sys.exit(1)
    group_id = args.group_id
    if not group_id and not args.dry_run:
        print("Error: TEST_GROUP_ID not set. Set it in .env or pass --group-id.", file=sys.stderr)
        sys.exit(1)
    if args.dry_run and not group_id:
        group_id = "0"  # placeholder, not used

    async def _main(test_specs: list[str] | None = None) -> None:
        bot = None
        if not args.dry_run:
            bot = Bot(token=SENDER_BOT_TOKEN)
        try:
            await run(bot, int(group_id), args.dry_run, args.delay, test_specs)
        finally:
            if bot:
                await bot.session.close()

    asyncio.run(_main(args.test))


if __name__ == "__main__":
    main()
