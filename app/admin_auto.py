"""Yönetici bot: otomatik mesaj zinciri ve promo havuzu yönetimi."""
import html

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, func, select

from .admin import ALBUM_HELP, BACK_HOME, BUTTON_HELP, kb, preview, show, take_content
from .automsg import fmt_delay, list_items, parse_delay, sync_commands
from .broadcast import fmt
from .config import ADMIN_IDS
from .db import AutoQueue, Item, ManagedBot, Session
from .keyboards import parse_buttons, split_btn_text
from .manager import BotManager

router = Router()
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

NAMES = {"auto": ("📨", "Otomatik mesajlar"), "promo": ("🎁", "Promolar")}


class ItemSt(StatesGroup):
    msg = State()
    buttons = State()
    delay = State()
    edit_delay = State()


def _on(v) -> bool:
    return v is not False


# ---------------- tüm botlar: tek ekranda aç/kapat ----------------
async def all_screen():
    async with Session() as s:
        bots = (await s.scalars(select(ManagedBot).order_by(ManagedBot.created_at))).all()
        counts = dict((await s.execute(select(Item.bot_id, func.count()).where(Item.kind == "auto")
                                       .group_by(Item.bot_id))).all())
        pcounts = dict((await s.execute(select(Item.bot_id, func.count()).where(Item.kind == "promo")
                                        .group_by(Item.bot_id))).all())
    lines = ["📨 <b>Otomatik Mesajlar — Tüm Botlar</b>", "",
             "Her bot için start sonrası zinciri ve promoları ayrı ayrı açıp kapatabilirsin.", ""]
    rows = []
    for b in bots:
        a_on, p_on = _on(b.auto_enabled), _on(b.promo_enabled)
        lines.append(f"<b>@{html.escape(b.username)}</b>\n"
                     f"   📨 Zincir: {'🟢 Açık' if a_on else '⏸ Kapalı'} ({counts.get(b.id, 0)} mesaj) • "
                     f"🎁 Promo: {'🟢 Açık' if p_on else '⏸ Kapalı'} ({pcounts.get(b.id, 0)})")
        rows.append([(f"{'🟢' if a_on else '⏸'} Zincir @{b.username}"[:40], f"tga:auto:{b.id}"),
                     (f"{'🟢' if p_on else '⏸'} Promo", f"tga:promo:{b.id}")])
        rows.append([("⚙️ Düzenle", f"itl:auto:{b.id}")])
    if not bots:
        lines.append("Henüz bağlı bot yok.")
    rows.append([("🟢 Hepsini aç", "tgall:1"), ("⏸ Hepsini durdur", "tgall:0")])
    rows.append(BACK_HOME)
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data == "autoall")
async def cb_autoall(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, *(await all_screen()))


@router.callback_query(F.data.startswith("tgall:"))
async def cb_toggle_all(c: CallbackQuery, manager: BotManager):
    val = c.data.endswith(":1")
    async with Session() as s:
        for b in (await s.scalars(select(ManagedBot))).all():
            b.auto_enabled = val
            b.promo_enabled = val
        await s.commit()
    for bid in list(manager.bots):
        await sync_commands(manager.bots[bid])
    await show(c, *(await all_screen()))


async def _toggle_bot(bot_id: int, kind: str, manager: BotManager) -> bool:
    async with Session() as s:
        b = await s.get(ManagedBot, bot_id)
        if kind == "auto":
            b.auto_enabled = not _on(b.auto_enabled)
            val = b.auto_enabled
        else:
            b.promo_enabled = not _on(b.promo_enabled)
            val = b.promo_enabled
        await s.commit()
    if kind == "promo" and manager.sender(bot_id):
        await sync_commands(manager.sender(bot_id))
    return val


@router.callback_query(F.data.startswith("tga:"))
async def cb_toggle_from_all(c: CallbackQuery, manager: BotManager):
    _, kind, bid = c.data.split(":")
    await _toggle_bot(int(bid), kind, manager)
    await show(c, *(await all_screen()))


# ---------------- bir botun listesi ----------------
async def list_screen(kind: str, bot_id: int):
    async with Session() as s:
        b = await s.get(ManagedBot, bot_id)
    items = await list_items(bot_id, kind)
    icon, name = NAMES[kind]
    on = _on(b.auto_enabled if kind == "auto" else b.promo_enabled)
    lines = [f"{icon} <b>@{html.escape(b.username)} — {name}</b>  [{'🟢 Açık' if on else '⏸ Kapalı'}]", ""]
    if kind == "auto":
        lines.append("Yeni abone /start dediğinde sırayla gider. Gecikmeli olanlar zamanı gelince gönderilir.")
    else:
        lines.append("Kullanıcı /promo yazınca veya menüdeki promo butonuna basınca sıradaki promo gider.")
    lines.append("")
    rows = []
    for i, it in enumerate(items, 1):
        st = "🟢" if it.enabled else "⏸"
        extra = fmt_delay(it.delay_sec) if kind == "auto" else f"👁 {fmt(it.count)}"
        lines.append(f"{i}. {st} {html.escape(it.title)} → {extra}"
                     + (f" • 📤 {fmt(it.count)}" if kind == "auto" else ""))
        rows.append([(f"{i}. {st} {it.title}"[:40], f"it:{it.id}")])
    if not items:
        lines.append("<i>Henüz bir şey eklenmedi.</i>")
    rows.append([("➕ Ekle", f"it+:{kind}:{bot_id}"),
                 ("⏸ Durdur" if on else "▶️ Başlat", f"tgb:{kind}:{bot_id}")])
    other = "promo" if kind == "auto" else "auto"
    rows.append([(f"{NAMES[other][0]} {NAMES[other][1]}", f"itl:{other}:{bot_id}"),
                 ("📋 Tüm botlar", "autoall")])
    rows.append([("⬅️ Bot paneli", f"bot:{bot_id}")])
    if kind == "promo":
        lines += ["", "💡 Alt menüde bir butona promo göstermesi için: Bot paneli → 💬 Menü yanıtları → "
                      "buton seç → <code>promo</code> yaz."]
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data.startswith("itl:"))
async def cb_list(c: CallbackQuery, state: FSMContext):
    await state.clear()
    _, kind, bid = c.data.split(":")
    await show(c, *(await list_screen(kind, int(bid))))


@router.callback_query(F.data.startswith("tgb:"))
async def cb_toggle_bot(c: CallbackQuery, manager: BotManager):
    _, kind, bid = c.data.split(":")
    await _toggle_bot(int(bid), kind, manager)
    await show(c, *(await list_screen(kind, int(bid))))


# ---------------- ekleme ----------------
@router.callback_query(F.data.startswith("it+:"))
async def cb_add(c: CallbackQuery, state: FSMContext):
    _, kind, bid = c.data.split(":")
    await state.set_state(ItemSt.msg)
    await state.update_data(kind=kind, bot_id=int(bid))
    what = "otomatik mesajı" if kind == "auto" else "promoyu"
    await show(c, f"✍️ Eklemek istediğin {what} gönder.\nMetin, foto, video veya albüm olabilir. İptal: /iptal")


@router.message(ItemSt.msg)
async def on_item_msg(m: Message, state: FSMContext):
    msg_ids = await take_content(m, state)
    if msg_ids is None:
        return
    title = (m.text or m.caption or "").strip().splitlines()
    title = (title[0] if title else ("📷 Medya" if not m.text else "Mesaj"))[:40]
    await state.update_data(msgs=msg_ids, title=title)
    await state.set_state(ItemSt.buttons)
    await m.answer("🔘 Altına butonları ekle.\n\n" + BUTTON_HELP + (ALBUM_HELP if len(msg_ids) > 1 else ""))


DELAY_HELP = ("⏱ Start'tan ne kadar sonra gitsin?\n\n"
              "<code>hemen</code> • <code>10sn</code> • <code>5dk</code> • <code>1saat</code> • <code>1g</code> • <code>3g</code>")


@router.message(ItemSt.buttons, F.text)
async def on_item_buttons(m: Message, state: FSMContext, bot: Bot, manager: BotManager):
    btn_text, rest = split_btn_text(m.text)
    try:
        buttons = parse_buttons(rest)
    except ValueError as e:
        await m.answer(f"❌ {html.escape(str(e))}")
        return
    await state.update_data(buttons=buttons, btn_text=btn_text)
    data = await state.get_data()
    if data["kind"] == "auto":
        await state.set_state(ItemSt.delay)
        await m.answer(DELAY_HELP)
        return
    await _save_item(m, state, bot, manager, 0)


@router.message(ItemSt.delay, F.text)
async def on_item_delay(m: Message, state: FSMContext, bot: Bot, manager: BotManager):
    sec = parse_delay(m.text)
    if sec is None:
        await m.answer("❌ Anlaşılmadı (en fazla 30 gün). " + DELAY_HELP)
        return
    await _save_item(m, state, bot, manager, sec)


async def _save_item(m: Message, state: FSMContext, bot: Bot, manager: BotManager, delay: int):
    data = await state.get_data()
    async with Session() as s:
        pos = await s.scalar(select(func.coalesce(func.max(Item.position), 0))
                             .where(Item.bot_id == data["bot_id"], Item.kind == data["kind"])) or 0
        it = Item(bot_id=data["bot_id"], kind=data["kind"], position=pos + 1, title=data["title"],
                  msgs=data["msgs"], buttons=data.get("buttons") or None, btn_text=data.get("btn_text"),
                  delay_sec=delay)
        s.add(it)
        await s.commit()
    await state.clear()
    await m.answer("✅ Kaydedildi. Önizleme:")
    await preview(bot, m.chat.id, data["msgs"], data.get("buttons"), btn_text=data.get("btn_text"))
    await m.answer("—", reply_markup=kb([("📋 Listeye dön", f"itl:{data['kind']}:{data['bot_id']}")]))
    if data["kind"] == "promo":
        await _resync(manager, data["bot_id"])


async def _resync(manager: BotManager, bot_id: int):
    if manager.sender(bot_id):
        await sync_commands(manager.sender(bot_id))


@router.callback_query(F.data.startswith(("it:", "itt:", "itu:", "itx:", "itxo:", "itp:", "itd:")))
async def cb_item(c: CallbackQuery, state: FSMContext, bot: Bot, manager: BotManager):
    action, iid = c.data.split(":")
    iid = int(iid)
    async with Session() as s:
        it = await s.get(Item, iid)
        if not it:
            await c.answer("Bulunamadı", show_alert=True)
            return
        kind, bid = it.kind, it.bot_id
        if action == "itt":
            it.enabled = not it.enabled
            await s.commit()
        elif action == "itu":
            items = (await s.scalars(select(Item).where(Item.bot_id == bid, Item.kind == kind)
                                     .order_by(Item.position, Item.id))).all()
            idx = [x.id for x in items].index(iid)
            if idx > 0:
                items[idx - 1], items[idx] = items[idx], items[idx - 1]
            for n, x in enumerate(items, 1):
                x.position = n
            await s.commit()
        elif action == "itxo":
            await s.execute(delete(AutoQueue).where(AutoQueue.item_id == iid))
            await s.delete(it)
            await s.commit()
    if action == "itxo":
        if kind == "promo":
            await _resync(manager, bid)
        await show(c, *(await list_screen(kind, bid)))
        return
    if action == "itx":
        await show(c, "🗑 Silinsin mi?", kb([("✅ Evet", f"itxo:{iid}"), ("❌ Vazgeç", f"it:{iid}")]))
        return
    if action == "itp":
        await c.answer()
        await preview(bot, c.message.chat.id, it.msgs, it.buttons, btn_text=it.btn_text)
    if action == "itd":
        await state.set_state(ItemSt.edit_delay)
        await state.update_data(item_id=iid)
        await show(c, DELAY_HELP)
        return
    if action == "itt" and kind == "promo":
        await _resync(manager, bid)
    await _item_screen(c, iid, new_message=(action == "itp"))


async def _item_screen(target, iid: int, new_message=False):
    async with Session() as s:
        it = await s.get(Item, iid)
        pending = await s.scalar(select(func.count()).where(AutoQueue.item_id == iid)) or 0
    icon, name = NAMES[it.kind]
    lines = [f"{icon} <b>{html.escape(it.title)}</b>",
             f"Durum: {'🟢 Açık' if it.enabled else '⏸ Kapalı'}",
             f"İçerik: {'📎 albüm (' + str(len(it.msgs)) + ')' if len(it.msgs) > 1 else 'tek mesaj'}",
             f"Buton: {sum(len(r) for r in (it.buttons or []))} adet"]
    if it.kind == "auto":
        lines += [f"Zaman: {fmt_delay(it.delay_sec)}", f"📤 Gönderildi: {fmt(it.count)} • ⏳ Bekleyen: {fmt(pending)}"]
    else:
        lines.append(f"👁 Gösterildi: {fmt(it.count)}")
    rows = [[("👁 Önizle", f"itp:{iid}"), ("⏸ Kapat" if it.enabled else "▶️ Aç", f"itt:{iid}")],
            [("⬆️ Yukarı taşı", f"itu:{iid}"), ("🗑 Sil", f"itx:{iid}")]]
    if it.kind == "auto":
        rows.insert(1, [("⏱ Zamanı değiştir", f"itd:{iid}")])
    rows.append([("⬅️ Liste", f"itl:{it.kind}:{it.bot_id}")])
    text, markup = "\n".join(lines), kb(*rows)
    if new_message:
        await target.message.answer(text, reply_markup=markup)
    else:
        await show(target, text, markup)


@router.message(ItemSt.edit_delay, F.text)
async def on_edit_delay(m: Message, state: FSMContext):
    sec = parse_delay(m.text)
    if sec is None:
        await m.answer("❌ Anlaşılmadı. " + DELAY_HELP)
        return
    iid = (await state.get_data())["item_id"]
    async with Session() as s:
        it = await s.get(Item, iid)
        it.delay_sec = sec
        await s.commit()
    await state.clear()
    await m.answer(f"✅ Zaman güncellendi: {fmt_delay(sec)}\n(Zaten kuyrukta bekleyenler eski zamanlarında gider.)",
                   reply_markup=kb([("⬅️ Mesaja dön", f"it:{iid}")]))
