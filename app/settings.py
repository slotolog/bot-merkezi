"""Telegram'dan değiştirilebilen ayarlar (sunucuda .env düzenlemeye gerek kalmasın)."""
from .config import STORAGE_CHAT_ID as _ENV_STORAGE
from .db import Session, Setting

_state = {"storage": _ENV_STORAGE}


def storage_id() -> int:
    return _state["storage"]


async def load():
    async with Session() as s:
        row = await s.get(Setting, "storage_chat_id")
        if row and row.value:
            _state["storage"] = int(row.value)


async def set_storage(chat_id: int):
    async with Session() as s:
        row = await s.get(Setting, "storage_chat_id")
        if row:
            row.value = str(chat_id)
        else:
            s.add(Setting(key="storage_chat_id", value=str(chat_id)))
        await s.commit()
    _state["storage"] = chat_id
