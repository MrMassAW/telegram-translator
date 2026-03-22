"""
Start the Translator Bot and the Web App (Mini App + API) in one script.
Press Ctrl+C to stop both.
"""
import asyncio
import logging
import sys
import threading

# Load env before importing app/bot
from dotenv import load_dotenv
load_dotenv()

def run_web():
    import uvicorn
    uvicorn.run("web.app:app", host="0.0.0.0", port=8000, log_level="info")

async def run_bot():
    from bot.main import main
    await main()

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logger = logging.getLogger(__name__)

    # Start web app in a background thread; bind 0.0.0.0:8000 for Docker / reverse proxies
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    logger.info("Web app listening on 0.0.0.0:8000 (set WEB_APP_URL to your public HTTPS base URL)")

    # Run bot in main thread
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Stopped.")
    except Exception as e:
        logger.exception("Bot error: %s", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
