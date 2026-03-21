import asyncio
import logging
from bot.main import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.error(f"Bot crashed: {e}")
        import traceback
        traceback.print_exc()
