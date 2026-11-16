"""Haftalık program: belirlenen gün/saatlerde abonelere (zinciri bitmiş olanlara) otomatik gönderim."""
import asyncio
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import select

from .config import ADMIN_IDS, TZ
from .db import Broadcast, LibItem, Link, ManagedBot, Schedule, Session

log = logging.getLogger(__name__)

DAYS = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
DAYS_LONG = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
CATCHUP = timedelta(minutes=30)  # sunucu kısa süre kapalı kaldıysa en fazla 30 dk geç gönderilir


def parse_times(text: str) -> list[tuple[int, int]] | None:
    """'12:00, 15:30 21' → [(12,0),(15,30),(21,0)]"""
    out = []
    for part in re.split(r"[,\s;]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?", part)
        if not m:
            return None
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            return None
        out.append((h, mi))
    return sorted(set(out)) or None


async def create_program_broadcast(bot_id: int, lib: LibItem) -> int:
    async with Session() as s:
        bc = Broadcast(created_by=min(ADMIN_IDS) if ADMIN_IDS else 0, msg=lib.msgs[0], msg_ids=lib.msgs,
                       btn_text=lib.btn_text, buttons=None, unsub_btn=True, bot_ids=[bot_id],
                       status="queued", source="program")
        s.add(bc)
        await s.flush()
        rows = []
        for row in lib.buttons or []:
            new_row = []
            for btn in row:
                link = Link(broadcast_id=bc.id, text=btn["text"][:128], url=btn["url"])
                s.add(link)
                await s.flush()
                new_row.append({**btn, "lid": link.id})
            rows.append(new_row)
        bc.buttons = rows or None
        await s.commit()
        return bc.id


class ScheduleRunner:
    def __init__(self, runner):
        self.runner = runner  # BroadcastRunner

    async def loop(self):
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Program hatası")
            await asyncio.sleep(30)

    async def tick(self, now: datetime | None = None):
        now = now or datetime.now(TZ)
        today = now.strftime("%Y-%m-%d")
        async with Session() as s:
            slots = (await s.scalars(select(Schedule).where(
                Schedule.enabled.is_(True), Schedule.weekday == now.weekday()))).all()
        for sl in slots:
            at = now.replace(hour=sl.hour, minute=sl.minute, second=0, microsecond=0)
            if sl.last_run == today or not (at <= now < at + CATCHUP):
                continue
            async with Session() as s:
                row = await s.get(Schedule, sl.id)
                row.last_run = today
                b = await s.get(ManagedBot, sl.bot_id)
                lib = await s.get(LibItem, sl.lib_id)
                ok = bool(b and b.enabled and b.schedule_enabled is not False and lib and lib.enabled)
                if ok:
                    row.count += 1
                await s.commit()
            if not ok:
                continue
            bc_id = await create_program_broadcast(sl.bot_id, lib)
            log.info("Program: @%s %s %02d:%02d → %s (yayın #%s)", b.username, DAYS[sl.weekday], sl.hour,
                     sl.minute, lib.title, bc_id)
            self.runner.start(bc_id)
