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
from aiogram.types import InputRichMessage, LinkPreviewOptions
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
    if p.get("rich"):
        return None
    n = visible_len(render(p.get("title", ""), p.get("details"), label))
    lim = CAPTION_LIMIT if p.get("media_msg") else TEXT_LIMIT
    if n > lim:
        return (f"⚠️ Yazı çok uzun: {n}/{lim} karakter. "
                + ("Görselli postlarda Telegram sınırı 1024 karakter; detayı kısalt." if p.get("media_msg") else ""))
    return None


# ---------- zengin mesaj (tablo, başlık, liste…) ----------
MEDIA_KINDS = ("photo", "animation", "video")
_file_cache: dict[tuple[int, int], str] = {}


async def store_media(admin_bot: Bot, kind: str, file_id: str, spoiler: bool = False) -> int:
    """Gelen dosyayı depo kanalına koyar (her bot oradan kendi file_id'sini alır)."""
    send = {"photo": admin_bot.send_photo, "animation": admin_bot.send_animation, "video": admin_bot.send_video}[kind]
    r = await send(storage_id(), file_id, has_spoiler=spoiler or None)
    return r.message_id


def _recv_file_id(kind: str, obj) -> str:
    if kind == "photo":
        return (obj[-1] if isinstance(obj, list) else obj)["file_id"]
    return obj["file_id"]


async def bot_file_id(bot: Bot, store_msg: int, kind: str) -> str:
    """Depodaki dosyanın bu bota ait file_id'si (iletip hemen siler, önbelleğe alır)."""
    key = (bot.id, store_msg)
    if key not in _file_cache:
        fwd = await bot.forward_message(storage_id(), storage_id(), store_msg, disable_notification=True)
        if kind == "photo":
            fid = fwd.photo[-1].file_id
        else:
            fid = (getattr(fwd, kind, None) or fwd.animation or fwd.video or fwd.document).file_id
        _file_cache[key] = fid
        try:
            await bot.delete_message(storage_id(), fwd.message_id)
        except Exception:
            pass
    return _file_cache[key]


def rich_plain(node) -> str:
    """Zengin metinden düz yazı (liste görünümü için)."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(rich_plain(x) for x in node)
    if isinstance(node, dict):
        if node.get("type") == "custom_emoji":
            return node.get("alternative_text", "")
        for k in ("text", "blocks", "cells"):
            if k in node and node[k]:
                return rich_plain(node[k])
    return ""


async def rich_from_message(admin_bot: Bot, rm) -> dict:
    """Gelen RichMessage → saklanabilir blok listesi. İçindeki görseller depoya alınır."""
    data = [b.model_dump(mode="json", exclude_none=True) for b in rm.blocks]

    async def walk(node):
        if isinstance(node, list):
            return [await walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        kind = node.get("type")
        if kind in MEDIA_KINDS and kind in node and not isinstance(node[kind], str):
            msg = await store_media(admin_bot, kind, _recv_file_id(kind, node[kind]), bool(node.get("has_spoiler")))
            out = {"type": kind, "_store": msg}
            if node.get("caption"):
                out["caption"] = node["caption"]
            if node.get("has_spoiler"):
                out["has_spoiler"] = True
            return out
        out = {k: await walk(v) for k, v in node.items()}
        if "blocks" in out and "label" in out:  # liste öğesi: label sadece gelen mesajda var
            out.pop("label")
        return out

    blocks = await walk(data)
    summary = ""
    for b in blocks:
        summary = rich_plain(b).strip()
        if summary:
            break
    return {"blocks": blocks, "summary": summary[:120]}


def rich_has_media(rich: dict | None) -> bool:
    return "_store" in str(rich or "")


def html_to_rich(text: str):
    """Basit HTML (b, i, u, s, a, tg-emoji) → RichText. Başlığı zengin mesajın başına koymak için."""
    tag_re = re.compile(r'<(/?)(b|strong|i|em|u|ins|s|strike|del|a|tg-emoji)((?:\s[^>]*)?)>', re.I)
    kinds = {"b": "bold", "strong": "bold", "i": "italic", "em": "italic", "u": "underline", "ins": "underline",
             "s": "strikethrough", "strike": "strikethrough", "del": "strikethrough"}
    root: list = []
    stack: list[tuple[str, dict, list]] = []
    cur = root
    pos = 0
    for m in tag_re.finditer(text):
        plain = html.unescape(re.sub(r"<[^>]+>", "", text[pos:m.start()]))
        if plain:
            cur.append(plain)
        pos = m.end()
        closing, tag, attrs = m.group(1), m.group(2).lower(), m.group(3) or ""
        if not closing:
            node = {"tag": tag, "attrs": attrs}
            stack.append((tag, node, cur))
            cur = []
            node["children"] = cur
        elif stack:
            tag0, node, parent = stack.pop()
            children = node["children"]
            if tag0 == "tg-emoji":
                eid = re.search(r'emoji-id="(\d+)"', node["attrs"])
                alt = rich_plain(children) or "⭐"
                parent.append({"type": "custom_emoji", "custom_emoji_id": eid.group(1), "alternative_text": alt}
                              if eid else alt)
            elif tag0 == "a":
                href = re.search(r'href="([^"]+)"', node["attrs"])
                parent.append({"type": "url", "text": children, "url": html.unescape(href.group(1))}
                              if href else children)
            else:
                parent.append({"type": kinds[tag0], "text": children})
            cur = parent
    tail = html.unescape(re.sub(r"<[^>]+>", "", text[pos:]))
    if tail:
        cur.append(tail)
    while stack:  # kapanmamış etiket: içeriği düz ekle
        _, node, parent = stack.pop()
        parent.extend(node["children"])
    return root[0] if len(root) == 1 else root


async def build_rich(bot: Bot, p: dict) -> InputRichMessage:
    async def resolve(node):
        if isinstance(node, list):
            return [await resolve(x) for x in node]
        if not isinstance(node, dict):
            return node
        if "_store" in node:
            kind = node["type"]
            media = {"type": kind, "media": await bot_file_id(bot, node["_store"], kind)}
            if node.get("has_spoiler"):
                media["has_spoiler"] = True
            out = {"type": kind, kind: media}
            if node.get("caption"):
                out["caption"] = node["caption"]
            return out
        return {k: await resolve(v) for k, v in node.items()}

    blocks = []
    if p.get("title"):
        blocks.append({"type": "paragraph", "text": html_to_rich(p["title"])})
    blocks += await resolve(copy.deepcopy(p["rich"]["blocks"]))
    if p.get("media_msg"):
        kind = p.get("media_kind") or "photo"
        if kind not in MEDIA_KINDS:
            raise ValueError("Tablolu postta görsel foto, video veya GIF olmalı (dosya olarak gönderme).")
        mb = await resolve({"type": kind, "_store": p["media_msg"]})
        if p.get("above", True):
            blocks.append(mb)
        else:
            blocks.insert(0, mb)
    return InputRichMessage.model_validate({"blocks": blocks})


# ---------- gönderim ----------
async def deliver(bot: Bot, chat_id: int, p: dict, label: str, giris: str, silent: bool = False) -> int:
    """Postu gönderir, mesaj ID'sini döndürür. p: title, details, media_msg, buttons, above."""
    markup = build_markup(resolve_buttons(p.get("buttons"), giris))
    if p.get("rich"):
        r = await bot.send_rich_message(chat_id, await build_rich(bot, p), reply_markup=markup,
                                        disable_notification=silent)
        return r.message_id
    text = render(p.get("title", ""), p.get("details"), label)
    if p.get("media_msg"):
        above = bool(p.get("above", True)) if p.get("media_kind") in MEDIA_KINDS else None
        r = await bot.copy_message(chat_id=chat_id, from_chat_id=storage_id(), message_id=p["media_msg"],
                                   caption=text, parse_mode="HTML",
                                   show_caption_above_media=above,
                                   reply_markup=markup, disable_notification=silent)
    else:
        r = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup,
                                   disable_notification=silent,
                                   link_preview_options=LinkPreviewOptions(is_disabled=True))
    return r.message_id


def post_dict(sp: SlotPost) -> dict:
    return {"title": sp.title, "details": sp.details, "rich": sp.rich, "media_msg": sp.media_msg, "media_kind": sp.media_kind,
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
