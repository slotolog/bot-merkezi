"""Haberolog (@haberologbot) A/B modu entegrasyonu.

A modu: Bot sunucudaki ayrı Haberolog servisi tarafından çalıştırılır (otomatik haberler, bülten…).
        Bot Merkezi bu botu POLL ETMEZ, ama abonelerini her dakika içe aktarır;
        böylece buradan elle yayın yapılabilir.
B modu: Haberolog beklemeye geçer, bot tamamen Bot Merkezi'den yürütülür.

Haberolog'un veri klasörü konteynere /haberolog olarak bağlanır (docker-compose.yml).
İletişim dosyalarla yapılır: MODE, users.json (Haberolog → Merkez), from_merkezi.json (Merkez → Haberolog).
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
HB_DIR = Path(os.environ.get("HABEROLOG_DIR", "/haberolog"))
HB_USERNAME = os.environ.get("HABEROLOG_BOT", "haberologbot").lower().lstrip("@")
_HB_IDS: set[int] = set()  # Haberolog botunun ID'si (önbellek)


def available() -> bool:
    return HB_DIR.is_dir() and (HB_DIR / "haberolog.db").exists()


def is_hb(mb: ManagedBot | None) -> bool:
    ok = bool(mb) and (mb.username or "").lower() == HB_USERNAME and available()
    if ok:
        _HB_IDS.add(mb.id)
    return ok


def mode() -> str:
    try:
        return "B" if (HB_DIR / "MODE").read_text().strip().upper() == "B" else "A"
    except FileNotFoundError:
        return "A"


def externally_run(bot_id: int) -> bool:
    """True ise bot şu an Haberolog tarafından çalıştırılıyor; Merkez poll etmemeli, komutlarını ezmemeli."""
    return bot_id in _HB_IDS and available() and mode() == "A"


def _write(name: str, text: str):
    tmp = HB_DIR / (name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, HB_DIR / name)


async def import_users(bot_id: int) -> int:
    """Haberolog'un users.json listesini abone tablosuna ekler. Eklenen yeni abone sayısını döndürür."""
    path = HB_DIR / "users.json"
    if not path.exists():
        return 0
    try:
        users = json.loads(path.read_text())
    except Exception as e:
        log.warning("Haberolog users.json okunamadı: %s", e)
        return 0
    added = 0
    async with Session() as s:
        existing = {sub.user_id: sub for sub in
                    (await s.scalars(select(Subscriber).where(Subscriber.bot_id == bot_id))).all()}
        for u in users:
            uid = int(u["user_id"])
            blocked = bool(u.get("blocked"))
            sub = existing.get(uid)
            if sub is None:
                joined = datetime.fromtimestamp(u.get("joined_at") or 0, timezone.utc) if u.get("joined_at") else None
                s.add(Subscriber(bot_id=bot_id, user_id=uid, username=u.get("username"),
                                 first_name=u.get("first_name"), lang=u.get("lang"),
                                 source=(u.get("ref") or "haberolog")[:64],
                                 status="blocked" if blocked else "active",
                                 **({"joined_at": joined} if joined else {})))
                added += 1
            elif blocked and sub.status == "active":
                sub.status = "blocked"
            elif not blocked and sub.status == "blocked":
                sub.status = "active"
        await s.commit()
    return added


async def export_subscribers(bot_id: int) -> int:
    """B modunda gelen aboneleri Haberolog'a geri vermek için from_merkezi.json yazar."""
    async with Session() as s:
        subs = (await s.scalars(select(Subscriber).where(Subscriber.bot_id == bot_id))).all()
    data = [{"user_id": x.user_id, "username": x.username, "first_name": x.first_name, "lang": x.lang,
             "source": x.source, "status": x.status} for x in subs]
    _write("from_merkezi.json", json.dumps(data, ensure_ascii=False))
    return len(data)


async def switch(manager, bot_id: int, target: str) -> str:
    """A ⇄ B geçişi. Kullanıcıya gösterilecek özet metni döndürür."""
    async with Session() as s:
        b = await s.get(ManagedBot, bot_id)
    if not is_hb(b):
        return "❌ Bu bot için Haberolog sistemi bulunamadı."
    if target == "B":
        n = await import_users(bot_id)
        _write("MODE", "B")
        await asyncio.sleep(15)  # Haberolog'un botu bırakmasını bekle
        async with Session() as s:
            b = await s.get(ManagedBot, bot_id)
            b.enabled = True
            await s.commit()
        await manager.start_polling(b)
        return (f"✅ <b>B moduna geçildi.</b> @{b.username} artık Bot Merkezi'den yürütülüyor.\n"
                f"👥 Haberolog'dan {n} yeni abone aktarıldı.")
    await manager.stop_polling(bot_id)
    n = await export_subscribers(bot_id)
    _write("MODE", "A")
    return (f"✅ <b>A moduna geçildi.</b> Haberolog otomatik sistemi ~10 sn içinde devralır.\n"
            f"👥 {n} abone kaydı Haberolog'a iletildi.\n"
            f"📢 Bu moddayken de buradan yayın yapabilirsin.")


class HaberologSync:
    """Her dakika: A modunda aboneleri içe aktarır ve botun Merkez'de poll edilmediğinden emin olur."""

    def __init__(self, manager):
        self.manager = manager

    async def loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                await self.tick()
            except Exception as e:
                log.warning("Haberolog senkron hatası: %s", e)
            await asyncio.sleep(60)

    async def tick(self):
        if not available():
            return
        async with Session() as s:
            bots = (await s.scalars(select(ManagedBot))).all()
        for b in bots:
            if not is_hb(b):
                continue
            if mode() == "A":
                if self.manager.is_running(b.id):
                    await self.manager.stop_polling(b.id)
                    log.info("Haberolog A modunda: @%s polling durduruldu.", b.username)
                n = await import_users(b.id)
                if n:
                    log.info("Haberolog'dan %d yeni abone aktarıldı.", n)
