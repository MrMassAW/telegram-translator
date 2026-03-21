# Scripts

## send_test_messages.py

Sends a set of test messages to a Telegram group to cover different content types (text, formatting, links, media, albums, polls). Use this to test the translator bot against all supported Telegram message types.

### Setup

1. **SENDER_BOT_TOKEN** – Use a *different* bot to send test messages (create one with @BotFather). Then the translator bot (BOT_TOKEN) will receive these messages as normal updates and will translate. If you use the same bot, Telegram does not deliver its own messages to it. Fallback: **BOT_TOKEN** if SENDER_BOT_TOKEN is not set.
2. **TEST_GROUP_ID** – Target group chat ID (e.g. `-1001234567890`). Add *both* the sender bot and the translator bot to the group.
   - Add the sender bot to the group, then send any message in the group.
   - Get updates: `curl "https://api.telegram.org/bot<SENDER_TOKEN>/getUpdates"` and read `message.chat.id` for the group.

### Usage

```bash
# From project root (uses BOT_TOKEN and TEST_GROUP_ID from .env)
python scripts/send_test_messages.py

# Override group ID
python scripts/send_test_messages.py --group-id -1001234567890

# Preview only (no API calls)
python scripts/send_test_messages.py --dry-run

# Custom delay between messages (default 1s)
python scripts/send_test_messages.py --delay 2

# Run only specific test(s) by number or name
python scripts/send_test_messages.py --test 4
python scripts/send_test_messages.py --test 4_complex_formatting
python scripts/send_test_messages.py --test 1 4 20
```

### Message types sent

| #  | Type                    | Description                          |
|----|-------------------------|--------------------------------------|
| 1  | Plain text              | Simple message                        |
| 2  | Formatted text          | Bold, italic, code, underline        |
| 3  | Links                   | Inline HTML links                     |
| 4  | Complex formatting      | Nested tags, spoiler, pre, list       |
| 5  | MarkdownV2              | Bold, italic, code                    |
| 6  | Mention / hashtag       | @username #hashtag                    |
| 7  | Multi-line               | Paragraphs and newlines               |
| 8  | Photo                   | Single image (URL)                    |
| 9  | Photo + plain caption   | Image with text                       |
| 10 | Photo + formatted caption | Image with HTML caption            |
| 11 | Video                   | Short video (URL)                     |
| 12 | Video + caption         | Video with caption                    |
| 13 | Document                | PDF with caption                      |
| 16 | Animation               | GIF with caption                      |
| 18 | Media group             | Album of 3 photos                     |
| 19 | Media group + caption   | Album with caption on first photo     |
| 20 | Poll                    | Regular multiple-choice poll           |
| 21 | Quiz                    | Quiz with correct answer + explanation|
| 24 | Long caption            | Photo with ~1024 char caption         |
| 25 | Long text               | Text near 4096 chars                  |

Audio, voice, and sticker are not included (would require local files or existing file_id). Add them locally if needed.
