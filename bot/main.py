import asyncio
import logging
import os
from aiogram import Bot, Dispatcher
from shared.config import BOT_TOKEN, WEB_APP_URL
from bot.handlers import router
from database.db import init_db

# Verbose logging: file + console. Log file: logs/translator_bot.log (grep for action=, result=, module=)
def setup_logging():
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "translator_bot.log")
    format_str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(format_str, datefmt=date_fmt)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, format=format_str, datefmt=date_fmt, force=True)
    root = logging.getLogger()
    root.addHandler(file_handler)
    logging.getLogger("aiogram.event").setLevel(logging.DEBUG)
    return log_file

async def main():
    log_file = setup_logging()
    _log = logging.getLogger(__name__)
    _log.info("Log file: %s", log_file)
    
    # Initialize Database
    await init_db()
    
    # Initialize Bot and Dispatcher
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    
    # Register Routers
    dp.include_router(router)
    
    # Single "Open" button that opens the mini app (no command menu)
    # The button appears as the bot icon left of the message input; tap it to see "Open" and launch the app.
    from aiogram.types import MenuButtonWebApp, WebAppInfo
    await bot.set_my_commands([])
    if WEB_APP_URL:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Open", web_app=WebAppInfo(url=WEB_APP_URL)))
            _log.info("Menu button set to 'Open' (web app): %s", WEB_APP_URL[:50] + "..." if len(WEB_APP_URL) > 50 else WEB_APP_URL)
        except Exception as e:
            _log.warning("Failed to set menu button: %s", e)
    else:
        _log.warning("WEB_APP_URL not set; menu button not updated")
    
    # Drop pending updates so we do not process messages that arrived while bot was offline
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Start Polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
