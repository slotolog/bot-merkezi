"""Yönetici bot: 📢 Kanal Paylaşımları menüsü."""
import asyncio
import html

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, func, select

from .admin import BACK_HOME, kb, show
from .automsg import fmt_delay, parse_delay
from .broadcast import fmt
from .channels import (channels, feed_posts, get_feed, hours_text, parse_hours, post_to_channel,
                       register_channel, run_feed_once, update_feed)
from .config import ADMIN_IDS, TZ
from .db import Channel, ChannelLog, FeedPost, LibItem, ManagedBot, Session, aware
from .manager import BotManager

router = Router()
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


class ChSt(StatesGroup):
    interval = State()
    hours = State()
    add_channel = State()
    manual_targets = State()


async def add_feed_post(lib_id: int) -> bool:
    async with Session() as s:
        if await s.scalar(select(FeedPost.id).where(FeedPost.lib_id == lib_id)):
            return False
        pos = await s.scalar(select(func.coalesce(func.max(FeedPost.position), 0))) or 0
        s.add(FeedPost(lib_id=lib_id, position=pos + 1))
        await s.commit()
    return True


# ---------------- ana ekran ----------------
async def main_screen():
    cfg = await get_feed()
    posts = await feed_posts(only_enabled=False)
    active = [p for p in posts if p[0].enabled and p[1].enabled]
    chans = await channels(only_enabled=False)
    on_ch = [c for c in chans if c.enabled]
    async with Session() as s:
        last = await s.scalar(select(ChannelLog).where(ChannelLog.source == "feed")
                              .order_by(ChannelLog.id.desc()).limit(1))
    nxt = active[cfg["idx"] % len(active)][1].title if active else "—"
    every = fmt_delay(cfg["interval"] * 60).replace(" sonra", "")
    lines = ["📢 <b>Kanal Paylaşımları</b>", "",
             f"Akış: {'🟢 Çalışıyor' if cfg['enabled'] else '⏸ Durduruldu'}",
             f"⏱ Sıklık: her {every} • 🕘 Saatler: {hours_text(cfg)}",
             f"🔀 Mod: {'her kanala aynı post' if cfg['mode'] == 'same' else 'her kanala farklı post'}",
             f"🗑 Önceki postu sil: {'Açık' if cfg['delete_prev'] else 'Kapalı'} • 📌 Sabitle: {'Açık' if cfg['pin'] else 'Kapalı'}",
             "",
             f"📋 Post sırası: {len(active)} aktif / {len(posts)}",
             f"📡 Kanallar: {len(on_ch)} aktif / {len(chans)}",
             f"⏭ Sıradaki post: {html.escape(nxt)}"]
    if last:
        lines.append(f"🕐 Son paylaşım: {aware(last.at).astimezone(TZ):%d.%m %H:%M}")
    if not chans:
        lines += ["", "ℹ️ Kanal eklemek için paylaşım botunu kanala <b>admin</b> yap (mesaj gönderme yetkisiyle). "
                      "Sistem kanalı otomatik tanır."]
    rows = [[("⏸ Akışı durdur" if cfg["enabled"] else "▶️ Akışı başlat", "cht")],
            [("📋 Post sırası", "chq"), ("📡 Kanallar", "chc")],
            [(f"⏱ Sıklık", "chi"), ("🕘 Saatler", "chh")],
            [("🔀 Mod değiştir", "chm"), (f"🗑 Önceki sil: {'Açık' if cfg['delete_prev'] else 'Kapalı'}", "chd")],
            [(f"📌 Sabitle: {'Açık' if cfg['pin'] else 'Kapalı'}", "chp")],
            [("⏭ Sıradakini şimdi paylaş", "chnow"), ("🚀 Seçip paylaş", "chman")],
            [("📑 Son paylaşımlar", "chlog")],
            BACK_HOME]
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data == "ch")
async def cb_main(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, *(await main_screen()))


@router.callback_query(F.data.in_({"cht", "chm", "chd", "chp"}))
async def cb_toggles(c: CallbackQuery):
    cfg = await get_feed()
    if c.data == "cht":
        if not cfg["enabled"]:
            if not await feed_posts():
                await c.answer("Önce post sırasına en az bir post ekle.", show_alert=True)
                return
            if not await channels():
                await c.answer("Önce en az bir kanal ekle.", show_alert=True)
                return
        await update_feed(enabled=not cfg["enabled"])
    elif c.data == "chm":
        await update_feed(mode="spread" if cfg["mode"] == "same" else "same")
    elif c.data == "chd":
        await update_feed(delete_prev=not cfg["delete_prev"])
    elif c.data == "chp":
        await update_feed(pin=not cfg["pin"])
    await show(c, *(await main_screen()))


# ---------------- sıklık / saatler ----------------
@router.callback_query(F.data == "chi")
async def cb_interval(c: CallbackQuery, state: FSMContext):
    await state.set_state(ChSt.interval)
    await show(c, "⏱ Ne sıklıkla paylaşılsın?\n\n<code>30dk</code> • <code>1saat</code> • <code>2saat</code> • "
                  "<code>3saat</code>\n(en az 15 dk) İptal: /iptal")


@router.message(ChSt.interval, F.text)
async def on_interval(m: Message, state: FSMContext):
    sec = parse_delay(m.text)
    if not sec or sec < 900 or sec > 86400:
        await m.answer("❌ 15 dk ile 24 saat arası yaz. Örnek: <code>1saat</code>")
        return
    await update_feed(interval=sec // 60, last_slot="")
    await state.clear()
    await m.answer(f"✅ Sıklık: her {fmt_delay(sec).replace(' sonra', '')}",
                   reply_markup=kb([("📢 Kanal Paylaşımları", "ch")]))


@router.callback_query(F.data == "chh")
async def cb_hours(c: CallbackQuery, state: FSMContext):
    await state.set_state(ChSt.hours)
    await show(c, "🕘 Hangi saatler arasında paylaşılsın? (Türkiye saati)\n\n"
                  "<code>7/24</code> • <code>09-02</code> (sabah 9'dan gece 2'ye) • <code>10-23</code>\nİptal: /iptal")


@router.message(ChSt.hours, F.text)
async def on_hours(m: Message, state: FSMContext):
    h = parse_hours(m.text)
    if not h:
        await m.answer("❌ Anlaşılmadı. Örnek: <code>09-02</code> veya <code>7/24</code>")
        return
    cfg = await update_feed(start=h[0], end=h[1])
    await state.clear()
    await m.answer(f"✅ Paylaşım saatleri: {hours_text(cfg)}", reply_markup=kb([("📢 Kanal Paylaşımları", "ch")]))


# ---------------- post sırası ----------------
async def queue_screen():
    cfg = await get_feed()
    posts = await feed_posts(only_enabled=False)
    active_ids = [fp.id for fp, lib in posts if fp.enabled and lib.enabled]
    nxt = active_ids[cfg["idx"] % len(active_ids)] if active_ids else None
    lines = ["📋 <b>Post sırası</b>", "", "Her paylaşım saatinde sıradaki post gider, sona gelince başa döner.", ""]
    rows = []
    for i, (fp, lib) in enumerate(posts, 1):
        st = "🟢" if fp.enabled and lib.enabled else "⏸"
        mark = " ⏭" if fp.id == nxt else ""
        lines.append(f"{i}. {st} {html.escape(lib.title)} • 📤 {fmt(fp.count)}{mark}")
        rows.append([(f"{i}. {st} {lib.title}"[:40], f"fq:{fp.id}")])
    if not posts:
        lines.append("<i>Henüz post yok.</i>")
    lines += ["", "⏭ = sıradaki"]
    rows.append([("➕ Kütüphaneden ekle", "fqadd"), ("✍️ Yeni post", "fqnew")])
    rows.append([("⬅️ Kanal Paylaşımları", "ch")])
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data == "chq")
async def cb_queue(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, *(await queue_screen()))


@router.callback_query(F.data == "fqadd")
async def cb_queue_pick(c: CallbackQuery):
    async with Session() as s:
        libs = (await s.scalars(select(LibItem).order_by(LibItem.id.desc()))).all()
        have = set((await s.scalars(select(FeedPost.lib_id))).all())
    rows = [[(f"{'✅' if l.id in have else ('🟢' if l.enabled else '⏸')} {l.title}"[:40], f"fqp:{l.id}")] for l in libs]
    if libs:
        rows.append([("✅ Tümünü ekle", "fqall")])
    rows.append([("✍️ Yeni post", "fqnew"), ("⬅️ Geri", "chq")])
    await show(c, "Kütüphaneden seç (✅ = zaten sırada):" if libs else "Kütüphane boş, yeni post yaz:", kb(*rows))


@router.callback_query(F.data.startswith("fqp:"))
async def cb_queue_add(c: CallbackQuery):
    added = await add_feed_post(int(c.data.split(":")[1]))
    await c.answer("Eklendi ✅" if added else "Zaten sırada")
    await cb_queue_pick(c)


@router.callback_query(F.data == "fqall")
async def cb_queue_all(c: CallbackQuery):
    async with Session() as s:
        ids = (await s.scalars(select(LibItem.id).order_by(LibItem.id))).all()
    for lid in ids:
        await add_feed_post(lid)
    await show(c, *(await queue_screen()))


@router.callback_query(F.data == "fqnew")
async def cb_queue_new(c: CallbackQuery, state: FSMContext):
    from .admin_auto import LibSt
    await state.set_state(LibSt.msg)
    await state.update_data(origin={"kind": "feed", "bot": 0})
    await show(c, "✍️ Postu gönder (metin, foto, video veya albüm). Promo kütüphanesine de kaydedilir.\nİptal: /iptal")


@router.callback_query(F.data.startswith(("fq:", "fqt:", "fqu:", "fqx:")))
async def cb_queue_item(c: CallbackQuery, bot: Bot):
    action, pid = c.data.split(":")
    pid = int(pid)
    async with Session() as s:
        fp = await s.get(FeedPost, pid)
        if not fp:
            await show(c, *(await queue_screen()))
            return
        lib = await s.get(LibItem, fp.lib_id)
        if action == "fqt":
            fp.enabled = not fp.enabled
        elif action == "fqu":
            items = (await s.scalars(select(FeedPost).order_by(FeedPost.position, FeedPost.id))).all()
            idx = [x.id for x in items].index(pid)
            if idx > 0:
                items[idx - 1], items[idx] = items[idx], items[idx - 1]
            for n, x in enumerate(items, 1):
                x.position = n
        elif action == "fqx":
            await s.delete(fp)
        await s.commit()
    if action in ("fqu", "fqx"):
        await show(c, *(await queue_screen()))
        return
    lines = [f"📋 <b>{html.escape(lib.title if lib else '?')}</b>",
             f"Akışta: {'🟢 Açık' if fp.enabled else '⏸ Kapalı'}",
             f"📤 Paylaşım sayısı: {fmt(fp.count)}"]
    if lib and not lib.enabled:
        lines.append("⚠️ Kütüphanede kapalı olduğu için paylaşılmıyor.")
    await show(c, "\n".join(lines), kb(
        [("⏸ Akışta kapat" if fp.enabled else "▶️ Akışta aç", f"fqt:{pid}"), ("⬆️ Yukarı", f"fqu:{pid}")],
        [("👁 Önizle", f"lbp:{fp.lib_id}"), ("➖ Sıradan çıkar", f"fqx:{pid}")],
        [("⬅️ Post sırası", "chq")]))


# ---------------- kanallar ----------------
async def channels_screen():
    chans = await channels(only_enabled=False)
    async with Session() as s:
        names = {b.id: b.username for b in (await s.scalars(select(ManagedBot))).all()}
        counts = dict((await s.execute(select(ChannelLog.channel_id, func.count())
                                       .where(ChannelLog.ok.is_(True)).group_by(ChannelLog.channel_id))).all())
    lines = ["📡 <b>Kanallar</b>", ""]
    rows = []
    for ch in chans:
        st = "🟢" if ch.enabled else "⏸"
        lines.append(f"{st} <b>{html.escape(ch.title)}</b> — @{html.escape(names.get(ch.bot_id, '?'))} • "
                     f"📤 {fmt(counts.get(ch.id, 0))}")
        rows.append([(f"{st} {ch.title}"[:40], f"chct:{ch.id}"), ("🗑", f"chcx:{ch.id}")])
    if not chans:
        lines.append("<i>Henüz kanal yok.</i>")
    lines += ["", "<b>Kanal eklemek için:</b> Bağlı botlarından birini kanala <b>admin</b> yap "
                  "(\"Mesaj gönderme\" yetkisi açık). Sistem otomatik tanır.",
              "Bot zaten kanalda admin ise ➕ ile kanalın kullanıcı adını yaz veya kanaldan bir postu ilet.",
              "", "🟢/⏸ = o kanala paylaşımı aç/kapat"]
    rows.append([("➕ Kanal ekle", "chcadd")])
    rows.append([("⬅️ Kanal Paylaşımları", "ch")])
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data == "chc")
async def cb_channels(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, *(await channels_screen()))


@router.callback_query(F.data.startswith(("chct:", "chcx:", "chcxo:")))
async def cb_channel_item(c: CallbackQuery):
    action, cid = c.data.split(":")
    cid = int(cid)
    if action == "chcx":
        await show(c, "🗑 Kanal listeden çıkarılsın mı? (Bot kanalda kalır, sadece paylaşım yapılmaz.)",
                   kb([("✅ Evet", f"chcxo:{cid}"), ("❌ Vazgeç", "chc")]))
        return
    async with Session() as s:
        ch = await s.get(Channel, cid)
        if ch:
            if action == "chct":
                ch.enabled = not ch.enabled
            else:
                await s.delete(ch)
            await s.commit()
    await show(c, *(await channels_screen()))


@router.callback_query(F.data == "chcadd")
async def cb_channel_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(ChSt.add_channel)
    await show(c, "➕ Kanalın kullanıcı adını yaz (ör. <code>@kanalim</code>) veya kanaldan bir postu buraya ilet.\n"
                  "Bağlı botlarından biri o kanalda admin olmalı. İptal: /iptal")


@router.message(ChSt.add_channel)
async def on_channel_add(m: Message, state: FSMContext, manager: BotManager):
    target = None
    origin = getattr(m, "forward_origin", None)
    if origin is not None and getattr(origin, "chat", None) is not None:
        target = origin.chat.id
    elif m.text:
        t = m.text.strip()
        if t.startswith("https://t.me/"):
            t = "@" + t.split("/")[-1]
        target = t if t.startswith("@") else (int(t) if t.lstrip("-").isdigit() else None)
    if target is None:
        await m.answer("❌ Kullanıcı adı (@kanal) yaz veya kanaldan bir post ilet.")
        return
    for bid, bot in list(manager.bots.items()):
        try:
            chat = await bot.get_chat(target)
            me = await bot.me()
            member = await bot.get_chat_member(chat.id, me.id)
        except Exception:
            continue
        if member.status == "administrator" and chat.type == "channel":
            await register_channel(bot, chat, bool(getattr(member, "can_post_messages", False)))
            await state.clear()
            await m.answer(f"✅ <b>{html.escape(chat.title or '')}</b> eklendi (@{html.escape(me.username)} paylaşacak).",
                           reply_markup=kb([("📡 Kanallar", "chc")]))
            return
    await m.answer("❌ Bağlı botlarından hiçbiri bu kanalda admin değil. Önce bir botu kanala admin yap, sonra tekrar dene.")


# ---------------- şimdi paylaş ----------------
@router.callback_query(F.data == "chnow")
async def cb_now(c: CallbackQuery, manager: BotManager):
    await c.answer("Paylaşılıyor…")
    result = await run_feed_once(manager)
    await c.message.answer(f"⏭ {result}")
    text, markup = await main_screen()
    await c.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "chman")
async def cb_manual(c: CallbackQuery, state: FSMContext):
    await state.clear()
    async with Session() as s:
        libs = (await s.scalars(select(LibItem).where(LibItem.enabled.is_(True)).order_by(LibItem.id.desc()))).all()
    if not libs:
        await show(c, "Kütüphanede aktif post yok. Önce 🎁 Promolar'dan ekle.", kb([("⬅️ Geri", "ch")]))
        return
    rows = [[(l.title[:40], f"chml:{l.id}")] for l in libs] + [[("⬅️ Geri", "ch")]]
    await show(c, "🚀 Hangi post paylaşılsın?", kb(*rows))


async def manual_targets_kb(sel):
    chans = await channels(only_enabled=False)
    rows = [[(f"{'☑️' if ch.id in sel else '⬜'} {ch.title}"[:40], f"chms:{ch.id}")] for ch in chans]
    rows.append([("✅ Hepsi", "chms:all"), ("🔲 Hiçbiri", "chms:none")])
    rows.append([("🚀 Paylaş", "chms:go"), ("❌ İptal", "ch")])
    return kb(*rows)


@router.callback_query(F.data.startswith("chml:"))
async def cb_manual_pick(c: CallbackQuery, state: FSMContext):
    lid = int(c.data.split(":")[1])
    sel = [ch.id for ch in await channels()]
    await state.set_state(ChSt.manual_targets)
    await state.update_data(lib_id=lid, sel=sel)
    await show(c, "📡 Hangi kanallara?", await manual_targets_kb(sel))


@router.callback_query(ChSt.manual_targets, F.data.startswith("chms:"))
async def cb_manual_targets(c: CallbackQuery, state: FSMContext, manager: BotManager):
    data = await state.get_data()
    sel, arg = list(data["sel"]), c.data.split(":")[1]
    if arg == "go":
        if not sel:
            await c.answer("En az bir kanal seç.", show_alert=True)
            return
        await state.clear()
        await show(c, "🚀 Paylaşılıyor…")
        cfg = await get_feed()
        async with Session() as s:
            lib = await s.get(LibItem, data["lib_id"])
            chans = (await s.scalars(select(Channel).where(Channel.id.in_(sel)))).all()
        ok = fail = 0
        errs = []
        for ch in chans:
            good, err = await post_to_channel(manager, ch, lib, cfg, "manual")
            ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
            if err:
                errs.append(f"{ch.title}: {err}")
            await asyncio.sleep(0.5)
        txt = f"✅ {ok} kanala paylaşıldı" + (f", ❌ {fail} hata:\n" + "\n".join(html.escape(e) for e in errs[:5]) if fail else "")
        await c.message.answer(txt, reply_markup=kb([("📢 Kanal Paylaşımları", "ch")]))
        return
    if arg == "all":
        sel = [ch.id for ch in await channels(only_enabled=False)]
    elif arg == "none":
        sel = []
    else:
        cid = int(arg)
        sel = [x for x in sel if x != cid] if cid in sel else sel + [cid]
    await state.update_data(sel=sel)
    await show(c, "📡 Hangi kanallara?", await manual_targets_kb(sel))


# ---------------- son paylaşımlar ----------------
@router.callback_query(F.data == "chlog")
async def cb_log(c: CallbackQuery):
    async with Session() as s:
        rows = (await s.execute(select(ChannelLog, Channel.title, LibItem.title)
                                .outerjoin(Channel, Channel.id == ChannelLog.channel_id)
                                .outerjoin(LibItem, LibItem.id == ChannelLog.lib_id)
                                .order_by(ChannelLog.id.desc()).limit(20))).all()
    lines = ["📑 <b>Son paylaşımlar</b>", ""]
    for lg, ch_title, lib_title in rows:
        icon = "✅" if lg.ok else "❌"
        src = "🔁" if lg.source == "feed" else "🚀"
        lines.append(f"{icon}{src} {aware(lg.at).astimezone(TZ):%d.%m %H:%M} • {html.escape(ch_title or '?')} • "
                     f"{html.escape(lib_title or '?')}" + (f"\n   ⚠️ {html.escape(lg.error)}" if lg.error else ""))
    if not rows:
        lines.append("<i>Henüz paylaşım yok.</i>")
    lines += ["", "🔁 akış • 🚀 elle"]
    await show(c, "\n".join(lines), kb([("🔄 Yenile", "chlog"), ("⬅️ Geri", "ch")]))
