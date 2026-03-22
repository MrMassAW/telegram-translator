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

   Set at least `BOT_TOKEN`, `GEMINI_API_KEY`, and `WEB_APP_URL` (public base URL of the web app, as Telegram will open it). See [.env.example](.env.example) for all options.

4. **Production admin:** set a strong `SECRET_KEY`, prefer `ADMIN_PASSWORD_HASH` (bcrypt) over plain `ADMIN_PASSWORD`, and set `SESSION_COOKIE_SECURE=true` when the site is served over HTTPS.

## Run

**Bot + web (recommended for Mini App / webhooks):**

```bash
python start_all.py
```

The API and static UI listen on `http://0.0.0.0:8000`. Set `WEB_APP_URL` to the **public HTTPS URL** users reach (your platform’s assigned URL or a reverse proxy in front of port 8000).

For **web-only local dev** (hot reload): `uvicorn web.app:app --reload --host 127.0.0.1 --port 8000` (project root, `.env` present). For **bot-only** (no HTTP): `python -c "import asyncio; from dotenv import load_dotenv; load_dotenv(); from bot.main import main; asyncio.run(main())"`.

## Docker

1. Copy `.env.example` to `.env` and fill in secrets.

2. Build and run:

   ```bash
   docker compose up --build
   ```

The compose file sets `DATABASE_URL` to a SQLite file under `/app/data` with a named volume so the database persists. Port `8000` is published for HTTP.

## DigitalOcean App Platform

Use the root [`Dockerfile`](Dockerfile) as the build. An example spec lives at [`.do/app.yaml`](.do/app.yaml): set `github.repo` to your GitHub path (or connect the repo in the UI and align **HTTP port 8000** and the health check path **`/health`**).

Apps **do not read a `.env` file from Git** (and `.env` must not be committed). In the DigitalOcean UI, add **Environment Variables** (runtime) for the same keys as [.env.example](.env.example). Minimum to boot: `BOT_TOKEN`, `GEMINI_API_KEY`, `WEB_APP_URL`.

- **`WEB_APP_URL`:** Use your App Platform **live HTTPS URL** (the default route to this service) so Telegram’s Mini App button matches what users open.
- **Health check:** The app exposes **`GET /health`** returning `{"status":"ok"}` — point the component health check at **`/health`** on port **8000** (avoids depending on static `index.html`).
- **`DATABASE_URL`:** The [`Dockerfile`](Dockerfile) defaults to `sqlite+aiosqlite:////app/data/bot.db` under `/app/data`. Add a **persistent volume** mounted at `/app/data` in App Platform if you need SQLite to survive redeploys; otherwise data is ephemeral.
- **Production admin:** Set `SECRET_KEY`, `SESSION_COOKIE_SECURE=true`, and prefer `ADMIN_PASSWORD_HASH` over plain `ADMIN_PASSWORD`.

## Project layout

| Path | Role |
|------|------|
| `.do/app.yaml` | Optional DigitalOcean App Platform spec template |
| `bot/` | aiogram bot entrypoint and handlers |
| `web/` | FastAPI app, routes, static admin/Mini App assets |
| `shared/` | Config, billing, prompts, utilities |
| `database/` | SQLAlchemy models and DB session |
| `scripts/` | Optional local scripts (placeholder only) |

## Security

Never commit `.env` or real API tokens. If secrets were ever exposed, rotate the Telegram bot token, Gemini key, admin password, and Stripe keys.

## License

See [LICENSE](LICENSE).
