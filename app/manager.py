"""Bağlı botları çalıştırır / durdurur. Yeni bot eklemek için restart gerekmez."""
import asyncio
import logging

from aiogram import Bot, Dispatcher

from .child import make_child_router
from .db import ManagedBot, all_bots

log = logging.getLogger(__name__)
UPDATES = ["message", "callback_query", "my_chat_member"]


class BotManager:
    def __init__(self):
        self.bots: dict[int, Bot] = {}        # gönderim için (hepsi)
        self.dps: dict[int, Dispatcher] = {}  # sadece polling yapan (aktif) botlar
        self.tasks: dict[int, asyncio.Task] = {}

    async def load_all(self):
        for mb in await all_bots():
            self.bots[mb.id] = Bot(mb.token)
            if mb.enabled:
                await self.start_polling(mb)

    async def start_polling(self, mb: ManagedBot):
        if mb.id in self.dps:
            return
        bot = self.bots.get(mb.id) or Bot(mb.token)
        self.bots[mb.id] = bot
        dp = Dispatcher()
        dp.include_router(make_child_router())
        self.dps[mb.id] = dp
        self.tasks[mb.id] = asyncio.create_task(
            dp.start_polling(bot, handle_signals=False, close_bot_session=False, allowed_updates=UPDATES),
            name=f"bot-{mb.username}")
        log.info("Bot başlatıldı: @%s", mb.username)

    async def stop_polling(self, bot_id: int):
        dp = self.dps.pop(bot_id, None)
        task = self.tasks.pop(bot_id, None)
        if dp:
            try:
                await dp.stop_polling()
            except RuntimeError:
                pass
        if task:
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                task.cancel()

    async def remove(self, bot_id: int):
        await self.stop_polling(bot_id)
        bot = self.bots.pop(bot_id, None)
        if bot:
            await bot.session.close()

    def sender(self, bot_id: int) -> Bot | None:
        return self.bots.get(bot_id)

    def is_running(self, bot_id: int) -> bool:
        return bot_id in self.dps

    async def close(self):
        for bid in list(self.bots):
            await self.remove(bid)
