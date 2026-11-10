"""Harici (kendi sistemi olan) botlar için A/B modu — Haberolog, Seyahat Rehberi ve sonrakiler.

A modu: Bot sunucudaki kendi servisi tarafından çalıştırılır (otomatik içerik, bülten…).
        Merkez bu botu POLL ETMEZ, komutlarını ezmez; abonelerini her dakika içe aktarır,
        böylece buradan elle yayın yapılabilir.
B modu: Botun kendi servisi beklemeye geçer, bot tamamen Merkez'den yürütülür.

Her harici botun veri klasörü konteynere bağlanır (docker-compose.yml) ve EXTERNAL_DIRS ile listelenir.
Bot kendini klasördeki BOT_ID dosyasıyla tanıtır. İletişim dosyaları: MODE, users.json, from_merkezi.json.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from .db import ManagedBot, Session, Subscriber

log = logging.getLogger(__name__)
EXT_DIRS = [Path(p.strip()) for p in os.environ.get("EXTERNAL_DIRS", "/haberolog,/seyahat").split(",") if p.strip()]
LEGACY = {"haberolog": "haberologbot"}  # BOT_ID dosyası yazmayan eski sürümler için
_DIR_BY_ID: dict[int, Path] = {}


def _ident(d: Path):
    """(bot_id, username) — klasördeki BOT_ID dosyasından."""
    try:
        parts = (d / "BOT_ID").read_text().split()
        return int(parts[0]), (parts[1] if len(parts) > 1 else "").lower()
    except (FileNotFoundError, ValueError, IndexError):
        return None, None


def ext_dir(mb: ManagedBot | None) -> Path | None:
    """Bu bot harici bir sisteme bağlı mı? Bağlıysa veri klasörünü döndürür."""
    if not mb:
        return None
    for d in EXT_DIRS:
        if not d.is_dir():
            continue
        bid, uname = _ident(d)
        legacy = LEGACY.get(d.name) == (mb.username or "").lower() and any(d.glob("*.db"))
        if bid == mb.id or (bid is None and legacy):
            _DIR_BY_ID[mb.id] = d
            return d
    return None


def label(d: Path) -> str:
    return {"haberolog": "Haberolog", "seyahat": "Seyahat Rehberi"}.get(d.name, d.name.capitalize())


def mode(d: Path) -> str:
    try:
        return "B" if (d / "MODE").read_text().strip().upper() == "B" else "A"
    except FileNotFoundError:
        return "A"


def externally_run(bot_id: int) -> bool:
    """True ise bot şu an kendi servisi tarafından çalıştırılıyor; Merkez poll etmemeli, komutlarını ezmemeli."""
    d = _DIR_BY_ID.get(bot_id)
    return bool(d) and d.is_dir() and mode(d) == "A"


def _write(d: Path, name: str, text: str):
    tmp = d / (name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, d / name)


async def import_users(d: Path, bot_id: int) -> int:
    path = d / "users.json"
    if not path.exists():
        return 0
    try:
        users = json.loads(path.read_text())
    except Exception as e:
        log.warning("%s users.json okunamadı: %s", d, e)
        return 0
    added = 0
    async with Session() as s:
        existing = {x.user_id: x for x in
                    (await s.scalars(select(Subscriber).where(Subscriber.bot_id == bot_id))).all()}
        for u in users:
            uid = int(u["user_id"])
            blocked = bool(u.get("blocked"))
            sub = existing.get(uid)
            if sub is None:
                extra = {}
                if u.get("joined_at"):
                    extra["joined_at"] = datetime.fromtimestamp(u["joined_at"], timezone.utc)
                s.add(Subscriber(bot_id=bot_id, user_id=uid, username=u.get("username"),
                                 first_name=u.get("first_name"), lang=(u.get("lang") or None),
                                 source=(u.get("ref") or d.name)[:64],
                                 status="blocked" if blocked else "active", **extra))
                added += 1
            elif blocked and sub.status == "active":
                sub.status = "blocked"
            elif not blocked and sub.status == "blocked":
                sub.status = "active"
        await s.commit()
    return added


async def export_subscribers(d: Path, bot_id: int) -> int:
    async with Session() as s:
        subs = (await s.scalars(select(Subscriber).where(Subscriber.bot_id == bot_id))).all()
    data = [{"user_id": x.user_id, "username": x.username, "first_name": x.first_name, "lang": x.lang,
             "source": x.source, "status": x.status} for x in subs]
    _write(d, "from_merkezi.json", json.dumps(data, ensure_ascii=False))
    return len(data)


async def switch(manager, bot_id: int, target: str) -> str:
    async with Session() as s:
        b = await s.get(ManagedBot, bot_id)
    d = ext_dir(b)
    if not d:
        return "❌ Bu bot için harici sistem bulunamadı."
    name = label(d)
    if target == "B":
        n = await import_users(d, bot_id)
        _write(d, "MODE", "B")
        await asyncio.sleep(15)  # botun kendi servisinin bırakmasını bekle
        async with Session() as s:
            b = await s.get(ManagedBot, bot_id)
            b.enabled = True
            await s.commit()
        await manager.start_polling(b)
        return (f"✅ <b>B moduna geçildi.</b> @{b.username} artık Bot Merkezi'den yürütülüyor.\n"
                f"👥 {name} sisteminden {n} yeni abone aktarıldı.")
    await manager.stop_polling(bot_id)
    n = await export_subscribers(d, bot_id)
    _write(d, "MODE", "A")
    return (f"✅ <b>A moduna geçildi.</b> {name} otomatik sistemi ~10 sn içinde devralır.\n"
            f"👥 {n} abone kaydı {name} sistemine iletildi.\n📢 Bu moddayken de buradan yayın yapabilirsin.")


class ExternalSync:
    """Her dakika: A modundaki harici botların abonelerini içe aktarır ve Merkez'de poll edilmediğinden emin olur."""

    def __init__(self, manager):
        self.manager = manager

    async def loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                await self.tick()
            except Exception as e:
                log.warning("Harici bot senkron hatası: %s", e)
            await asyncio.sleep(60)

    async def tick(self):
        async with Session() as s:
            bots = (await s.scalars(select(ManagedBot))).all()
        for b in bots:
            d = ext_dir(b)
            if not d or mode(d) != "A":
                continue
            if self.manager.is_running(b.id):
                await self.manager.stop_polling(b.id)
                log.info("@%s A modunda: polling kendi servisinde, Merkez durdurdu.", b.username)
            n = await import_users(d, b.id)
            if n:
                log.info("@%s: %d yeni abone aktarıldı.", b.username, n)
