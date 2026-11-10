"""Kanal paylaşımları: kanal tanıma, düzenli akış (saatlik vb.), anlık paylaşım."""
import asyncio
import html
import json
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy import select, update

from .config import ADMIN_IDS, TZ
from .content import send_content
from .db import Channel, ChannelLog, FeedPost, LibItem, Session, Setting
from .keyboards import build_markup
from .settings import RUNTIME, storage_id

log = logging.getLogger(__name__)

FEED_DEFAULT = {"enabled": False, "interval": 60, "start": 0, "end": 24, "mode": "same",
                "delete_prev": False, "pin": False, "idx": 0, "last_slot": ""}


# ---------- ayarlar ----------
async def get_feed() -> dict:
    async with Session() as s:
        row = await s.get(Setting, "feed")
    cfg = dict(FEED_DEFAULT)
    if row and row.value:
        try:
            cfg.update(json.loads(row.value))
        except ValueError:
            pass
    return cfg


async def save_feed(cfg: dict):
    async with Session() as s:
        row = await s.get(Setting, "feed")
        val = json.dumps(cfg)
        if row:
            row.value = val
        else:
            s.add(Setting(key="feed", value=val))
        await s.commit()


async def update_feed(**kw) -> dict:
    cfg = await get_feed()
    cfg.update(kw)
    await save_feed(cfg)
    return cfg


def hours_text(cfg) -> str:
    if cfg["start"] == 0 and cfg["end"] == 24:
        return "7/24"
    return f"{cfg['start']:02d}:00–{cfg['end'] % 24:02d}:00"


def in_hours(cfg, hour: int) -> bool:
    st, en = cfg["start"], cfg["end"]
    if st == 0 and en == 24:
        return True
    if st < en:
        return st <= hour < en
    return hour >= st or hour < en  # gece yarısını geçen aralık (ör. 09–02)


def parse_hours(text: str):
    """'7/24', 'hepsi', '09-02', '10-23' → (start, end)."""
    t = text.strip().lower().replace(" ", "").replace(".", ":").replace(":00", "")
    if t in ("7/24", "hepsi", "tümü", "tum", "0-24", "00-24"):
        return 0, 24
    if "-" not in t:
        return None
    a, b = t.split("-", 1)
    if not (a.isdigit() and b.isdigit()):
        return None
    a, b = int(a), int(b)
    if not (0 <= a <= 23 and 0 <= b <= 24) or a == b:
        return None
    return a, b


# ---------- kanal tanıma ----------
async def notify_admins(text: str):
    admin: Bot | None = RUNTIME.get("admin_bot")
    if not admin:
        return
    for uid in ADMIN_IDS:
        try:
            await admin.send_message(uid, text)
        except Exception:
            pass


async def register_channel(bot: Bot, chat, can_post: bool):
    if chat.id == storage_id():
        return
    async with Session() as s:
        ch = await s.get(Channel, chat.id)
        new = ch is None
        if new:
            ch = Channel(id=chat.id, bot_id=bot.id, title=chat.title or "", username=chat.username, enabled=can_post)
            s.add(ch)
        else:
            ch.bot_id, ch.title, ch.username = bot.id, chat.title or ch.title, chat.username
            ch.enabled = can_post
        await s.commit()
    me = await bot.me()
    if not can_post:
        await notify_admins(f"⚠️ @{html.escape(me.username)} <b>{html.escape(chat.title or '')}</b> kanalına admin "
                            "eklendi ama <b>mesaj gönderme</b> yetkisi kapalı. Yetkiyi açınca kanal otomatik aktif olur.")
    elif new:
        await notify_admins(f"✅ Yeni kanal eklendi: <b>{html.escape(chat.title or '')}</b>\n"
                            f"Paylaşımı @{html.escape(me.username)} yapacak. 📢 Kanal Paylaşımları menüsünden yönetebilirsin.")


async def unregister_channel(bot: Bot, chat):
    async with Session() as s:
        ch = await s.get(Channel, chat.id)
        if ch and ch.bot_id == bot.id:
            ch.enabled = False
            await s.commit()
            await notify_admins(f"⚠️ Bot <b>{html.escape(chat.title or '')}</b> kanalından çıkarıldı, "
                                "bu kanala paylaşım durduruldu.")


# ---------- paylaşım ----------
async def feed_posts(only_enabled=True) -> list[tuple[FeedPost, LibItem]]:
    async with Session() as s:
        q = select(FeedPost, LibItem).join(LibItem, LibItem.id == FeedPost.lib_id)
        if only_enabled:
            q = q.where(FeedPost.enabled.is_(True), LibItem.enabled.is_(True))
        return list((await s.execute(q.order_by(FeedPost.position, FeedPost.id))).all())


async def channels(only_enabled=True) -> list[Channel]:
    async with Session() as s:
        q = select(Channel)
        if only_enabled:
            q = q.where(Channel.enabled.is_(True))
        return list((await s.scalars(q.order_by(Channel.added_at))).all())


async def post_to_channel(manager, ch: Channel, lib: LibItem, cfg: dict, source: str) -> tuple[bool, str]:
    bot = manager.sender(ch.bot_id)
    if not bot:
        err = "Paylaşım botu bağlı değil"
    else:
        err = ""
        prev = None
        if cfg.get("delete_prev"):
            async with Session() as s:
                prev = await s.scalar(select(ChannelLog).where(
                    ChannelLog.channel_id == ch.id, ChannelLog.ok.is_(True), ChannelLog.deleted.is_(False))
                    .order_by(ChannelLog.id.desc()).limit(1))
        for attempt in range(3):
            try:
                ids = await send_content(bot, ch.id, lib.msgs, build_markup(lib.buttons), lib.btn_text)
                if cfg.get("pin") and ids:
                    try:
                        await bot.pin_chat_message(ch.id, ids[0], disable_notification=True)
                    except Exception as e:
                        log.warning("Sabitlenemedi: %s", e)
                if prev and prev.message_ids:
                    for mid in prev.message_ids:
                        try:
                            await bot.delete_message(ch.id, mid)
                        except Exception:
                            pass
                    async with Session() as s:
                        await s.execute(update(ChannelLog).where(ChannelLog.id == prev.id).values(deleted=True))
                        await s.commit()
                async with Session() as s:
                    s.add(ChannelLog(channel_id=ch.id, lib_id=lib.id, message_ids=ids, ok=True, source=source))
                    await s.commit()
                return True, ""
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Exception as e:
                err = str(e)[:250]
                break
    async with Session() as s:
        s.add(ChannelLog(channel_id=ch.id, lib_id=lib.id, ok=False, error=err or "bilinmeyen hata", source=source))
        await s.commit()
    return False, err


async def run_feed_once(manager, advance=True) -> str:
    """Sıradaki postu/postları kanallara gönderir; özet metni döndürür."""
    cfg = await get_feed()
    posts = await feed_posts()
    chans = await channels()
    if not posts:
        return "Akışta aktif post yok."
    if not chans:
        return "Aktif kanal yok."
    n, idx = len(posts), cfg["idx"]
    ok = fail = 0
    errors = []
    sent_posts = set()
    for i, ch in enumerate(chans):
        fp, lib = posts[(idx + (i if cfg["mode"] == "spread" else 0)) % n]
        good, err = await post_to_channel(manager, ch, lib, cfg, "feed")
        if good:
            ok += 1
            sent_posts.add(fp.id)
        else:
            fail += 1
            errors.append(f"{ch.title}: {err}")
        await asyncio.sleep(0.5)
    if sent_posts:
        async with Session() as s:
            for pid in sent_posts:
                await s.execute(update(FeedPost).where(FeedPost.id == pid).values(count=FeedPost.count + 1))
            await s.commit()
    if advance:
        await update_feed(idx=(idx + 1) % n)
    if errors:
        await notify_admins("⚠️ Kanal paylaşımında hata:\n" + "\n".join(html.escape(e) for e in errors[:5]))
    return f"{ok} kanala paylaşıldı" + (f", {fail} hata" if fail else "")


class FeedRunner:
    def __init__(self, manager):
        self.manager = manager

    async def loop(self):
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Kanal akışı hatası")
            await asyncio.sleep(20)

    async def tick(self, now: datetime | None = None):
        cfg = await get_feed()
        if not cfg["enabled"]:
            return
        now = now or datetime.now(TZ)
        minutes = now.hour * 60 + now.minute
        interval = max(15, int(cfg["interval"]))
        slot = f"{now:%Y-%m-%d}-{minutes // interval}"
        if slot == cfg["last_slot"] or not in_hours(cfg, now.hour):
            return
        await update_feed(last_slot=slot)
        result = await run_feed_once(self.manager)
        log.info("Akış paylaşımı: %s", result)
