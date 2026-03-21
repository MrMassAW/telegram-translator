# Telegram Translator

Telegram bot that translates messages (including images via Gemini), plus a FastAPI web app and admin console.

## Requirements

- Python 3.12+ (3.11+ may work; Docker image uses 3.12)
- A [Telegram Bot](https://core.telegram.org/bots/tutorial) token
- A [Google AI (Gemini) API key](https://ai.google.dev/)

## Setup

1. Clone the repository.

2. Create a virtual environment and install dependencies:

   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

   On Linux or macOS: `source .venv/bin/activate`

3. Copy the environment template and edit values:

   ```bash
   copy .env.example .env
   ```

   Set at least `BOT_TOKEN`, `GEMINI_API_KEY`, `ADMIN_USERNAME`, `ADMIN_ID`, and `WEB_APP_URL` (public base URL of the web app, as Telegram will open it). See [.env.example](.env.example) for all options.

4. **Production admin:** set a strong `SECRET_KEY`, prefer `ADMIN_PASSWORD_HASH` (bcrypt) over plain `ADMIN_PASSWORD`, and set `SESSION_COOKIE_SECURE=true` when the site is served over HTTPS.

## Run

**Bot + web (recommended for Mini App / webhooks):**

```bash
python start_all.py
```

The API and static UI listen on `http://0.0.0.0:8000`. Use a reverse proxy or tunnel (e.g. ngrok) so `WEB_APP_URL` matches what Telegram users hit.

**Bot only:**

```bash
python Start_Translator_Bot.py
```

**Web / admin only (dev, with reload):**

```bash
python Start_Admin_Portal.py
```

## Docker

1. Copy `.env.example` to `.env` and fill in secrets.

2. Build and run:

   ```bash
   docker compose up --build
   ```

The compose file sets `DATABASE_URL` to a SQLite file under `/app/data` with a named volume so the database persists. Port `8000` is published for HTTP.

## Project layout

| Path | Role |
|------|------|
| `bot/` | aiogram bot entrypoint and handlers |
| `web/` | FastAPI app, routes, static admin/Mini App assets |
| `shared/` | Config, billing, prompts, utilities |
| `database/` | SQLAlchemy models and DB session |
| `scripts/` | Optional tooling (e.g. test messages); see [scripts/README.md](scripts/README.md) |

## Security

Never commit `.env` or real API tokens. If secrets were ever exposed, rotate the Telegram bot token, Gemini key, admin password, and Stripe keys.

## License

See [LICENSE](LICENSE).
