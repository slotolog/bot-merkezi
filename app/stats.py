"""Analiz metinleri."""
import html
from datetime import datetime, timedelta

from sqlalchemy import distinct, func, select

from .config import TZ
from .db import Broadcast, Click, Link, ManagedBot, Session, Subscriber, aware

fmt = lambda n: f"{n:,}".replace(",", ".")  # noqa: E731


def _day_start(days_ago: int = 0) -> datetime:
    now = datetime.now(TZ)
    return (now - timedelta(days=days_ago)).replace(hour=0, minute=0, second=0, microsecond=0)


def _pct(a, b):
    return f"%{a / b * 100:.1f}" if b else "%0"


def _bar(v, mx, width=12):
    return "▇" * max(1 if v else 0, round(v / mx * width)) if mx else ""


async def bot_counts(s, bot_id: int | None = None) -> dict[int, dict]:
    """bot_id -> {total, active, blocked, unsub, today, week, lost_week}"""
    t0, t7 = _day_start(0), _day_start(6)
    q = select(
        Subscriber.bot_id,
        func.count(),
        func.count().filter(Subscriber.status == "active"),
        func.count().filter(Subscriber.status == "blocked"),
        func.count().filter(Subscriber.status == "unsub"),
        func.count().filter(Subscriber.joined_at >= t0),
        func.count().filter(Subscriber.joined_at >= t7),
        func.count().filter(Subscriber.left_at >= t7),
    ).group_by(Subscriber.bot_id)
    if bot_id:
        q = q.where(Subscriber.bot_id == bot_id)
    out = {}
    for r in (await s.execute(q)).all():
        out[r[0]] = dict(zip(("total", "active", "blocked", "unsub", "today", "week", "lost_week"), r[1:]))
    return out


EMPTY = {"total": 0, "active": 0, "blocked": 0, "unsub": 0, "today": 0, "week": 0, "lost_week": 0}


async def overview_text() -> str:
    async with Session() as s:
        bots = (await s.scalars(select(ManagedBot).order_by(ManagedBot.created_at))).all()
        counts = await bot_counts(s)
        unique = await s.scalar(select(func.count(distinct(Subscriber.user_id))).where(Subscriber.status == "active")) or 0
    if not bots:
        return "Henüz bağlı bot yok."
    lines = ["📊 <b>Genel Analiz</b>", ""]
    tot = dict(EMPTY)
    ranked = sorted(bots, key=lambda b: counts.get(b.id, EMPTY)["active"], reverse=True)
    for b in ranked:
        c = counts.get(b.id, EMPTY)
        for k in tot:
            tot[k] += c[k]
        icon = "🟢" if b.enabled else "⏸"
        lines.append(f"{icon} <b>@{html.escape(b.username)}</b>\n"
                     f"   👥 {fmt(c['total'])} start • ✅ {fmt(c['active'])} aktif ({_pct(c['active'], c['total'])})\n"
                     f"   📈 Bugün +{fmt(c['today'])} • 7 gün +{fmt(c['week'])} • 📉 7 gün -{fmt(c['lost_week'])}")
    lines += ["", "━━━━━━━━━━━━",
              f"<b>Toplam start:</b> {fmt(tot['total'])}",
              f"<b>Toplam aktif:</b> {fmt(tot['active'])} ({_pct(tot['active'], tot['total'])})",
              f"<b>Tekil aktif kişi</b> (tüm botlarda): {fmt(unique)}",
              f"🚫 Engelleyen: {fmt(tot['blocked'])} • 🔕 Bildirim kapatan: {fmt(tot['unsub'])}",
              f"📈 Bugün +{fmt(tot['today'])} • Son 7 gün +{fmt(tot['week'])}"]
    return "\n".join(lines)


async def bot_detail_text(bot_id: int) -> str:
    async with Session() as s:
        b = await s.get(ManagedBot, bot_id)
        if not b:
            return "Bot bulunamadı."
        c = (await bot_counts(s, bot_id)).get(bot_id, EMPTY)
        t30 = _day_start(29)
        new30 = await s.scalar(select(func.count()).where(Subscriber.bot_id == bot_id, Subscriber.joined_at >= t30)) or 0
        active7 = await s.scalar(select(func.count()).where(
            Subscriber.bot_id == bot_id, Subscriber.status == "active", Subscriber.last_seen >= _day_start(6))) or 0
        joined = (await s.scalars(select(Subscriber.joined_at).where(
            Subscriber.bot_id == bot_id, Subscriber.joined_at >= _day_start(6)))).all()
        sources = (await s.execute(select(Subscriber.source, func.count().label("n"),
                                          func.count().filter(Subscriber.status == "active"))
                                   .where(Subscriber.bot_id == bot_id)
                                   .group_by(Subscriber.source).order_by(func.count().desc()).limit(10))).all()
        langs = (await s.execute(select(Subscriber.lang, func.count())
                                 .where(Subscriber.bot_id == bot_id, Subscriber.status == "active")
                                 .group_by(Subscriber.lang).order_by(func.count().desc()).limit(5))).all()

    days = {}
    for i in range(6, -1, -1):
        days[_day_start(i).date()] = 0
    for j in joined:
        d = aware(j).astimezone(TZ).date()
        if d in days:
            days[d] += 1
    mx = max(days.values()) if days else 0

    lines = [f"📊 <b>@{html.escape(b.username)} — Detaylı Analiz</b>", "",
             f"👥 Toplam start: <b>{fmt(c['total'])}</b>",
             f"✅ Aktif abone: <b>{fmt(c['active'])}</b> ({_pct(c['active'], c['total'])})",
             f"🚫 Engelleyen: {fmt(c['blocked'])} • 🔕 Bildirim kapatan: {fmt(c['unsub'])}",
             f"💬 Son 7 günde botla etkileşen: {fmt(active7)}", "",
             f"📈 Yeni: bugün +{fmt(c['today'])} • 7 gün +{fmt(c['week'])} • 30 gün +{fmt(new30)}",
             f"📉 Son 7 günde kaybedilen: {fmt(c['lost_week'])}", "",
             "<b>Son 7 gün (yeni start)</b>"]
    for d, v in days.items():
        lines.append(f"<code>{d.strftime('%d.%m')}</code> {_bar(v, mx)} {fmt(v)}")
    if sources:
        lines += ["", "<b>Kaynaklar</b> (start linki → toplam / aktif)"]
        for src, n, act in sources:
            lines.append(f"• {html.escape(src or 'organik')}: {fmt(n)} / {fmt(act)}")
    if langs:
        lines += ["", "<b>Dil dağılımı</b> (aktif)"]
        lines.append(" • ".join(f"{(l or '?').upper()}: {fmt(n)}" for l, n in langs))
    lines += ["", f"🔗 Kaynak takibi linki: <code>https://t.me/{html.escape(b.username)}?start=KAYNAK_ADI</code>"]
    return "\n".join(lines)


async def report_text(bc_id: int) -> str:
    async with Session() as s:
        bc = await s.get(Broadcast, bc_id)
        names = {b.id: b.username for b in (await s.scalars(select(ManagedBot))).all()}
        clicks = (await s.execute(
            select(Link.id, Link.text, func.count(Click.id), func.count(distinct(Click.user_id)))
            .select_from(Link).outerjoin(Click, Click.link_id == Link.id)
            .where(Link.broadcast_id == bc_id).group_by(Link.id, Link.text).order_by(Link.id))).all()
        uniq_clickers = await s.scalar(
            select(func.count(distinct(Click.bot_id * 10_000_000_000 + Click.user_id)))
            .join(Link, Link.id == Click.link_id).where(Link.broadcast_id == bc_id)) or 0
        per_bot_clicks = dict((await s.execute(
            select(Click.bot_id, func.count(distinct(Click.user_id)))
            .join(Link, Link.id == Click.link_id).where(Link.broadcast_id == bc_id)
            .group_by(Click.bot_id))).all())
    if not bc:
        return "Yayın bulunamadı."
    st_names = {"scheduled": "⏰ Zamanlandı", "queued": "⏳ Sırada", "running": "📤 Gönderiliyor",
                "done": "✅ Tamamlandı", "cancelled": "❌ İptal", "deleted": "🗑 Silindi"}
    lines = [f"📑 <b>Yayın #{bc.id}</b> — {st_names.get(bc.status, bc.status)}",
             f"Oluşturma: {aware(bc.created_at).astimezone(TZ):%d.%m.%Y %H:%M}"]
    if bc.scheduled_at:
        lines.append(f"Zaman: {aware(bc.scheduled_at).astimezone(TZ):%d.%m.%Y %H:%M}")
    lines.append("Botlar: " + ", ".join("@" + html.escape(names.get(i, str(i))) for i in bc.bot_ids))
    if bc.stats:
        lines.append("")
        tot = {"total": 0, "sent": 0, "blocked": 0, "failed": 0}
        for bid, p in bc.stats.items():
            for k in tot:
                tot[k] += p[k]
            lines.append(f"@{html.escape(names.get(int(bid), bid))}: {fmt(p['sent'])}/{fmt(p['total'])}"
                         f" ({_pct(p['sent'], p['total'])}) • 🚫 {fmt(p['blocked'])}")
        lines.append(f"\n<b>Toplam ulaşan:</b> {fmt(tot['sent'])}/{fmt(tot['total'])} ({_pct(tot['sent'], tot['total'])})")
        if bc.started_at and bc.finished_at:
            sec = (aware(bc.finished_at) - aware(bc.started_at)).total_seconds()
            lines.append(f"Süre: {int(sec // 60)} dk {int(sec % 60)} sn")
    if clicks:
        sent_total = sum(p["sent"] for p in (bc.stats or {}).values())
        lines += ["", "👆 <b>Buton tıklamaları</b> (toplam / tekil kişi)"]
        for _, text, n, u in clicks:
            lines.append(f"• {html.escape(text)}: {fmt(n)} / {fmt(u)}")
        lines.append(f"<b>Tıklayan kişi:</b> {fmt(uniq_clickers)} • <b>Tıklama oranı:</b> {_pct(uniq_clickers, sent_total)}")
        if bc.stats and len(bc.stats) > 1:
            for bid, p in bc.stats.items():
                u = per_bot_clicks.get(int(bid), 0)
                lines.append(f"   @{html.escape(names.get(int(bid), bid))}: {fmt(u)} tıklayan ({_pct(u, p['sent'])})")
    return "\n".join(lines)
