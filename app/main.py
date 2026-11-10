import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from .admin import channel_router, router as admin_router
from .admin_auto import router as auto_router
from .admin_channels import router as channels_router
from .broadcast import BroadcastRunner
from .config import ADMIN_BOT_TOKEN, ADMIN_IDS, PUBLIC_URL
from .settings import RUNTIME, load as load_settings, storage_id
from .db import init_db
from .automsg import AutoWorker
from .channels import FeedRunner
from .manager import BotManager
from .haberolog import HaberologSync
from .web import start_web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("botmerkezi")


async def main():
    if not ADMIN_BOT_TOKEN or not ADMIN_IDS:
        sys.exit("ADMIN_BOT_TOKEN ve ADMIN_IDS .env içinde ayarlanmalı.")
    await init_db()
    await load_settings()
    if not storage_id():
        log.warning("Depo kanalı ayarlı değil: kanala /depo yazılınca ayarlanır.")
    admin_bot = Bot(ADMIN_BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    RUNTIME["admin_bot"] = admin_bot
    manager = BotManager()
    await manager.load_all()
    runner = BroadcastRunner(manager, admin_bot)

    dp = Dispatcher(manager=manager, runner=runner)
    dp.include_router(admin_router)
    dp.include_router(auto_router)
    dp.include_router(channels_router)
    dp.include_router(channel_router)

    await admin_bot.set_my_commands([BotCommand(command="menu", description="Ana menü"),
                                     BotCommand(command="iptal", description="İşlemi iptal et")])
    web_runner = await start_web()
    if not PUBLIC_URL:
        log.warning("PUBLIC_URL boş: buton tıklama takibi kapalı.")
    sched = asyncio.create_task(runner.scheduler_loop())
    auto_task = asyncio.create_task(AutoWorker(manager).loop())
    feed_task = asyncio.create_task(FeedRunner(manager).loop())
    hb_task = asyncio.create_task(HaberologSync(manager).loop())
    log.info("Yönetici bot çalışıyor. Bağlı bot sayısı: %d", len(manager.bots))
    try:
        await dp.start_polling(admin_bot, allowed_updates=["message", "callback_query", "channel_post"])
    finally:
        sched.cancel()
        auto_task.cancel()
        feed_task.cancel()
        hb_task.cancel()
        await web_runner.cleanup()
        await manager.close()


if __name__ == "__main__":
    asyncio.run(main())
