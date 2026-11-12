"""Bot nesnesi üretici: TELEGRAM_API ayarlıysa Telegram'a aracı (Worker) üzerinden bağlanır."""
import asyncio
import logging

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from .config import TELEGRAM_API

log = logging.getLogger(__name__)


def make_bot(token: str, **kw) -> Bot:
    if TELEGRAM_API:
        kw["session"] = AiohttpSession(api=TelegramAPIServer.from_base(TELEGRAM_API))
    return Bot(token, **kw)


async def wait_online(bot: Bot, name: str = "bot"):
    """Telegram'a ulaşılana kadar bekler (çökmek yerine); 5 sn'den 60 sn'ye artan aralıkla dener."""
    delay = 5
    while True:
        try:
            return await bot.get_me()
        except Exception as e:
            log.warning("%s Telegram'a ulaşamadı (%s), %d sn sonra tekrar.", name, type(e).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
