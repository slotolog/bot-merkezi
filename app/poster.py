"""Slotolog Post Oluşturucu: tek kanala (Slotolog) görsel + başlık + açılır detay + buton paylaşımı.

Post yapısı (Telegram'daki "promosyon kartı" görünümü):
    <başlık>                      ← görselin üstünde (show_caption_above_media)
    ▸ DETAYLAR (açılır alıntı)    ← expandable blockquote, tıklayınca açılır
    [görsel]
    [buton] [buton]
    [buton]
Butonlarda {giris} yazılırsa o anki "güncel giriş" linki kullanılır; link değişince eski
postlardaki butonlar tek tuşla güncellenir.
"""
import asyncio
import copy
import html
import logging
import re
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import LinkPreviewOptions
from sqlalchemy import select

from .db import Channel, Session, Setting, SlotPost, utcnow
from .keyboards import build_markup, buttons_to_text, parse_buttons
from .settings import storage_id

log = logging.getLogger(__name__)

GIRIS = "https://giris.slotolog.invalid/"   # butonlarda {giris} yerine saklanan işaret
DEFAULT_LABEL = "📋 DETAYLAR"
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


# ---------- ayarlar ----------
async def get_setting(key: str, default: str = "") -> str:
    async with Session() as s:
        row = await s.get(Setting, key)
    return row.value if row and row.value is not None else default


async def set_setting(key: str, value: str):
    async with Session() as s:
        row = await s.get(Setting, key)
        if row:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))
        await s.commit()


async def poster_channel_id() -> int:
    v = await get_setting("sp_channel")
    return int(v) if v else 0


async def giris_url() -> str:
    return await get_setting("sp_giris")


async def details_label() -> str:
    v = await get_setting("sp_label", DEFAULT_LABEL)
    return "" if v == "-" else v


# ---------- butonlar ----------
def parse_post_buttons(text: str) -> list[list[dict]]:
    return parse_buttons(re.sub(r"\{\s*giri[sş]\s*\}", GIRIS, text, flags=re.I))


def post_buttons_text(rows) -> str:
    return buttons_to_text(rows).replace(GIRIS, "{giris}")


def uses_giris(rows) -> bool:
    return any(b.get("url") == GIRIS for row in (rows or []) for b in row)


def resolve_buttons(rows, giris: str):
    if not rows:
        return None
    if uses_giris(rows) and not giris:
        raise ValueError("Butonlarda {giris} var ama güncel giriş linki ayarlı değil (🔗 Giriş linki).")
    out = copy.deepcopy(rows)
    for row in out:
        for b in row:
            if b.get("url") == GIRIS:
                b["url"] = giris
            b.pop("lid", None)
    return out


# ---------- metin ----------
def render(title: str, details: str | None, label: str) -> str:
    out = title or ""
    if details:
        head = f"<b>{html.escape(label)}</b>\n" if label else ""
        out += f"\n\n<blockquote expandable>{head}{details}</blockquote>"
    return out.strip()


def visible_len(html_text: str) -> int:
    """Telegram'ın saydığı uzunluk (UTF-16, etiketsiz)."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", html_text))
    return len(plain.encode("utf-16-le")) // 2


def check_length(p: dict, label: str) -> str | None:
    n = visible_len(render(p.get("title", ""), p.get("details"), label))
    lim = CAPTION_LIMIT if p.get("media_msg") else TEXT_LIMIT
    if n > lim:
        return (f"⚠️ Yazı çok uzun: {n}/{lim} karakter. "
                + ("Görselli postlarda Telegram sınırı 1024 karakter; detayı kısalt." if p.get("media_msg") else ""))
    return None


# ---------- gönderim ----------
async def deliver(bot: Bot, chat_id: int, p: dict, label: str, giris: str, silent: bool = False) -> int:
    """Postu gönderir, mesaj ID'sini döndürür. p: title, details, media_msg, buttons, above."""
    markup = build_markup(resolve_buttons(p.get("buttons"), giris))
    text = render(p.get("title", ""), p.get("details"), label)
    if p.get("media_msg"):
        r = await bot.copy_message(chat_id=chat_id, from_chat_id=storage_id(), message_id=p["media_msg"],
                                   caption=text, parse_mode="HTML",
                                   show_caption_above_media=bool(p.get("above", True)),
                                   reply_markup=markup, disable_notification=silent)
    else:
        r = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup,
                                   disable_notification=silent,
                                   link_preview_options=LinkPreviewOptions(is_disabled=True))
    return r.message_id


def post_dict(sp: SlotPost) -> dict:
    return {"title": sp.title, "details": sp.details, "media_msg": sp.media_msg, "media_kind": sp.media_kind,
            "buttons": sp.buttons, "above": sp.above is not False, "pin": bool(sp.pin), "silent": bool(sp.silent)}


async def channel_bot(manager, channel_id: int) -> tuple[Channel | None, Bot | None]:
    async with Session() as s:
        ch = await s.get(Channel, channel_id)
    return ch, (manager.sender(ch.bot_id) if ch else None)


async def publish(manager, post_id: int) -> tuple[bool, str]:
    async with Session() as s:
        sp = await s.get(SlotPost, post_id)
        if not sp or sp.status not in ("scheduled", "sending"):
            return False, "Post bulunamadı veya zaten işlendi."
        sp.status = "sending"
        await s.commit()
        p = post_dict(sp)
        channel_id = sp.channel_id
    ch, bot = await channel_bot(manager, channel_id)
    err, mid = "", None
    if not bot:
        err = "Kanalın paylaşım botu bağlı değil."
    else:
        label, giris = await details_label(), await giris_url()
        for _ in range(3):
            try:
                mid = await deliver(bot, channel_id, p, label, giris, p["silent"])
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Exception as e:
                err = str(e)[:250]
                if "chat not found" in err.lower():
                    from .channels import explain_not_found
                    err = await explain_not_found(bot, channel_id)
                break
        if mid and p["pin"]:
            try:
                await bot.pin_chat_message(channel_id, mid, disable_notification=True)
            except Exception as e:
                log.warning("Slotolog postu sabitlenemedi: %s", e)
    async with Session() as s:
        sp = await s.get(SlotPost, post_id)
        if mid:
            sp.status, sp.message_id, sp.sent_at, sp.error = "sent", mid, utcnow(), None
        else:
            sp.status, sp.error = "failed", (err or "bilinmeyen hata")[:256]
        await s.commit()
    return bool(mid), err


async def refresh_buttons(manager, post_ids: list[int] | None = None) -> tuple[int, int]:
    """Gönderilmiş postların butonlarını güncel giriş linkiyle yeniden yazar. (başarılı, hata)"""
    giris, ok, fail = await giris_url(), 0, 0
    async with Session() as s:
        q = select(SlotPost).where(SlotPost.status == "sent", SlotPost.message_id.is_not(None))
        if post_ids:
            q = q.where(SlotPost.id.in_(post_ids))
        posts = [p for p in (await s.scalars(q)).all() if post_ids or uses_giris(p.buttons)]
    for sp in posts:
        _, bot = await channel_bot(manager, sp.channel_id)
        if not bot:
            fail += 1
            continue
        try:
            await bot.edit_message_reply_markup(chat_id=sp.channel_id, message_id=sp.message_id,
                                                reply_markup=build_markup(resolve_buttons(sp.buttons, giris)))
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            fail += 1
        except Exception as e:
            if "not modified" in str(e).lower():
                ok += 1
            else:
                fail += 1
                log.warning("Buton güncellenemedi #%s: %s", sp.id, e)
        await asyncio.sleep(0.15)
    return ok, fail


async def giris_post_count() -> int:
    async with Session() as s:
        posts = (await s.scalars(select(SlotPost).where(SlotPost.status == "sent"))).all()
    return sum(1 for p in posts if uses_giris(p.buttons))


class PosterRunner:
    """Zamanlanmış Slotolog postlarını vakti gelince paylaşır."""

    def __init__(self, manager):
        self.manager = manager

    async def loop(self):
        # yarıda kalmış gönderimleri tekrar sıraya al
        async with Session() as s:
            for sp in (await s.scalars(select(SlotPost).where(SlotPost.status == "sending"))).all():
                sp.status = "scheduled"
            await s.commit()
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Slotolog post zamanlayıcı hatası")
            await asyncio.sleep(20)

    async def tick(self, now: datetime | None = None):
        now = now or utcnow()
        async with Session() as s:
            due = (await s.scalars(select(SlotPost.id).where(
                SlotPost.status == "scheduled", SlotPost.scheduled_at <= now).order_by(SlotPost.scheduled_at))).all()
        for pid in due:
            ok, err = await publish(self.manager, pid)
            from .channels import notify_admins
            await notify_admins(f"🎰 Zamanlanmış Slotolog postu #{pid} "
                                + ("paylaşıldı ✅" if ok else f"paylaşılamadı ❌\n{html.escape(err)}"))
