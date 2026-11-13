"""Bot teşhisi: sessiz polling hatalarını yakalar, anlaşılır açıklar ve uyarır."""
import asyncio
import html
import logging
import re
import time

from sqlalchemy import func, select

from .db import ManagedBot, Session, Subscriber, aware
from .config import TZ

log = logging.getLogger(__name__)

# bot_id -> {"error": str, "since": ts, "last": ts, "count": int}
POLL_ERRORS: dict[int, dict] = {}


class _PollingLogHandler(logging.Handler):
    """aiogram 'Failed to fetch updates' hatalarını bot bazında kaydeder."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self._last = ""

    def emit(self, record):
        try:
            msg = record.getMessage()
            if msg.startswith("Failed to fetch updates"):
                self._last = msg.replace("Failed to fetch updates - ", "", 1)
                return
            m = re.search(r"bot id = (\d+)", msg)
            if not m:
                return
            bid = int(m.group(1))
            if msg.startswith("Sleep for"):
                e = POLL_ERRORS.get(bid) or {"since": time.time(), "count": 0}
                e.update(error=self._last, last=time.time(), count=e["count"] + 1)
                POLL_ERRORS[bid] = e
            elif msg.startswith("Connection established"):
                POLL_ERRORS.pop(bid, None)
        except Exception:
            pass


def install():
    logging.getLogger("aiogram.dispatcher").addHandler(_PollingLogHandler())


def explain(err: str) -> str:
    e = err.lower()
    if "webhook is active" in e or "webhook" in e and "conflict" in e:
        return ("Bu botta başka bir sistemin <b>webhook</b>'u ayarlı (eski bot kurucu / başka sunucu). "
                "🩺 Teşhis ekranındaki <b>🔧 Webhook'u kaldır</b> ile düzeltilir.")
    if "terminated by other getupdates" in e or "conflict" in e:
        return ("Bu bot şu an <b>başka bir yerde de çalışıyor</b> (eski sunucu, bilgisayarında açık bir program, "
                "başka bir panel). Orayı kapatmadan bu sistem botu dinleyemez.")
    if "unauthorized" in e or "not found" in e and "token" in e:
        return ("Bot <b>token'ı geçersiz</b> (BotFather'da token yenilenmiş veya bot silinmiş). "
                "Yeni token'ı ➕ Bot Ekle ile gönder.")
    if "network" in e or "timeout" in e or "connect" in e:
        return "Telegram'a bağlantı sorunu (genelde kendiliğinden düzelir)."
    return "Beklenmeyen hata: " + html.escape(err[:200])


async def diagnose(manager, bot_id: int) -> tuple[str, bool]:
    """(rapor metni, webhook_var_mı)"""
    async with Session() as s:
        b = await s.get(ManagedBot, bot_id)
        last_seen = await s.scalar(select(func.max(Subscriber.last_seen)).where(Subscriber.bot_id == bot_id))
    if not b:
        return "Bot bulunamadı.", False
    lines = [f"🩺 <b>@{html.escape(b.username)} — Teşhis</b>", ""]
    ok = True
    if not b.enabled:
        lines.append("⏸ Bot panelden <b>durdurulmuş</b>. ▶️ Başlat ile aç.")
        ok = False
    from .external import ext_dir, label, mode as ext_mode
    xd = ext_dir(b)
    external_a = bool(xd) and ext_mode(xd) == "A"
    if external_a:
        lines.append(f"🅰️ A modu: botu <b>{label(xd)} servisi</b> çalıştırıyor, Merkez bilerek dinlemiyor.")
    else:
        running = manager.is_healthy(bot_id)
        lines.append(f"{'🟢' if running else '🔴'} Dinleme (Merkez): "
                     f"{'çalışıyor' if running else 'çalışmıyor → 🔄 Yeniden başlat'}")
        if bot_id in manager.crashes:
            lines.append(f"   ↳ Son durma sebebi: <code>{html.escape(manager.crashes[bot_id][:200])}</code>")
        ok &= running
    bot = manager.sender(bot_id)
    has_webhook = False
    if bot:
        try:
            me = await bot.get_me()
            lines.append(f"🟢 Token geçerli (@{html.escape(me.username)})")
        except Exception as e:
            lines.append(f"🔴 Token: {explain(str(e))}")
            ok = False
        try:
            wh = await bot.get_webhook_info()
            if wh.url:
                has_webhook = True
                ok = False
                lines.append(f"🔴 Webhook ayarlı: <code>{html.escape(wh.url[:60])}</code>\n"
                             "   Başka bir sistem bu botu çalıştırıyor/çalıştırmış. Kaldırınca bu sistem dinlemeye başlar.")
            if wh.pending_update_count:
                lines.append(f"⏳ Bekleyen güncelleme: {wh.pending_update_count}")
                if external_a and not wh.url:
                    ok = False
                    lines.append(f"🔴 <b>{label(xd)} servisi botu dinlemiyor</b> (mesajlar okunmadan bekliyor).\n"
                                 "   Hızlı çözüm: bot panelinde <b>🔀 Mod → B</b> ile botu Merkez'e devret.\n"
                                 f"   Kalıcı çözüm: sunucuda {label(xd)} servisinin yeniden başlatılması.")
        except Exception:
            pass
    err = POLL_ERRORS.get(bot_id)
    if err and time.time() - err["last"] < 600:
        ok = False
        lines.append(f"🔴 Son hata ({err['count']} kez): {explain(err['error'])}")
    if last_seen:
        lines.append(f"👤 Son kullanıcı etkileşimi: {aware(last_seen).astimezone(TZ):%d.%m %H:%M}")
    lines += ["", "✅ Sorun görünmüyor. Bota /start yazıp dene." if ok else "⚠️ Yukarıdaki 🔴 maddeyi düzelt."]
    return "\n".join(lines), has_webhook


_wd_alerted: dict[int, float] = {}


async def watchdog(manager, notify):
    """Merkez'de çalışması gereken ama dinlemesi durmuş botları yeniden başlatır (A modu hariç)."""
    from .external import ext_dir, mode as ext_mode
    async with Session() as s:
        bots = (await s.scalars(select(ManagedBot).where(ManagedBot.enabled.is_(True)))).all()
    for b in bots:
        xd = ext_dir(b)
        if (xd and ext_mode(xd) == "A") or manager.is_healthy(b.id):
            continue
        reason = manager.crashes.get(b.id, "dinleme görevi bulunamadı")
        await manager.restart(b)
        await asyncio.sleep(2)
        ok = manager.is_healthy(b.id)
        log.warning("Bekçi: @%s yeniden başlatıldı (%s) → %s", b.username, reason, ok)
        if not ok and time.time() - _wd_alerted.get(b.id, 0) < 6 * 3600:
            continue
        _wd_alerted[b.id] = time.time()
        await notify(f"{'🔄' if ok else '🔴'} <b>@{html.escape(b.username)}</b> durmuştu, "
                     f"{'otomatik yeniden başlatıldı ✅' if ok else 'yeniden başlatılamadı ❌'}\n"
                     f"Sebep: <code>{html.escape(reason[:200])}</code>")


_ext_pending: dict[int, float] = {}


async def external_check(manager, notify, alerted):
    """A modundaki harici botlarda mesajlar 5 dk'dan uzun okunmadan bekliyorsa servis durmuştur."""
    from .external import ext_dir, label, mode as ext_mode
    async with Session() as s:
        bots = (await s.scalars(select(ManagedBot))).all()
    for b in bots:
        xd = ext_dir(b)
        bot = manager.sender(b.id)
        if not xd or ext_mode(xd) != "A" or not bot:
            _ext_pending.pop(b.id, None)
            continue
        try:
            wh = await bot.get_webhook_info()
        except Exception:
            continue
        if wh.url or not wh.pending_update_count:
            _ext_pending.pop(b.id, None)
            continue
        first = _ext_pending.setdefault(b.id, time.time())
        if time.time() - first > 300 and time.time() - alerted.get(-b.id, 0) > 6 * 3600:
            alerted[-b.id] = time.time()
            await notify(f"🔴 <b>@{html.escape(b.username)} cevap vermiyor</b>\n{label(xd)} servisi botu dinlemiyor "
                         f"({wh.pending_update_count} mesaj okunmadan bekliyor).\n\n"
                         "Hızlı çözüm: Botlarım → bot → <b>🔀 Mod → B</b> (Merkez devralır).")


async def monitor_loop(manager):
    """Her dakika: durmuş botları yeniden başlatır, harici servisleri ve polling hatalarını izler."""
    from .channels import notify_admins
    alerted: dict[int, float] = {}
    tick = 0
    while True:
        await asyncio.sleep(60)
        tick += 1
        try:
            await watchdog(manager, notify_admins)
        except Exception:
            log.exception("Bekçi hatası")
        if tick % 2 == 0:
            try:
                await external_check(manager, notify_admins, alerted)
            except Exception:
                log.exception("Harici bot kontrol hatası")
        try:
            for bid, e in list(POLL_ERRORS.items()):
                if time.time() - e["last"] > 300:  # artık hata gelmiyor
                    continue
                if time.time() - e["since"] < 180 or time.time() - alerted.get(bid, 0) < 6 * 3600:
                    continue
                alerted[bid] = time.time()
                async with Session() as s:
                    b = await s.get(ManagedBot, bid)
                name = f"@{b.username}" if b else str(bid)
                await notify_admins(f"🔴 <b>{html.escape(name)} çalışmıyor</b>\n{explain(e['error'])}\n\n"
                                    "Detay: Botlarım → bot → 🩺 Teşhis")
        except Exception:
            log.exception("Bot izleme hatası")
