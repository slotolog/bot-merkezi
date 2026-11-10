"""Abonelerin kullandığı botların (Bot 1, Bot 2 ...) davranışı."""
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from sqlalchemy import select

from .automsg import send_next_promo, start_chain
from .content import content_ids, send_content
from .db import Session, Subscriber, get_bot_row, utcnow
from .keyboards import build_markup, build_reply_menu

log = logging.getLogger(__name__)


async def set_status(bot_id: int, user_id: int, status: str):
    async with Session() as s:
        sub = await s.scalar(select(Subscriber).where(Subscriber.bot_id == bot_id, Subscriber.user_id == user_id))
        if sub and sub.status != status:
            sub.status = status
            sub.left_at = None if status == "active" else utcnow()
            await s.commit()


async def send_stored(bot: Bot, chat_id: int, stored: dict, extra_markup=None):
    ids = content_ids(stored)
    markup = build_markup(stored.get("buttons")) or (extra_markup if len(ids) == 1 else None)
    await send_content(bot, chat_id, ids, markup, stored.get("btn_text"))


def make_child_router() -> Router:
    r = Router()

    @r.message(CommandStart(), F.chat.type == "private")
    async def on_start(m: Message, bot: Bot, command: CommandObject):
        source = (command.args or "organik")[:64]
        u = m.from_user
        is_new = False
        async with Session() as s:
            sub = await s.scalar(select(Subscriber).where(Subscriber.bot_id == bot.id, Subscriber.user_id == u.id))
            if sub is None:
                is_new = True
                s.add(Subscriber(bot_id=bot.id, user_id=u.id, username=u.username, first_name=u.first_name,
                                 lang=u.language_code, source=source))
            else:
                sub.status, sub.left_at, sub.last_seen = "active", None, utcnow()
                sub.username, sub.first_name = u.username, u.first_name
            await s.commit()

        row = await get_bot_row(bot.id)
        menu = (row.menu or {}) if row else {}
        reply_menu = build_reply_menu(menu.get("rows"))
        welcome = row.welcome if row else None
        try:
            if welcome:
                has_inline = bool(welcome.get("buttons"))
                is_album = len(content_ids(welcome)) > 1
                await send_stored(bot, m.chat.id, welcome, None if has_inline else reply_menu)
                if reply_menu and (has_inline or is_album):
                    await m.answer("👇", reply_markup=reply_menu)
            else:
                await m.answer(f"Hoş geldin {u.first_name}! 👋", reply_markup=reply_menu)
        except Exception as e:  # depo mesajı silinmiş vb.
            log.warning("Karşılama gönderilemedi (%s): %s", bot.id, e)
            await m.answer(f"Hoş geldin {u.first_name}! 👋", reply_markup=reply_menu)
        if is_new:
            await start_chain(bot, u.id)

    @r.message(Command("promo"), F.chat.type == "private")
    async def on_promo(m: Message, bot: Bot):
        if not await send_next_promo(bot, m.from_user.id):
            await m.answer("Şu an aktif bir promo yok. Yeni kampanyalar geldiğinde haber vereceğiz! 🔔")

    @r.message(Command("stop"), F.chat.type == "private")
    async def on_stop(m: Message, bot: Bot):
        await set_status(bot.id, m.from_user.id, "unsub")
        await m.answer("🔕 Bildirimler kapatıldı. Tekrar açmak için /start yazabilirsin.")

    @r.my_chat_member(F.chat.type == "private")
    async def on_member(ev: ChatMemberUpdated, bot: Bot):
        st = ev.new_chat_member.status
        if st == "kicked":
            await set_status(bot.id, ev.from_user.id, "blocked")
        elif st == "member":
            await set_status(bot.id, ev.from_user.id, "active")

    @r.callback_query(F.data == "unsub")
    async def on_unsub(c: CallbackQuery, bot: Bot):
        await set_status(bot.id, c.from_user.id, "unsub")
        await c.answer("Bildirimler kapatıldı. Tekrar açmak için /start yazabilirsin.", show_alert=True)

    @r.message(F.chat.type == "private", F.text)
    async def on_text(m: Message, bot: Bot):
        row = await get_bot_row(bot.id)
        responses = ((row.menu or {}).get("responses") or {}) if row else {}
        resp = responses.get(m.text)
        if resp and resp.get("action") == "promo":
            if not await send_next_promo(bot, m.chat.id):
                await m.answer("Şu an aktif bir promo yok. 🔔")
        elif resp:
            try:
                await send_stored(bot, m.chat.id, resp)
            except Exception as e:
                log.warning("Menü yanıtı gönderilemedi: %s", e)
        async with Session() as s:
            sub = await s.scalar(select(Subscriber).where(Subscriber.bot_id == bot.id, Subscriber.user_id == m.from_user.id))
            if sub:
                sub.last_seen = utcnow()
                await s.commit()

    return r
