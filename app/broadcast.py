"""Yayın motoru: bot başına hız sınırı, 429 bekleme, engelleyenleri işaretleme,
canlı ilerleme, zamanlanmış yayınlar, yayın silme ve aktiflik kontrolü."""
import asyncio
import html
import logging
import time
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, select, update

from .config import PUBLIC_URL, SEND_RATE
from .content import send_content, track_url
from .db import Broadcast, Delivery, Session, Subscriber, all_bots, utcnow
from .keyboards import build_markup
from .manager import BotManager

log = logging.getLogger(__name__)


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", ".")


async def _with_retry(coro_factory):
    """('ok', result) | ('blocked', None) | ('failed', None)"""
    for _ in range(4):
        try:
            return "ok", await coro_factory()
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            return "blocked", None
        except TelegramBadRequest as e:
            if "chat not found" in str(e).lower():
                return "blocked", None
            return "failed", None
        except Exception as e:  # ağ hatası vs.
            log.warning("Gönderim hatası: %s", e)
            await asyncio.sleep(1)
    return "failed", None


async def _mark_blocked(bot_id: int, user_ids: list[int]):
    if not user_ids:
        return
    async with Session() as s:
        await s.execute(update(Subscriber)
                        .where(Subscriber.bot_id == bot_id, Subscriber.user_id.in_(user_ids))
                        .values(status="blocked", left_at=utcnow()))
        await s.commit()


async def active_user_ids(bot_id: int) -> list[int]:
    async with Session() as s:
        return list((await s.scalars(select(Subscriber.user_id)
                                     .where(Subscriber.bot_id == bot_id, Subscriber.status == "active"))).all())


class BroadcastRunner:
    def __init__(self, manager: BotManager, admin_bot: Bot):
        self.manager = manager
        self.admin_bot = admin_bot
        self.running: set[int] = set()

    # ---------- yayın ----------
    def start(self, bc_id: int):
        if bc_id not in self.running:
            self.running.add(bc_id)
            asyncio.create_task(self.run(bc_id))

    async def run(self, bc_id: int):
        self.running.add(bc_id)
        try:
            async with Session() as s:
                bc = await s.get(Broadcast, bc_id)
                if not bc or bc.status not in ("queued", "scheduled"):
                    return
                bc.status, bc.started_at = "running", utcnow()
                await s.commit()
            names = {b.id: b.username for b in await all_bots()}
            progress: dict[int, dict] = {bid: {"total": 0, "sent": 0, "blocked": 0, "failed": 0} for bid in bc.bot_ids}
            status_msg = await self.admin_bot.send_message(bc.created_by, f"📢 Yayın #{bc_id} başladı…")
            msg_ids = bc.msg_ids or [bc.msg]

            done = asyncio.Event()

            async def updater():
                last = ""
                while not done.is_set():
                    await asyncio.sleep(4)
                    text = self._progress_text(bc_id, progress, names, final=False)
                    if text != last:
                        try:
                            await status_msg.edit_text(text)
                        except Exception:
                            pass
                        last = text

            upd = asyncio.create_task(updater())
            await asyncio.gather(*(self._send_bot(bc, bid, msg_ids, progress[bid]) for bid in bc.bot_ids))
            done.set()
            upd.cancel()

            async with Session() as s:
                bc = await s.get(Broadcast, bc_id)
                bc.status, bc.finished_at = "done", utcnow()
                bc.stats = {str(k): v for k, v in progress.items()}
                await s.commit()
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🗑 Tüm botlardan sil", callback_data=f"repdel:{bc_id}"),
                InlineKeyboardButton(text="📑 Rapor", callback_data=f"rep:{bc_id}")]])
            await status_msg.edit_text(self._progress_text(bc_id, progress, names, final=True), reply_markup=kb)
        except Exception:
            log.exception("Yayın #%s hata", bc_id)
        finally:
            self.running.discard(bc_id)

    def _progress_text(self, bc_id, progress, names, final):
        head = f"✅ <b>Yayın #{bc_id} tamamlandı</b>" if final else f"⏳ <b>Yayın #{bc_id} gönderiliyor…</b>"
        lines = [head, ""]
        tot = {"total": 0, "sent": 0, "blocked": 0, "failed": 0}
        for bid, p in progress.items():
            for k in tot:
                tot[k] += p[k]
            lines.append(f"@{html.escape(names.get(bid, str(bid)))}: {fmt(p['sent'])}/{fmt(p['total'])}"
                         f" • 🚫 {fmt(p['blocked'])} • ⚠️ {fmt(p['failed'])}")
        rate = (tot["sent"] / tot["total"] * 100) if tot["total"] else 0
        lines += ["", f"<b>Toplam:</b> {fmt(tot['sent'])}/{fmt(tot['total'])} ulaştı (%{rate:.1f})",
                  f"🚫 Engelleyen: {fmt(tot['blocked'])} • ⚠️ Hata: {fmt(tot['failed'])}"]
        return "\n".join(lines)

    async def _send_bot(self, bc, bot_id, msg_ids, p):
        bc_id = bc.id
        bot = self.manager.sender(bot_id)
        tracking = bool(PUBLIC_URL) and any(b.get("lid") for row in (bc.buttons or []) for b in row)
        static_markup = build_markup(bc.buttons, unsub=bc.unsub_btn)
        users = await active_user_ids(bot_id)
        p["total"] = len(users)
        if not bot:
            p["failed"] = len(users)
            return
        interval = 1 / SEND_RATE
        deliveries, blocked = [], []
        for uid in users:
            t0 = time.monotonic()
            markup = (build_markup(bc.buttons, unsub=bc.unsub_btn,
                                   url_fn=lambda b, u=uid: track_url(b["lid"], bot_id, u))
                      if tracking else static_markup)
            res, mids = await _with_retry(lambda: send_content(bot, uid, msg_ids, markup, bc.btn_text))
            if res == "ok":
                p["sent"] += 1
                deliveries += [Delivery(broadcast_id=bc_id, bot_id=bot_id, user_id=uid, message_id=x) for x in mids]
            elif res == "blocked":
                p["blocked"] += 1
                blocked.append(uid)
            else:
                p["failed"] += 1
            if len(deliveries) >= 500 or len(blocked) >= 500:
                await self._flush(bot_id, deliveries, blocked)
                deliveries, blocked = [], []
            await asyncio.sleep(max(0, interval - (time.monotonic() - t0)))
        await self._flush(bot_id, deliveries, blocked)

    async def _flush(self, bot_id, deliveries, blocked):
        if deliveries:
            async with Session() as s:
                s.add_all(deliveries)
                await s.commit()
        await _mark_blocked(bot_id, blocked)

    # ---------- yayını silme ----------
    async def delete_broadcast(self, bc_id: int) -> tuple[int, int]:
        async with Session() as s:
            rows = (await s.execute(select(Delivery.bot_id, Delivery.user_id, Delivery.message_id)
                                    .where(Delivery.broadcast_id == bc_id))).all()
        ok = fail = 0
        for bot_id, uid, mid in rows:
            bot = self.manager.sender(bot_id)
            if not bot:
                fail += 1
                continue
            res, _ = await _with_retry(lambda: bot.delete_message(chat_id=uid, message_id=mid))
            ok, fail = (ok + 1, fail) if res == "ok" else (ok, fail + 1)
            await asyncio.sleep(1 / SEND_RATE)
        async with Session() as s:
            bc = await s.get(Broadcast, bc_id)
            if bc:
                bc.status = "deleted"
                await s.commit()
        return ok, fail

    # ---------- aktiflik kontrolü ----------
    async def check_active(self, bot_id: int) -> tuple[int, int]:
        """Mesaj atmadan 'yazıyor…' sinyali gönderir; hata verenleri engelli işaretler."""
        bot = self.manager.sender(bot_id)
        if not bot:
            return 0, 0
        users = await active_user_ids(bot_id)
        blocked = []
        for uid in users:
            res, _ = await _with_retry(lambda: bot.send_chat_action(chat_id=uid, action="typing"))
            if res == "blocked":
                blocked.append(uid)
            await asyncio.sleep(1 / SEND_RATE)
        await _mark_blocked(bot_id, blocked)
        return len(users) - len(blocked), len(blocked)

    async def cleanup(self):
        """3 günden eski yayınların gönderim kayıtlarını siler (Telegram 48 saatten sonra
        mesaj sildirmediği için gereksiz; disk dolmasın)."""
        try:
            limit = utcnow() - timedelta(days=3)
            async with Session() as s:
                old = select(Broadcast.id).where(Broadcast.finished_at.is_not(None), Broadcast.finished_at < limit)
                res = await s.execute(delete(Delivery).where(Delivery.broadcast_id.in_(old)))
                await s.commit()
                if res.rowcount:
                    log.info("Temizlik: %s eski gönderim kaydı silindi", res.rowcount)
        except Exception:
            log.exception("Temizlik hatası")

    # ---------- zamanlayıcı ----------
    async def scheduler_loop(self):
        # yeniden başlatmada yarım kalan yayınları kurtar
        async with Session() as s:
            await s.execute(update(Broadcast).where(Broadcast.status == "running").values(status="done"))
            await s.commit()
        last_cleanup = 0.0
        while True:
            if time.monotonic() - last_cleanup > 3600:
                last_cleanup = time.monotonic()
                await self.cleanup()
            try:
                async with Session() as s:
                    due = (await s.scalars(select(Broadcast.id).where(
                        Broadcast.status.in_(("scheduled", "queued")),
                        (Broadcast.scheduled_at.is_(None)) | (Broadcast.scheduled_at <= utcnow())))).all()
                for bc_id in due:
                    self.start(bc_id)
            except Exception:
                log.exception("Zamanlayıcı hatası")
            await asyncio.sleep(20)
