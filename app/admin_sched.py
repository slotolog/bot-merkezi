"""Yönetici bot: 🗓 Haftalık program ekranları."""
import html

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from .admin import kb, preview, show
from .broadcast import fmt
from .config import ADMIN_IDS
from .db import LibItem, ManagedBot, Schedule, Session
from .schedule import DAYS, DAYS_LONG, parse_times

router = Router()
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


class SchSt(StatesGroup):
    days = State()
    times = State()
    lib = State()
    copy = State()


async def sched_screen(bid: int):
    async with Session() as s:
        b = await s.get(ManagedBot, bid)
        slots = (await s.scalars(select(Schedule).where(Schedule.bot_id == bid)
                                 .order_by(Schedule.weekday, Schedule.hour, Schedule.minute))).all()
        libs = {l.id: l for l in (await s.scalars(select(LibItem))).all()}
    on = b.schedule_enabled is not False
    lines = [f"🗓 <b>@{html.escape(b.username)} — Haftalık program</b>  [{'🟢 Açık' if on else '⏸ Kapalı'}]", "",
             "Her hafta belirlenen gün ve saatte abonelere otomatik gönderilir.",
             "Otomatik zinciri hâlâ devam eden kullanıcılar o gönderimde atlanır.", ""]
    rows = []
    cur = None
    for sl in slots:
        lib = libs.get(sl.lib_id)
        title = lib.title if lib else "⚠️ silinmiş içerik"
        st = "🟢" if sl.enabled and lib and lib.enabled else "⏸"
        if sl.weekday != cur:
            cur = sl.weekday
            lines.append(f"<b>{DAYS_LONG[sl.weekday]}</b>")
        lines.append(f"   {st} {sl.hour:02d}:{sl.minute:02d} — {html.escape(title)} • 📤 {fmt(sl.count)}")
        rows.append([(f"{st} {DAYS[sl.weekday]} {sl.hour:02d}:{sl.minute:02d} {title}"[:40], f"schi:{sl.id}")])
    if not slots:
        lines.append("<i>Program boş.</i>")
    rows.append([("➕ Gönderim ekle", f"sch+:{bid}"), ("⏸ Programı durdur" if on else "▶️ Programı başlat", f"scht:{bid}")])
    if slots:
        rows.append([("📋 Başka botlara kopyala", f"schcp:{bid}")])
    rows.append([("⬅️ Bot paneli", f"bot:{bid}")])
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data.startswith("sch:"))
async def cb_sched(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, *(await sched_screen(int(c.data.split(":")[1]))))


@router.callback_query(F.data.startswith("scht:"))
async def cb_toggle(c: CallbackQuery):
    bid = int(c.data.split(":")[1])
    async with Session() as s:
        b = await s.get(ManagedBot, bid)
        b.schedule_enabled = b.schedule_enabled is False
        await s.commit()
    await show(c, *(await sched_screen(bid)))


# ---------------- ekleme: gün → saat → içerik ----------------
def days_kb(sel: list[int]):
    rows = [[(f"{'☑️' if d in sel else '⬜'} {DAYS[d]}", f"schd:{d}") for d in range(0, 4)],
            [(f"{'☑️' if d in sel else '⬜'} {DAYS[d]}", f"schd:{d}") for d in range(4, 7)],
            [("Her gün", "schd:all"), ("Hafta içi", "schd:wd"), ("Hafta sonu", "schd:we")],
            [("➡️ Devam", "schd:ok"), ("❌ İptal", "schd:no")]]
    return kb(*rows)


@router.callback_query(F.data.startswith("sch+:"))
async def cb_add(c: CallbackQuery, state: FSMContext):
    bid = int(c.data.split(":")[1])
    await state.set_state(SchSt.days)
    await state.update_data(bot_id=bid, days=[])
    await show(c, "📅 Hangi günler gönderilsin?", days_kb([]))


@router.callback_query(SchSt.days, F.data.startswith("schd:"))
async def cb_days(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sel, arg = list(data["days"]), c.data.split(":")[1]
    if arg == "no":
        await state.clear()
        await show(c, *(await sched_screen(data["bot_id"])))
        return
    if arg == "ok":
        if not sel:
            await c.answer("En az bir gün seç.", show_alert=True)
            return
        await state.set_state(SchSt.times)
        await show(c, f"⏰ {', '.join(DAYS[d] for d in sorted(sel))} — hangi saat(ler)de? (Türkiye saati)\n\n"
                      "<code>12:00</code> veya birden fazla: <code>12:00, 15:00, 21:30</code>\nİptal: /iptal")
        return
    sel = {"all": list(range(7)), "wd": list(range(5)), "we": [5, 6]}.get(arg) or (
        [x for x in sel if x != int(arg)] if int(arg) in sel else sel + [int(arg)])
    await state.update_data(days=sel)
    await show(c, "📅 Hangi günler gönderilsin?", days_kb(sel))


@router.message(SchSt.times, F.text)
async def on_times(m: Message, state: FSMContext):
    times = parse_times(m.text)
    if not times:
        await m.answer("❌ Anlaşılmadı. Örnek: <code>12:00</code> veya <code>12:00, 15:00</code>")
        return
    await state.update_data(times=times)
    await state.set_state(SchSt.lib)
    async with Session() as s:
        libs = (await s.scalars(select(LibItem).order_by(LibItem.id.desc()))).all()
    if not libs:
        await state.clear()
        await m.answer("Promo kütüphanesi boş. Önce 🎁 Promolar → ➕ Yeni promo ile içerik ekle.",
                       reply_markup=kb([("🎁 Promolar", "lib")]))
        return
    rows = [[(f"{'🟢' if l.enabled else '⏸'} {l.title}"[:40], f"schl:{l.id}")] for l in libs]
    rows.append([("❌ İptal", "schd:no")])
    await m.answer("📦 Ne gönderilsin? (Promo kütüphanesinden seç)", reply_markup=kb(*rows))


@router.callback_query(SchSt.lib, F.data == "schd:no")
async def cb_lib_cancel(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await show(c, *(await sched_screen(data["bot_id"])))


@router.callback_query(SchSt.lib, F.data.startswith("schl:"))
async def cb_lib(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lid = int(c.data.split(":")[1])
    added = 0
    async with Session() as s:
        for d in data["days"]:
            for h, mi in data["times"]:
                dup = await s.scalar(select(Schedule.id).where(
                    Schedule.bot_id == data["bot_id"], Schedule.weekday == d, Schedule.hour == h,
                    Schedule.minute == mi, Schedule.lib_id == lid))
                if not dup:
                    s.add(Schedule(bot_id=data["bot_id"], weekday=d, hour=h, minute=mi, lib_id=lid))
                    added += 1
        await s.commit()
    await state.clear()
    await c.answer(f"{added} gönderim eklendi ✅")
    await show(c, *(await sched_screen(data["bot_id"])))


# ---------------- tek gönderim ----------------
@router.callback_query(F.data.startswith(("schi:", "schit:", "schix:", "schixo:", "schip:")))
async def cb_item(c: CallbackQuery, bot: Bot):
    action, sid = c.data.split(":")
    sid = int(sid)
    async with Session() as s:
        sl = await s.get(Schedule, sid)
        if not sl:
            await c.answer("Bulunamadı")
            return
        bid = sl.bot_id
        lib = await s.get(LibItem, sl.lib_id)
        if action == "schit":
            sl.enabled = not sl.enabled
            await s.commit()
        elif action == "schixo":
            await s.delete(sl)
            await s.commit()
    if action == "schixo":
        await show(c, *(await sched_screen(bid)))
        return
    if action == "schix":
        await show(c, "🗑 Bu gönderim programdan silinsin mi? (İçerik kütüphanede kalır.)",
                   kb([("✅ Evet", f"schixo:{sid}"), ("❌ Vazgeç", f"schi:{sid}")]))
        return
    if action == "schip":
        await c.answer()
        if lib:
            await preview(bot, c.message.chat.id, lib.msgs, lib.buttons, btn_text=lib.btn_text)
    text = (f"🗓 <b>{DAYS_LONG[sl.weekday]} {sl.hour:02d}:{sl.minute:02d}</b>\n"
            f"İçerik: {html.escape(lib.title) if lib else '⚠️ silinmiş'}\n"
            f"Durum: {'🟢 Açık' if sl.enabled else '⏸ Kapalı'}"
            + ("\n⚠️ İçerik kütüphanede kapalı, gönderilmez." if lib and not lib.enabled else "")
            + f"\n📤 Gönderim sayısı: {fmt(sl.count)}\n\nRaporlar: ana menü → 📑 Yayın Raporları")
    markup = kb([("👁 Önizle", f"schip:{sid}"), ("⏸ Kapat" if sl.enabled else "▶️ Aç", f"schit:{sid}")],
                [("🗑 Sil", f"schix:{sid}"), ("⬅️ Program", f"sch:{bid}")])
    if action == "schip":
        await c.message.answer(text, reply_markup=markup)
    else:
        await show(c, text, markup)


# ---------------- başka botlara kopyala ----------------
async def copy_kb(src: int, sel: list[int]):
    async with Session() as s:
        bots = (await s.scalars(select(ManagedBot).where(ManagedBot.id != src).order_by(ManagedBot.created_at))).all()
    rows = [[(f"{'☑️' if b.id in sel else '⬜'} @{b.username}", f"schc:{b.id}")] for b in bots]
    rows.append([("✅ Hepsi", "schc:all"), ("💾 Kopyala", "schc:ok")])
    rows.append([("❌ İptal", f"sch:{src}")])
    return kb(*rows)


@router.callback_query(F.data.startswith("schcp:"))
async def cb_copy(c: CallbackQuery, state: FSMContext):
    src = int(c.data.split(":")[1])
    await state.set_state(SchSt.copy)
    await state.update_data(src=src, sel=[])
    await show(c, "📋 Bu program hangi botlara kopyalansın? (Var olan gönderimler korunur, aynısı tekrar eklenmez.)",
               await copy_kb(src, []))


@router.callback_query(SchSt.copy, F.data.startswith("schc:"))
async def cb_copy_sel(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    src, sel, arg = data["src"], list(data["sel"]), c.data.split(":")[1]
    if arg == "ok":
        if not sel:
            await c.answer("En az bir bot seç.", show_alert=True)
            return
        added = 0
        async with Session() as s:
            slots = (await s.scalars(select(Schedule).where(Schedule.bot_id == src))).all()
            for bid in sel:
                for sl in slots:
                    dup = await s.scalar(select(Schedule.id).where(
                        Schedule.bot_id == bid, Schedule.weekday == sl.weekday, Schedule.hour == sl.hour,
                        Schedule.minute == sl.minute, Schedule.lib_id == sl.lib_id))
                    if not dup:
                        s.add(Schedule(bot_id=bid, weekday=sl.weekday, hour=sl.hour, minute=sl.minute,
                                       lib_id=sl.lib_id, enabled=sl.enabled))
                        added += 1
            await s.commit()
        await state.clear()
        await c.answer(f"{added} gönderim kopyalandı ✅", show_alert=True)
        await show(c, *(await sched_screen(src)))
        return
    if arg == "all":
        async with Session() as s:
            sel = list((await s.scalars(select(ManagedBot.id).where(ManagedBot.id != src))).all())
    else:
        bid = int(arg)
        sel = [x for x in sel if x != bid] if bid in sel else sel + [bid]
    await state.update_data(sel=sel)
    await show(c, "📋 Bu program hangi botlara kopyalansın?", await copy_kb(src, sel))
