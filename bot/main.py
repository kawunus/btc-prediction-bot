import asyncio
import logging
import os

from aiogram import Bot, Dispatcher

from db import Database
from scheduler import Scheduler
from handlers import register_handlers

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    bot = Bot(token=os.environ["BOT_TOKEN"])
    db = Database(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 3306)),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
    )
    await db.connect()
    await db.init_schema()

    dp = Dispatcher()
    scheduler = Scheduler(bot, db)

    register_handlers(dp, db, scheduler)

    scheduler.start()

    # Re-schedule rounds that were active before restart
    await scheduler.reschedule_active_rounds()

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.stop()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
