"""Bağlı botları çalıştırır / durdurur. Yeni bot eklemek için restart gerekmez."""
import asyncio
import logging

from aiogram import Bot, Dispatcher

from .child import make_child_router
from .db import ManagedBot, all_bots
from .tg import make_bot, wait_online

log = logging.getLogger(__name__)
UPDATES = ["message", "callback_query", "my_chat_member"]


class BotManager:
    def __init__(self):
        self.bots: dict[int, Bot] = {}        # gönderim için (hepsi)
        self.dps: dict[int, Dispatcher] = {}  # sadece polling yapan (aktif) botlar
        self.tasks: dict[int, asyncio.Task] = {}
        self.crashes: dict[int, str] = {}  # bot_id -> son durma sebebi

    async def load_all(self):
        for mb in await all_bots():
            self.bots[mb.id] = make_bot(mb.token)
            if mb.enabled:
                await self.start_polling(mb)

    async def start_polling(self, mb: ManagedBot):
        if mb.id in self.dps:
            return
        from .external import ext_dir, mode
        d = ext_dir(mb)
        if d and mode(d) == "A":
            log.info("@%s A modunda: polling kendi servisinde.", mb.username)
            return
        bot = self.bots.get(mb.id) or make_bot(mb.token)
        self.bots[mb.id] = bot
        dp = Dispatcher()
        dp.include_router(make_child_router())
        self.dps[mb.id] = dp
        async def run():
            await wait_online(bot, f"@{mb.username}")
            await dp.start_polling(bot, handle_signals=False, close_bot_session=False, allowed_updates=UPDATES)

        task = asyncio.create_task(run(), name=f"bot-{mb.username}")
        task.add_done_callback(lambda t, bid=mb.id: self._on_done(bid, t))
        self.tasks[mb.id] = task
        log.info("Bot başlatıldı: @%s", mb.username)
        from .automsg import sync_commands
        asyncio.create_task(sync_commands(bot))

    def _on_done(self, bot_id: int, task: asyncio.Task):
        if task.cancelled():
            reason = "görev iptal edildi"
        elif task.exception():
            reason = f"{type(task.exception()).__name__}: {task.exception()}"
        else:
            reason = "dinleme beklenmedik şekilde sona erdi"
        # stop_polling ile bilerek durdurulduysa dps'den önceden çıkarılmıştır
        if self.tasks.get(bot_id) is task and bot_id in self.dps:
            self.crashes[bot_id] = reason[:300]
            log.error("Bot %s dinlemesi durdu: %s", bot_id, reason)

    def is_healthy(self, bot_id: int) -> bool:
        t = self.tasks.get(bot_id)
        return bot_id in self.dps and t is not None and not t.done()

    async def restart(self, mb: ManagedBot):
        """Dinlemesi durmuş botu temizleyip yeniden başlatır."""
        self.dps.pop(mb.id, None)
        old = self.tasks.pop(mb.id, None)
        if old and not old.done():
            old.cancel()
        await self.start_polling(mb)

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
