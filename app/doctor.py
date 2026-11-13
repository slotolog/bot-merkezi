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
    task = manager.tasks.get(bot_id)
    running = manager.is_running(bot_id) and task is not None and not task.done()
    lines.append(f"{'🟢' if running else '🔴'} Dinleme (polling): {'çalışıyor' if running else 'çalışmıyor'}")
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


async def monitor_loop(manager):
    """Polling hatası 3 dakikadan uzun sürerse yöneticiye bir kez (6 saatte bir) haber verir."""
    from .channels import notify_admins
    alerted: dict[int, float] = {}
    while True:
        await asyncio.sleep(60)
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
