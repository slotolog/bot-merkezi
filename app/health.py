"""Sunucu sağlık durumu (/durum) ve otomatik uyarılar."""
import asyncio
import logging
import os
import time

from sqlalchemy import func, select, text

from .db import AutoQueue, Channel, ManagedBot, Session, Subscriber, engine

log = logging.getLogger(__name__)
_STARTED = time.time()


def _meminfo() -> dict:
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                out[k] = int(v.strip().split()[0]) * 1024
    except OSError:
        pass
    return out


def _rss() -> int:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def _uptime() -> float:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except OSError:
        return 0.0


def gb(n: float) -> str:
    return f"{n / 1024**3:.1f} GB" if n >= 1024**3 else f"{n / 1024**2:.0f} MB"


def dur(sec: float) -> str:
    d, rem = divmod(int(sec), 86400)
    h, m = divmod(rem // 60, 60)
    return f"{d} gün {h} sa" if d else f"{h} sa {m} dk"


def collect() -> dict:
    cpus = os.cpu_count() or 1
    try:
        l1, l5, _ = os.getloadavg()
    except OSError:
        l1 = l5 = 0.0
    mem = _meminfo()
    total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    swap_t, swap_f = mem.get("SwapTotal", 0), mem.get("SwapFree", 0)
    st = os.statvfs("/")
    d_total, d_free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
    return {
        "cpus": cpus, "load1": l1, "load5": l5, "cpu_pct": min(100.0, l5 / cpus * 100),
        "mem_total": total, "mem_used": total - avail, "mem_pct": (total - avail) / total * 100 if total else 0,
        "swap_used": swap_t - swap_f, "swap_total": swap_t,
        "disk_total": d_total, "disk_used": d_total - d_free,
        "disk_pct": (d_total - d_free) / d_total * 100 if d_total else 0,
        "app_rss": _rss(), "host_up": _uptime(), "app_up": time.time() - _STARTED,
    }


def level(pct: float, warn=70, crit=85) -> str:
    return "🟢" if pct < warn else ("🟡" if pct < crit else "🔴")


async def status_text() -> str:
    s = collect()
    db_size = None
    try:
        async with engine.connect() as conn:
            if conn.dialect.name == "postgresql":
                db_size = (await conn.execute(text("select pg_database_size(current_database())"))).scalar()
    except Exception:
        pass
    async with Session() as ses:
        bots = await ses.scalar(select(func.count()).select_from(ManagedBot)) or 0
        subs = await ses.scalar(select(func.count()).select_from(Subscriber)) or 0
        active = await ses.scalar(select(func.count()).where(Subscriber.status == "active")) or 0
        queue = await ses.scalar(select(func.count()).select_from(AutoQueue)) or 0
        chans = await ses.scalar(select(func.count()).where(Channel.enabled.is_(True))) or 0
    levels = [level(s["cpu_pct"]), level(s["mem_pct"], 75, 90), level(s["disk_pct"], 75, 90)]
    verdict = ("🔴 Kaynak sınırına yakın! Disk/bellek temizliği veya paket yükseltmesi gerekebilir." if "🔴" in levels else
               "🟡 Kaynak kullanımı yükseliyor, takipte kal." if "🟡" in levels else
               "✅ Sistem rahat, yük düşük.")
    f = lambda n: f"{n:,}".replace(",", ".")  # noqa: E731
    lines = [
        "🖥 <b>Sistem Durumu</b>", "", verdict, "",
        f"{level(s['cpu_pct'])} <b>İşlemci:</b> %{s['cpu_pct']:.0f} (yük {s['load1']:.2f} / {s['cpus']} çekirdek)",
        f"{level(s['mem_pct'], 75, 90)} <b>Bellek:</b> {gb(s['mem_used'])} / {gb(s['mem_total'])} (%{s['mem_pct']:.0f})",
        f"   ↳ Bot sistemi: {gb(s['app_rss'])}" + (f" • Swap: {gb(s['swap_used'])} / {gb(s['swap_total'])}" if s['swap_total'] else ""),
        f"{level(s['disk_pct'], 75, 90)} <b>Disk:</b> {gb(s['disk_used'])} / {gb(s['disk_total'])} (%{s['disk_pct']:.0f})",
    ]
    if db_size is not None:
        lines.append(f"   ↳ Veritabanı: {gb(db_size)}")
    lines += ["",
              f"🤖 Bot: {f(bots)} • 👥 Kayıt: {f(subs)} (aktif {f(active)})",
              f"📡 Aktif kanal: {f(chans)} • ⏳ Bekleyen otomatik mesaj: {f(queue)}",
              "", f"⏱ Sunucu açık: {dur(s['host_up'])} • Sistem açık: {dur(s['app_up'])}"]
    return "\n".join(lines)


async def alert_loop():
    """30 dakikada bir kontrol; disk/bellek kritikse 6 saatte bir uyarır."""
    from .channels import notify_admins
    last = {}
    while True:
        await asyncio.sleep(1800)
        try:
            s = collect()
            for key, pct, limit, name in (("disk", s["disk_pct"], 85, "Disk"), ("mem", s["mem_pct"], 90, "Bellek")):
                if pct >= limit and time.time() - last.get(key, 0) > 6 * 3600:
                    last[key] = time.time()
                    await notify_admins(f"🔴 <b>{name} %{pct:.0f} dolu!</b>\nDetay için yönetici botta 🖥 Sistem durumu'na bak "
                                        "ve Claude'a haber ver.")
        except Exception:
            log.exception("Sağlık kontrolü hatası")
