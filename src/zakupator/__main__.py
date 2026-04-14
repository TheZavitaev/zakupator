"""Entry point: `python -m zakupator`.

Boots the SearchEngine, the Telegram bot, and starts long-polling. Handles
graceful shutdown so httpx clients get closed on Ctrl+C.
"""

from __future__ import annotations

import asyncio
import logging

from zakupator.bot import build_dispatcher
from zakupator.config import load_settings
from zakupator.db import create_all, init_engine
from zakupator.search import SearchEngine


async def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    init_engine(settings.database_url)
    await create_all()

    async with SearchEngine() as engine:
        bot, dp = await build_dispatcher(settings, engine)
        try:
            await dp.start_polling(bot)
        finally:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
