"""Yönetici bot: promo kütüphanesi, botların otomatik mesaj zinciri ve /promo havuzu.

Mantık: içerik (promo) kütüphaneye BİR KERE kaydedilir. Botların zincirine veya /promo
havuzuna sadece bağlantısı (Item.lib_id) eklenir. Kütüphanede kapatılan içerik her yerde durur.
"""
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
from .db import AutoQueue, Item, LibItem, ManagedBot, Session
from .keyboards import parse_buttons, split_btn_text
from .manager import BotManager

router = Router()
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

NAMES = {"auto": ("📨", "Otomatik zincir"), "promo": ("🎁", "/promo havuzu")}
DELAY_HELP = ("⏱ Start'tan ne kadar sonra gitsin?\n\n"
              "<code>hemen</code> • <code>10sn</code> • <code>5dk</code> • <code>1saat</code> • <code>1g</code> • <code>3g</code>")


class LibSt(StatesGroup):
    msg = State()
    buttons = State()
    targets = State()
    delay = State()
    edit_delay = State()


def _on(v) -> bool:
    return v is not False


async def _bots() -> list[ManagedBot]:
    async with Session() as s:
        return list((await s.scalars(select(ManagedBot).order_by(ManagedBot.created_at))).all())


async def _resync(manager: BotManager, bot_ids):
    for bid in bot_ids:
        if manager.sender(bid):
            await sync_commands(manager.sender(bid))


async def add_refs(lib_id: int, kind: str, bot_ids: list[int], delay: int = 0) -> int:
    """Kütüphane içeriğini botlara bağlar (aynı botta aynı türde zaten varsa atlar)."""
    added = 0
    async with Session() as s:
        lib = await s.get(LibItem, lib_id)
        for bid in bot_ids:
            exists = await s.scalar(select(Item.id).where(Item.bot_id == bid, Item.kind == kind, Item.lib_id == lib_id))
            if exists:
                continue
            pos = await s.scalar(select(func.coalesce(func.max(Item.position), 0))
                                 .where(Item.bot_id == bid, Item.kind == kind)) or 0
            s.add(Item(bot_id=bid, kind=kind, position=pos + 1, title=lib.title, msgs=[], lib_id=lib_id,
                       delay_sec=delay))
            added += 1
        await s.commit()
    return added


# ======================= PROMO KÜTÜPHANESİ =======================
async def lib_screen():
    async with Session() as s:
        libs = (await s.scalars(select(LibItem).order_by(LibItem.id.desc()))).all()
        usage = (await s.execute(select(Item.lib_id, Item.kind, func.count()).where(Item.lib_id.is_not(None))
                                 .group_by(Item.lib_id, Item.kind))).all()
    use = {}
    for lid, kind, n in usage:
        use.setdefault(lid, {})[kind] = n
    lines = ["🎁 <b>Promo Kütüphanesi</b>", "",
             "Promoları burada <b>bir kere</b> kaydedersin. Sonra istediğin botların /promo havuzuna veya "
             "otomatik zincirine eklersin. Burada ⏸ yaptığın promo tüm botlarda durur.", ""]
    rows = []
    for lib in libs:
        u = use.get(lib.id, {})
        st = "🟢" if lib.enabled else "⏸"
        lines.append(f"{st} {html.escape(lib.title)} — 🎁 {u.get('promo', 0)} bot • 📨 {u.get('auto', 0)} zincir")
        rows.append([(f"{st} {lib.title}"[:40], f"lb:{lib.id}")])
    if not libs:
        lines.append("<i>Henüz promo yok.</i>")
    rows.append([("➕ Yeni promo", "lb+")])
    rows.append([("📨 Otomatik mesajlar (botlar)", "autoall")])
    rows.append(BACK_HOME)
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data == "lib")
async def cb_lib(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, *(await lib_screen()))


@router.callback_query(F.data == "lb+")
async def cb_lib_add(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(LibSt.msg)
    await state.update_data(origin=None)
    await show(c, "✍️ Promoyu gönder (metin, foto, video veya albüm).\n"
                  "İlk satırı promo adı olarak kullanılır. İptal: /iptal")


# --- içerik → buton (kütüphaneye kayıt) ---
@router.message(LibSt.msg)
async def on_lib_msg(m: Message, state: FSMContext):
    msg_ids = await take_content(m, state)
    if msg_ids is None:
        return
    first = (m.text or m.caption or "").strip().splitlines()
    title = (first[0] if first else "📷 Medya promo")[:40]
    await state.update_data(msgs=msg_ids, title=title)
    await state.set_state(LibSt.buttons)
    await m.answer("🔘 Altına butonları ekle.\n\n" + BUTTON_HELP + (ALBUM_HELP if len(msg_ids) > 1 else ""))


@router.message(LibSt.buttons, F.text)
async def on_lib_buttons(m: Message, state: FSMContext, bot: Bot, manager: BotManager):
    btn_text, rest = split_btn_text(m.text)
    try:
        buttons = parse_buttons(rest)
    except ValueError as e:
        await m.answer(f"❌ {html.escape(str(e))}")
        return
    data = await state.get_data()
    async with Session() as s:
        lib = LibItem(title=data["title"], msgs=data["msgs"], buttons=buttons or None, btn_text=btn_text)
        s.add(lib)
        await s.commit()
        lib_id = lib.id
    await m.answer("✅ Kütüphaneye kaydedildi. Önizleme:")
    await preview(bot, m.chat.id, data["msgs"], buttons, btn_text=btn_text)
    origin = data.get("origin")  # bot ekranından geldiyse {"kind":..., "bot":...}
    if origin and origin["kind"] == "promo":
        await add_refs(lib_id, "promo", [origin["bot"]])
        await state.clear()
        await _resync(manager, [origin["bot"]])
        await m.answer("🎁 Botun /promo havuzuna eklendi.", reply_markup=kb([("📋 Listeye dön", f"itl:promo:{origin['bot']}")]))
        return
    if origin and origin["kind"] == "feed":
        from .admin_channels import add_feed_post
        await add_feed_post(lib_id)
        await state.clear()
        await m.answer("📢 Kanal akışına eklendi.", reply_markup=kb([("📋 Post sırası", "chq")]))
        return
    if origin and origin["kind"] == "auto":
        await state.set_state(LibSt.delay)
        await state.update_data(lib_id=lib_id, mode="auto", sel=[origin["bot"]])
        await m.answer(DELAY_HELP)
        return
    # kütüphaneden eklendiyse: hangi botların /promo havuzuna girsin? (varsayılan: hepsi)
    all_ids = [b.id for b in await _bots()]
    await state.set_state(LibSt.targets)
    await state.update_data(lib_id=lib_id, mode="promo", sel=all_ids)
    text, markup = await targets_screen(lib_id, "promo", all_ids)
    await m.answer(text, reply_markup=markup)


# --- bot seçimi (çoklu) ---
async def targets_screen(lib_id: int, mode: str, sel: list[int]):
    bots = await _bots()
    if mode == "promo":
        text = "🎁 Bu promo hangi botların <b>/promo havuzunda</b> olsun?\n(İşaretlemediğin botlardan çıkarılır.)"
    else:
        text = "📨 Bu promo hangi botların <b>otomatik zincirine</b> eklensin?"
    rows = [[(f"{'☑️' if b.id in sel else '⬜'} @{b.username}", f"lt:{b.id}")] for b in bots]
    rows.append([("✅ Hepsi", "lt:all"), ("🔲 Hiçbiri", "lt:none")])
    rows.append([("💾 Kaydet", "lt:ok"), ("↩️ Vazgeç", f"lb:{lib_id}")])
    return text, kb(*rows)


@router.callback_query(LibSt.targets, F.data.startswith("lt:"))
async def cb_targets(c: CallbackQuery, state: FSMContext, manager: BotManager):
    data = await state.get_data()
    sel, arg = list(data.get("sel", [])), c.data.split(":")[1]
    lib_id, mode = data["lib_id"], data["mode"]
    if arg == "ok":
        if mode == "auto":
            if not sel:
                await c.answer("En az bir bot seç.", show_alert=True)
                return
            await state.set_state(LibSt.delay)
            await show(c, DELAY_HELP)
            return
        # promo: seçime göre ekle / çıkar
        async with Session() as s:
            current = set((await s.scalars(select(Item.bot_id).where(Item.lib_id == lib_id, Item.kind == "promo"))).all())
            await s.execute(delete(Item).where(Item.lib_id == lib_id, Item.kind == "promo",
                                               Item.bot_id.not_in(sel or [0])))
            await s.commit()
        await add_refs(lib_id, "promo", sel)
        await state.clear()
        await _resync(manager, current | set(sel))
        await show(c, *(await lib_item_screen(lib_id)))
        return
    if arg == "all":
        sel = [b.id for b in await _bots()]
    elif arg == "none":
        sel = []
    else:
        bid = int(arg)
        sel = [x for x in sel if x != bid] if bid in sel else sel + [bid]
    await state.update_data(sel=sel)
    await show(c, *(await targets_screen(lib_id, mode, sel)))


@router.message(LibSt.delay, F.text)
async def on_delay(m: Message, state: FSMContext):
    sec = parse_delay(m.text)
    if sec is None:
        await m.answer("❌ Anlaşılmadı (en fazla 30 gün). " + DELAY_HELP)
        return
    data = await state.get_data()
    n = await add_refs(data["lib_id"], "auto", data["sel"], sec)
    await state.clear()
    back = [("📋 Zincire dön", f"itl:auto:{data['sel'][0]}")] if len(data["sel"]) == 1 else [("📨 Tüm botlar", "autoall")]
    await m.answer(f"✅ {n} botun zincirine eklendi ({fmt_delay(sec)}).", reply_markup=kb(back, [("🎁 Kütüphane", "lib")]))


# --- kütüphane öğesi ---
async def lib_item_screen(lib_id: int):
    async with Session() as s:
        lib = await s.get(LibItem, lib_id)
        if not lib:
            return "Bulunamadı.", kb([("🎁 Kütüphane", "lib")])
        refs = (await s.execute(select(Item.kind, ManagedBot.username, Item.delay_sec, Item.count, Item.enabled)
                                .join(ManagedBot, ManagedBot.id == Item.bot_id)
                                .where(Item.lib_id == lib_id))).all()
    promo = [r for r in refs if r[0] == "promo"]
    auto = [r for r in refs if r[0] == "auto"]
    lines = [f"🎁 <b>{html.escape(lib.title)}</b>",
             f"Durum: {'🟢 Açık (tüm botlarda)' if lib.enabled else '⏸ Kapalı (tüm botlarda durdu)'}",
             f"İçerik: {'📎 albüm (' + str(len(lib.msgs)) + ')' if len(lib.msgs) > 1 else 'tek mesaj'} • "
             f"Buton: {sum(len(r) for r in (lib.buttons or []))}", ""]
    lines.append("<b>/promo havuzunda:</b> " + (", ".join(
        f"@{html.escape(u)} (👁 {fmt(n)})" for _, u, _, n, _ in promo) or "—"))
    lines.append("<b>Otomatik zincirde:</b> " + (", ".join(
        f"@{html.escape(u)} ({fmt_delay(d)}, 📤 {fmt(n)})" for _, u, d, n, _ in auto) or "—"))
    rows = [[("👁 Önizle", f"lbp:{lib_id}"), ("⏸ Her yerde durdur" if lib.enabled else "▶️ Her yerde aç", f"lbt:{lib_id}")],
            [("🎁 /promo havuzları", f"lbh:{lib_id}"), ("📨 Zincire ekle", f"lbz:{lib_id}")],
            [("🗑 Sil", f"lbx:{lib_id}"), ("⬅️ Kütüphane", "lib")]]
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data.startswith(("lb:", "lbp:", "lbt:", "lbh:", "lbz:", "lbx:", "lbxo:")))
async def cb_lib_item(c: CallbackQuery, state: FSMContext, bot: Bot, manager: BotManager):
    action, lid = c.data.split(":")
    lid = int(lid)
    await state.clear()
    async with Session() as s:
        lib = await s.get(LibItem, lid)
        if not lib:
            await show(c, *(await lib_screen()))
            return
        users = set((await s.scalars(select(Item.bot_id).where(Item.lib_id == lid))).all())
        if action == "lbt":
            lib.enabled = not lib.enabled
            await s.commit()
        elif action == "lbxo":
            ids = (await s.scalars(select(Item.id).where(Item.lib_id == lid))).all()
            await s.execute(delete(AutoQueue).where(AutoQueue.item_id.in_(ids or [0])))
            await s.execute(delete(Item).where(Item.lib_id == lid))
            await s.delete(lib)
            await s.commit()
    if action in ("lbt", "lbxo"):
        await _resync(manager, users)
    if action == "lbxo":
        await show(c, *(await lib_screen()))
        return
    if action == "lbx":
        await show(c, "🗑 Promo silinsin mi? Bağlı olduğu tüm botların zincir ve /promo listelerinden de kalkar.",
                   kb([("✅ Evet, sil", f"lbxo:{lid}"), ("❌ Vazgeç", f"lb:{lid}")]))
        return
    if action == "lbp":
        await c.answer()
        await preview(bot, c.message.chat.id, lib.msgs, lib.buttons, btn_text=lib.btn_text)
        text, markup = await lib_item_screen(lid)
        await c.message.answer(text, reply_markup=markup)
        return
    if action in ("lbh", "lbz"):
        mode = "promo" if action == "lbh" else "auto"
        async with Session() as s:
            sel = list((await s.scalars(select(Item.bot_id).where(Item.lib_id == lid, Item.kind == "promo"))).all()) \
                if mode == "promo" else []
        await state.set_state(LibSt.targets)
        await state.update_data(lib_id=lid, mode=mode, sel=sel)
        await show(c, *(await targets_screen(lid, mode, sel)))
        return
    await show(c, *(await lib_item_screen(lid)))


# ======================= TÜM BOTLAR: aç / kapat =======================
async def all_screen():
    async with Session() as s:
        bots = (await s.scalars(select(ManagedBot).order_by(ManagedBot.created_at))).all()
        cnt = {}
        for bid, kind, n in (await s.execute(select(Item.bot_id, Item.kind, func.count())
                                             .group_by(Item.bot_id, Item.kind))).all():
            cnt[(bid, kind)] = n
    lines = ["📨 <b>Otomatik Mesajlar — Tüm Botlar</b>", "",
             "Her botun start sonrası zincirini ve /promo havuzunu ayrı ayrı açıp kapatabilirsin.", ""]
    rows = []
    for b in bots:
        a_on, p_on = _on(b.auto_enabled), _on(b.promo_enabled)
        lines.append(f"<b>@{html.escape(b.username)}</b>\n"
                     f"   📨 Zincir: {'🟢 Açık' if a_on else '⏸ Kapalı'} ({cnt.get((b.id, 'auto'), 0)} mesaj) • "
                     f"🎁 /promo: {'🟢 Açık' if p_on else '⏸ Kapalı'} ({cnt.get((b.id, 'promo'), 0)})")
        rows.append([(f"{'🟢' if a_on else '⏸'} Zincir @{b.username}"[:40], f"tga:auto:{b.id}"),
                     (f"{'🟢' if p_on else '⏸'} /promo", f"tga:promo:{b.id}")])
        rows.append([("📨 Zinciri düzenle", f"itl:auto:{b.id}"), ("🎁 Havuzu düzenle", f"itl:promo:{b.id}")])
    if not bots:
        lines.append("Henüz bağlı bot yok.")
    rows.append([("🟢 Hepsini aç", "tgall:1"), ("⏸ Hepsini durdur", "tgall:0")])
    rows.append([("🎁 Promo kütüphanesi", "lib")] + BACK_HOME)
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
    await _resync(manager, list(manager.bots))
    await show(c, *(await all_screen()))


async def _toggle_bot(bot_id: int, kind: str, manager: BotManager):
    async with Session() as s:
        b = await s.get(ManagedBot, bot_id)
        if kind == "auto":
            b.auto_enabled = not _on(b.auto_enabled)
        else:
            b.promo_enabled = not _on(b.promo_enabled)
        await s.commit()
    if kind == "promo":
        await _resync(manager, [bot_id])


@router.callback_query(F.data.startswith("tga:"))
async def cb_toggle_from_all(c: CallbackQuery, manager: BotManager):
    _, kind, bid = c.data.split(":")
    await _toggle_bot(int(bid), kind, manager)
    await show(c, *(await all_screen()))


# ======================= BİR BOTUN ZİNCİRİ / HAVUZU =======================
async def list_screen(kind: str, bot_id: int):
    async with Session() as s:
        b = await s.get(ManagedBot, bot_id)
        libs = {l.id: l for l in (await s.scalars(select(LibItem))).all()}
    items = await list_items(bot_id, kind)
    icon, name = NAMES[kind]
    on = _on(b.auto_enabled if kind == "auto" else b.promo_enabled)
    lines = [f"{icon} <b>@{html.escape(b.username)} — {name}</b>  [{'🟢 Açık' if on else '⏸ Kapalı'}]", ""]
    lines.append("<b>Yeni</b> abone /start dediğinde sırayla gider (daha önce kayıtlı olanlara tekrar gitmez)." if kind == "auto"
                 else "Kullanıcı /promo yazınca veya menüdeki promo butonuna basınca sıradaki gider.")
    lines.append("")
    rows = []
    for i, it in enumerate(items, 1):
        lib = libs.get(it.lib_id)
        title = lib.title if lib else it.title
        st = "🟢" if it.enabled and (lib is None or lib.enabled) else "⏸"
        extra = f"{fmt_delay(it.delay_sec)} • 📤 {fmt(it.count)}" if kind == "auto" else f"👁 {fmt(it.count)}"
        lines.append(f"{i}. {st} {html.escape(title)} → {extra}")
        rows.append([(f"{i}. {st} {title}"[:40], f"it:{it.id}")])
    if not items:
        lines.append("<i>Henüz bir şey eklenmedi.</i>")
    rows.append([("➕ Kütüphaneden ekle", f"it+:{kind}:{bot_id}"),
                 ("⏸ Durdur" if on else "▶️ Başlat", f"tgb:{kind}:{bot_id}")])
    if kind == "auto" and items:
        rows.append([("🧪 Bana test et", f"itest:{bot_id}")])
    other = "promo" if kind == "auto" else "auto"
    rows.append([(f"{NAMES[other][0]} {NAMES[other][1]}", f"itl:{other}:{bot_id}"), ("📋 Tüm botlar", "autoall")])
    rows.append([("⬅️ Bot paneli", f"bot:{bot_id}")])
    if kind == "promo":
        lines += ["", "💡 Alt menüdeki bir buton promo göstersin diye: Bot paneli → 💬 Menü yanıtları → "
                      "butonu seç → <code>promo</code> yaz."]
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


@router.callback_query(F.data.startswith("itest:"))
async def cb_test_chain(c: CallbackQuery, manager: BotManager):
    """Zinciri, gecikmeleri beklemeden yöneticiye o botun kendisinden gönderir."""
    from .automsg import send_item
    bid = int(c.data.split(":")[1])
    bot = manager.sender(bid)
    items = await list_items(bid, "auto", only_enabled=True)
    async with Session() as s:
        b = await s.get(ManagedBot, bid)
    if not bot or not items:
        await c.answer("Zincirde aktif mesaj yok.", show_alert=True)
        return
    await c.answer("Gönderiliyor…")
    sent, errors = 0, []
    for i, it in enumerate(items, 1):
        try:
            await bot.send_message(c.from_user.id, f"🧪 Test {i}/{len(items)} • normalde: {fmt_delay(it.delay_sec)}")
            if await send_item(bot, c.from_user.id, it) is not None:
                sent += 1
            else:
                errors.append(f"{i}. içerik kütüphanede kapalı")
        except Exception as e:
            err = str(e)
            if "chat not found" in err.lower() or "forbidden" in err.lower():
                await c.message.answer(f"❌ @{html.escape(b.username)} sana mesaj gönderemiyor. Önce o bota kendi "
                                       "hesabından /start yaz, sonra tekrar dene.")
                return
            errors.append(f"{i}. {html.escape(err[:120])}")
    txt = f"🧪 @{html.escape(b.username)} sohbetine {sent}/{len(items)} zincir mesajı gönderildi."
    if errors:
        txt += "\n⚠️ " + "\n⚠️ ".join(errors)
    if b.auto_enabled is False:
        txt += "\n\n⏸ Not: Bu botun zinciri şu an <b>kapalı</b>, yeni abonelere gitmiyor. Listeden ▶️ Başlat."
    await c.message.answer(txt, reply_markup=kb([("⬅️ Zincire dön", f"itl:auto:{bid}")]))


# --- bot ekranından kütüphaneden seç ---
@router.callback_query(F.data.startswith("it+:"))
async def cb_pick(c: CallbackQuery, state: FSMContext):
    await state.clear()
    _, kind, bid = c.data.split(":")
    async with Session() as s:
        libs = (await s.scalars(select(LibItem).order_by(LibItem.id.desc()))).all()
        have = set((await s.scalars(select(Item.lib_id).where(Item.bot_id == int(bid), Item.kind == kind))).all())
    rows = [[(f"{'✅' if l.id in have else ('🟢' if l.enabled else '⏸')} {l.title}"[:40], f"lp:{kind}:{bid}:{l.id}")]
            for l in libs]
    if kind == "promo" and libs:
        rows.append([("✅ Tümünü ekle", f"lpall:{bid}")])
    rows.append([("✍️ Yeni promo yaz", f"lbn:{kind}:{bid}")])
    rows.append([("⬅️ Geri", f"itl:{kind}:{bid}")])
    text = ("Kütüphaneden seç (✅ = zaten ekli):" if libs else "Kütüphane boş. Yeni bir promo yaz:")
    await show(c, text, kb(*rows))


@router.callback_query(F.data.startswith("lp:"))
async def cb_pick_one(c: CallbackQuery, state: FSMContext, manager: BotManager):
    _, kind, bid, lid = c.data.split(":")
    bid, lid = int(bid), int(lid)
    if kind == "auto":
        await state.set_state(LibSt.delay)
        await state.update_data(lib_id=lid, mode="auto", sel=[bid])
        await show(c, DELAY_HELP)
        return
    n = await add_refs(lid, "promo", [bid])
    await _resync(manager, [bid])
    await c.answer("Eklendi ✅" if n else "Zaten ekli")
    await show(c, *(await list_screen("promo", bid)))


@router.callback_query(F.data.startswith("lpall:"))
async def cb_pick_all(c: CallbackQuery, manager: BotManager):
    bid = int(c.data.split(":")[1])
    async with Session() as s:
        ids = (await s.scalars(select(LibItem.id).order_by(LibItem.id))).all()
    for lid in ids:
        await add_refs(lid, "promo", [bid])
    await _resync(manager, [bid])
    await show(c, *(await list_screen("promo", bid)))


@router.callback_query(F.data.startswith("lbn:"))
async def cb_new_from_bot(c: CallbackQuery, state: FSMContext):
    _, kind, bid = c.data.split(":")
    await state.set_state(LibSt.msg)
    await state.update_data(origin={"kind": kind, "bot": int(bid)})
    await show(c, "✍️ Yeni promoyu gönder (metin, foto, video veya albüm). Kütüphaneye de kaydedilir.\nİptal: /iptal")


# --- bottaki tek kayıt ---
async def _item_screen(target, iid: int, new_message=False):
    async with Session() as s:
        it = await s.get(Item, iid)
        lib = await s.get(LibItem, it.lib_id) if it.lib_id else None
        pending = await s.scalar(select(func.count()).where(AutoQueue.item_id == iid)) or 0
    icon, name = NAMES[it.kind]
    title = lib.title if lib else it.title
    lines = [f"{icon} <b>{html.escape(title)}</b>",
             f"Bu botta: {'🟢 Açık' if it.enabled else '⏸ Kapalı'}"]
    if lib and not lib.enabled:
        lines.append("⚠️ Kütüphanede kapalı olduğu için hiçbir botta gönderilmiyor.")
    if it.kind == "auto":
        lines += [f"Zaman: {fmt_delay(it.delay_sec)}", f"📤 Gönderildi: {fmt(it.count)} • ⏳ Bekleyen: {fmt(pending)}"]
    else:
        lines.append(f"👁 Gösterildi: {fmt(it.count)}")
    rows = [[("👁 Önizle", f"itp:{iid}"), ("⏸ Bu botta kapat" if it.enabled else "▶️ Bu botta aç", f"itt:{iid}")]]
    if it.kind == "auto":
        rows.append([("⏱ Zamanı değiştir", f"itd:{iid}")])
    rows.append([("⬆️ Yukarı", f"itu:{iid}"), ("➖ Bu bottan çıkar", f"itx:{iid}")])
    if lib:
        rows.append([("🎁 Kütüphanede aç", f"lb:{lib.id}")])
    rows.append([("⬅️ Liste", f"itl:{it.kind}:{it.bot_id}")])
    text, markup = "\n".join(lines), kb(*rows)
    if new_message:
        await target.message.answer(text, reply_markup=markup)
    else:
        await show(target, text, markup)


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
    if action in ("itt", "itxo") and kind == "promo":
        await _resync(manager, [bid])
    if action == "itxo":
        await show(c, *(await list_screen(kind, bid)))
        return
    if action == "itx":
        await show(c, "➖ Bu bottan çıkarılsın mı? (Kütüphanede kalır.)",
                   kb([("✅ Evet", f"itxo:{iid}"), ("❌ Vazgeç", f"it:{iid}")]))
        return
    if action == "itd":
        await state.set_state(LibSt.edit_delay)
        await state.update_data(item_id=iid)
        await show(c, DELAY_HELP)
        return
    if action == "itp":
        await c.answer()
        from .automsg import content_of
        content = await content_of(it)
        if content:
            await preview(bot, c.message.chat.id, content[0], content[1], btn_text=content[2])
        else:
            await c.message.answer("⏸ İçerik kütüphanede kapalı veya silinmiş.")
    await _item_screen(c, iid, new_message=(action == "itp"))


@router.message(LibSt.edit_delay, F.text)
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
    await m.answer(f"✅ Zaman güncellendi: {fmt_delay(sec)}\n(Kuyrukta bekleyenler eski zamanlarında gider.)",
                   reply_markup=kb([("⬅️ Geri", f"it:{iid}")]))
