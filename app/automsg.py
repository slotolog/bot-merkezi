"""Otomatik mesaj zinciri (start sonrası) ve promo havuzu (/promo, 'Sonraki Promo')."""
import asyncio
import logging
import re
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BotCommand
from sqlalchemy import delete, select, update

from .content import send_content
from .db import AutoQueue, Item, ManagedBot, Session, Subscriber, utcnow
from .keyboards import build_markup

log = logging.getLogger(__name__)

_UNITS = {"sn": 1, "s": 1, "saniye": 1, "dk": 60, "m": 60, "dakika": 60,
          "sa": 3600, "saat": 3600, "h": 3600, "g": 86400, "gün": 86400, "gun": 86400, "d": 86400}


def parse_delay(text: str) -> int | None:
    """'hemen', '0', '5sn', '10 dk', '1 saat', '2g' → saniye. Anlaşılmazsa None."""
    t = text.strip().lower()
    if t in ("hemen", "0", "-", "şimdi"):
        return 0
    m = re.fullmatch(r"(\d+)\s*([a-zçğışöü]+)", t)
    if not m or m.group(2) not in _UNITS:
        return None
    sec = int(m.group(1)) * _UNITS[m.group(2)]
    return sec if sec <= 30 * 86400 else None


def fmt_delay(sec: int) -> str:
    if sec <= 0:
        return "hemen"
    for unit, name in ((86400, "gün"), (3600, "saat"), (60, "dk")):
        if sec % unit == 0:
            return f"{sec // unit} {name} sonra"
    return f"{sec} sn sonra"


async def list_items(bot_id: int, kind: str, only_enabled=False) -> list[Item]:
    async with Session() as s:
        q = select(Item).where(Item.bot_id == bot_id, Item.kind == kind)
        if only_enabled:
            q = q.where(Item.enabled.is_(True))
        return list((await s.scalars(q.order_by(Item.position, Item.id))).all())


async def send_item(bot: Bot, chat_id: int, item: Item, extra_markup=None):
    markup = build_markup(item.buttons) or (extra_markup if len(item.msgs) == 1 else None)
    return await send_content(bot, chat_id, item.msgs, markup, item.btn_text)


async def _bump(item_id: int, n: int = 1):
    async with Session() as s:
        await s.execute(update(Item).where(Item.id == item_id).values(count=Item.count + n))
        await s.commit()


# ---------- otomatik zincir ----------
async def start_chain(bot: Bot, user_id: int):
    """Yeni abone için zinciri başlatır: 'hemen' olanlar şimdi gider, diğerleri kuyruğa."""
    async with Session() as s:
        b = await s.get(ManagedBot, bot.id)
        if not b or b.auto_enabled is False:
            return
    items = await list_items(bot.id, "auto", only_enabled=True)
    later = []
    for it in items:
        if it.delay_sec <= 0:
            try:
                await send_item(bot, user_id, it)
                await _bump(it.id)
            except Exception as e:
                log.warning("Otomatik mesaj gönderilemedi: %s", e)
            await asyncio.sleep(0.3)
        else:
            later.append(AutoQueue(bot_id=bot.id, user_id=user_id, item_id=it.id,
                                   due_at=utcnow() + timedelta(seconds=it.delay_sec)))
    if later:
        async with Session() as s:
            s.add_all(later)
            await s.commit()


class AutoWorker:
    """Kuyruktaki gecikmeli mesajları zamanı gelince gönderir."""

    def __init__(self, manager):
        self.manager = manager

    async def loop(self):
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Otomatik mesaj kuyruğu hatası")
            await asyncio.sleep(5)

    async def tick(self):
        async with Session() as s:
            rows = (await s.scalars(select(AutoQueue).where(AutoQueue.due_at <= utcnow())
                                    .order_by(AutoQueue.due_at).limit(300))).all()
        for row in rows:
            async with Session() as s:
                await s.execute(delete(AutoQueue).where(AutoQueue.id == row.id))
                item = await s.get(Item, row.item_id)
                b = await s.get(ManagedBot, row.bot_id)
                sub = await s.scalar(select(Subscriber).where(
                    Subscriber.bot_id == row.bot_id, Subscriber.user_id == row.user_id))
                await s.commit()
            if not item or not item.enabled or not b or b.auto_enabled is False:
                continue
            if not sub or sub.status != "active":
                continue
            bot = self.manager.sender(row.bot_id)
            if not bot:
                continue
            for _ in range(3):
                try:
                    await send_item(bot, row.user_id, item)
                    await _bump(item.id)
                    break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except (TelegramForbiddenError, TelegramBadRequest):
                    from .child import set_status
                    await set_status(row.bot_id, row.user_id, "blocked")
                    break
                except Exception as e:
                    log.warning("Gecikmeli mesaj hatası: %s", e)
                    break
            await asyncio.sleep(0.05)


# ---------- promo havuzu ----------
async def promo_on(bot_id: int) -> bool:
    async with Session() as s:
        b = await s.get(ManagedBot, bot_id)
        return bool(b) and b.promo_enabled is not False


async def send_next_promo(bot: Bot, user_id: int, extra_markup=None) -> bool:
    if not await promo_on(bot.id):
        return False
    promos = await list_items(bot.id, "promo", only_enabled=True)
    if not promos:
        return False
    async with Session() as s:
        sub = await s.scalar(select(Subscriber).where(Subscriber.bot_id == bot.id, Subscriber.user_id == user_id))
        last = sub.promo_last if sub else None
        ids = [p.id for p in promos]
        nxt = promos[(ids.index(last) + 1) % len(promos)] if last in ids else promos[0]
        if sub:
            sub.promo_last = nxt.id
            sub.last_seen = utcnow()
        await s.commit()
    await send_item(bot, user_id, nxt, extra_markup)
    await _bump(nxt.id)
    return True


async def sync_commands(bot: Bot):
    """Botun '/' menüsünü günceller (promo varsa /promo da görünür)."""
    cmds = [BotCommand(command="start", description="Başlat")]
    if await promo_on(bot.id) and await list_items(bot.id, "promo", only_enabled=True):
        cmds.append(BotCommand(command="promo", description="Güncel promo"))
    cmds.append(BotCommand(command="stop", description="Bildirimleri kapat"))
    try:
        await bot.set_my_commands(cmds)
    except Exception as e:
        log.warning("Komutlar ayarlanamadı: %s", e)
