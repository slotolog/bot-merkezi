"""Depo kanalındaki içeriği (tek mesaj veya albüm) kullanıcıya gönderme + takip linkleri."""
import hashlib
import hmac

from aiogram import Bot

from .config import PUBLIC_URL, TRACK_SECRET
from .settings import storage_id


def content_ids(stored: dict) -> list[int]:
    """Eski kayıtlar {"msg": id}, yeniler {"msgs": [...]}."""
    return stored.get("msgs") or [stored["msg"]]


def track_sig(lid: int, bid: int, uid: int) -> str:
    raw = f"{lid}:{bid}:{uid}".encode()
    return hmac.new(TRACK_SECRET.encode(), raw, hashlib.sha256).hexdigest()[:10]


def track_url(lid: int, bid: int, uid: int) -> str:
    return f"{PUBLIC_URL}/r/{lid}/{bid}/{uid}/{track_sig(lid, bid, uid)}"


async def send_content(bot: Bot, chat_id: int, msg_ids: list[int], markup=None,
                       btn_text: str | None = None) -> list[int]:
    """Mesajı/albümü kopyalar. Albümlere Telegram buton eklemeye izin vermediği için
    butonlar hemen altına ayrı bir mesajla gider. Gönderilen mesaj ID'lerini döndürür."""
    if len(msg_ids) == 1:
        r = await bot.copy_message(chat_id=chat_id, from_chat_id=storage_id(),
                                   message_id=msg_ids[0], reply_markup=markup)
        return [r.message_id]
    res = await bot.copy_messages(chat_id=chat_id, from_chat_id=storage_id(), message_ids=msg_ids)
    ids = [x.message_id for x in res]
    if markup:
        r = await bot.send_message(chat_id, btn_text or "👇", reply_markup=markup)
        ids.append(r.message_id)
    return ids
