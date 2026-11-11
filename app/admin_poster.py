"""Yönetici bot: 🎰 Slotolog Post menüsü (sadece Slotolog kanalı)."""
import html
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from .admin import BACK_HOME, NO_STORAGE, kb, parse_when, show, storage_ok
from .config import ADMIN_IDS, TZ
from .db import Channel, ManagedBot, Session, SlotButtonSet, SlotPost, aware
from .manager import BotManager
from .poster import (GIRIS, MEDIA_KINDS, check_length, rich_from_message, deliver, details_label, giris_post_count, giris_url,
                     parse_post_buttons, post_buttons_text, post_dict, poster_channel_id, publish,
                     refresh_buttons, resolve_buttons, set_setting, uses_giris)
from .settings import storage_id

router = Router()
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

KIND_NAME = {"photo": "🖼 Foto", "video": "🎬 Video", "animation": "🎞 GIF", "document": "📎 Dosya"}
RICH_TIP = ("\n\n📊 Tablolu / başlıklı <b>zengin mesaj</b> da gönderebilirsin; olduğu gibi alınır "
            "(başlık + tablo + görsel + butonlar tek post olur).")
STATUS = {"scheduled": "⏰", "sending": "⏳", "sent": "✅", "failed": "❌", "cancelled": "🚫", "deleted": "🗑"}
BTN_HELP = (
    "Her satır bir buton sırası, <code>|</code> ile yan yana:\n\n"
    "<code>🔴 🤖 ETKİNLİK BOTU - https://t.me/slotolog_bot | 🔴 💬 SOHBET GRUBU - https://t.me/grup\n"
    "🔴 🔗 GÜNCEL GİRİŞ - {giris}</code>\n\n"
    "🟢 yeşil • 🔴 kırmızı • 🔵 mavi (satır başına koy)\n"
    "<code>{giris}</code> = güncel giriş linki. Link değişince eski postlar da tek tuşla güncellenir."
)
BACK_SP = [("⬅️ Slotolog Post", "sp")]


class Sp(StatesGroup):
    media = State()
    title = State()
    details = State()
    buttons = State()
    confirm = State()
    schedule = State()
    set_name = State()
    giris = State()
    label = State()
    new_set = State()
    edit_btns = State()


def fmt_dt(dt) -> str:
    return aware(dt).astimezone(TZ).strftime("%d.%m %H:%M") if dt else "—"


def post_title(title: str, rich: dict | None) -> str:
    return title or ("📊 " + (rich or {}).get("summary", "") if rich else "")


async def take_rich(m: Message) -> dict | None:
    try:
        return await rich_from_message(m.bot, m.rich_message)
    except Exception as e:
        await m.answer(f"❌ Zengin mesaj alınamadı: {html.escape(str(e)[:300])}")
        return None


def short(text: str, n=32) -> str:
    import re
    t = html.unescape(re.sub(r"<[^>]+>", "", text or "")).replace("\n", " ").strip()
    return (t[:n] + "…") if len(t) > n else (t or "(başlıksız)")


# ---------------- ana ekran ----------------
async def main_screen():
    cid = await poster_channel_id()
    giris = await giris_url()
    async with Session() as s:
        ch = await s.get(Channel, cid) if cid else None
        bot = await s.get(ManagedBot, ch.bot_id) if ch else None
        n_sch = await s.scalar(select(func.count()).select_from(SlotPost).where(SlotPost.status == "scheduled"))
        n_sent = await s.scalar(select(func.count()).select_from(SlotPost).where(SlotPost.status == "sent"))
        n_sets = await s.scalar(select(func.count()).select_from(SlotButtonSet))
    lines = ["🎰 <b>Slotolog Post</b>", "",
             "Görsel + başlık + açılır detay + butonlu postlar, sadece Slotolog kanalına.", ""]
    if ch:
        lines.append(f"📡 Kanal: <b>{html.escape(ch.title)}</b> — @{html.escape(bot.username if bot else '?')}")
    else:
        lines.append("📡 Kanal: ⚠️ <b>seçilmedi</b> (⚙️ Ayarlar)")
    lines += [f"🔗 Güncel giriş: {html.escape(giris) if giris else '⚠️ ayarlı değil'}",
              f"📋 Detay başlığı: {html.escape(await details_label()) or '(yok)'}",
              f"🔘 Buton seti: {n_sets} • ⏰ Zamanlanmış: {n_sch} • ✅ Paylaşılan: {n_sent}"]
    rows = [[("✍️ Yeni post", "spnew")],
            [("⏰ Zamanlanmış", "spls:s"), ("📑 Son postlar", "spls:d")],
            [("🔘 Buton setleri", "spsets"), ("🔗 Giriş linki", "spgir")],
            [("⚙️ Ayarlar", "spcfg")],
            BACK_HOME]
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data == "sp")
async def cb_main(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, *(await main_screen()))


# ---------------- ayarlar ----------------
@router.callback_query(F.data == "spcfg")
async def cb_cfg(c: CallbackQuery, state: FSMContext):
    await state.clear()
    cid = await poster_channel_id()
    async with Session() as s:
        chans = (await s.scalars(select(Channel).order_by(Channel.added_at))).all()
        names = {b.id: b.username for b in (await s.scalars(select(ManagedBot))).all()}
    lines = ["⚙️ <b>Slotolog Post ayarları</b>", "", "<b>Kanal seç</b> (✅ = seçili):"]
    rows = []
    for ch in chans:
        mark = "✅" if ch.id == cid else "▫️"
        rows.append([(f"{mark} {ch.title} — @{names.get(ch.bot_id, '?')}"[:60], f"spch:{ch.id}")])
    if not chans:
        lines.append("<i>Kayıtlı kanal yok.</i>")
    lines += ["", "Listede yoksa: @slotologpostbot'u önce ➕ Bot Ekle ile bağla, sonra Slotolog kanalına "
                  "<b>admin</b> yap (mesaj gönderme + düzenleme + silme yetkisi). Kanal otomatik görünür.",
              "Bot ayrıca depo kanalında da admin olmalı.", "",
              "Seçilen kanal genel <b>Kanal Paylaşımları</b> akışından çıkarılır; oraya sadece bu menüden post gider."]
    rows.append([("📋 Detay başlığını değiştir", "splbl")])
    rows.append(BACK_SP)
    await show(c, "\n".join(lines), kb(*rows))


@router.callback_query(F.data.startswith("spch:"))
async def cb_set_channel(c: CallbackQuery, state: FSMContext):
    await set_setting("sp_channel", c.data.split(":")[1])
    await c.answer("Kanal seçildi ✅")
    await cb_cfg(c, state)


@router.callback_query(F.data == "splbl")
async def cb_label(c: CallbackQuery, state: FSMContext):
    await state.set_state(Sp.label)
    await show(c, f"📋 Açılır detay kutusunun ilk satırı. Şu an: <b>{html.escape(await details_label()) or '(yok)'}</b>\n\n"
                  "Yeni başlığı yaz (ör. <code>⌄ DETAYLAR</code>). Başlık istemiyorsan <code>-</code>\nİptal: /iptal")


@router.message(Sp.label, F.text)
async def on_label(m: Message, state: FSMContext):
    await set_setting("sp_label", m.text.strip()[:60])
    await state.clear()
    await m.answer("✅ Detay başlığı kaydedildi.", reply_markup=kb(BACK_SP))


# ---------------- giriş linki ----------------
@router.callback_query(F.data == "spgir")
async def cb_giris(c: CallbackQuery, state: FSMContext):
    await state.set_state(Sp.giris)
    g = await giris_url()
    await show(c, f"🔗 Güncel giriş linki: {html.escape(g) if g else '<i>ayarlı değil</i>'}\n\n"
                  "Yeni linki gönder (ör. <code>https://slotolog123.com</code>).\n"
                  "Butonlarda <code>{giris}</code> yazan her yer bu linke gider.\nİptal: /iptal",
               kb(BACK_SP))


@router.message(Sp.giris, F.text)
async def on_giris(m: Message, state: FSMContext):
    url = m.text.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if " " in url or "." not in url:
        await m.answer("❌ Geçerli bir link yaz. Örnek: <code>https://slotolog123.com</code>")
        return
    await set_setting("sp_giris", url[:500])
    await state.clear()
    n = await giris_post_count()
    rows = [[(f"🔁 Eski postları da güncelle ({n})", "spgirup")]] if n else []
    await m.answer(f"✅ Güncel giriş: {html.escape(url)}\n\nBundan sonraki postlar bu linki kullanır."
                   + (f"\n{n} paylaşılmış postta <code>{{giris}}</code> butonu var." if n else ""),
                   reply_markup=kb(*rows, BACK_SP))


@router.callback_query(F.data == "spgirup")
async def cb_giris_update(c: CallbackQuery, manager: BotManager):
    await c.answer("Güncelleniyor…")
    await c.message.edit_text("⏳ Eski postların butonları güncelleniyor…")
    ok, fail = await refresh_buttons(manager)
    await c.message.edit_text(f"✅ {ok} postun butonu güncellendi" + (f", ❌ {fail} hata" if fail else "") + ".",
                              reply_markup=kb(BACK_SP))


# ---------------- buton setleri ----------------
@router.callback_query(F.data == "spsets")
async def cb_sets(c: CallbackQuery, state: FSMContext):
    await state.clear()
    async with Session() as s:
        sets = (await s.scalars(select(SlotButtonSet).order_by(SlotButtonSet.id))).all()
    lines = ["🔘 <b>Buton setleri</b>", "", "Post hazırlarken tek dokunuşla seçilir."]
    rows = []
    for bs in sets:
        lines.append(f"\n<b>{html.escape(bs.name)}</b>\n<code>{html.escape(post_buttons_text(bs.rows))}</code>")
        rows.append([(f"🔘 {bs.name}"[:40], f"spsv:{bs.id}"), ("🗑", f"spsx:{bs.id}")])
    if not sets:
        lines.append("\n<i>Henüz set yok.</i>")
    rows.append([("➕ Yeni set", "spsadd")])
    rows.append(BACK_SP)
    await show(c, "\n".join(lines), kb(*rows))


@router.callback_query(F.data.startswith("spsx:"))
async def cb_set_del(c: CallbackQuery, state: FSMContext):
    async with Session() as s:
        bs = await s.get(SlotButtonSet, int(c.data.split(":")[1]))
        if bs:
            await s.delete(bs)
            await s.commit()
    await c.answer("Silindi")
    await cb_sets(c, state)


@router.callback_query(F.data.startswith("spsv:"))
async def cb_set_view(c: CallbackQuery):
    async with Session() as s:
        bs = await s.get(SlotButtonSet, int(c.data.split(":")[1]))
    if not bs:
        await c.answer("Bulunamadı")
        return
    await c.answer()
    try:
        markup = resolve_buttons(bs.rows, await giris_url() or "https://example.com")
        from .keyboards import build_markup
        await c.message.answer(f"🔘 <b>{html.escape(bs.name)}</b> önizleme:", reply_markup=build_markup(markup))
    except Exception as e:
        await c.message.answer(f"❌ {html.escape(str(e))}")


@router.callback_query(F.data == "spsadd")
async def cb_set_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(Sp.new_set)
    await show(c, "➕ <b>Yeni buton seti</b>\n\nİlk satıra setin adını, altına butonları yaz:\n\n"
                  "<code>Standart\n🔴 🤖 ETKİNLİK BOTU - https://t.me/slotolog_bot | 🔴 💬 SOHBET GRUBU - https://t.me/grup\n"
                  "🔴 🔗 GÜNCEL GİRİŞ - {giris}</code>\n\n" + BTN_HELP + "\n\nİptal: /iptal")


@router.message(Sp.new_set, F.text)
async def on_new_set(m: Message, state: FSMContext):
    lines = m.text.strip().splitlines()
    if len(lines) < 2:
        await m.answer("❌ İlk satır set adı, altındaki satırlar butonlar olmalı.")
        return
    try:
        rows = parse_post_buttons("\n".join(lines[1:]))
    except ValueError as e:
        await m.answer(f"❌ {html.escape(str(e))}")
        return
    if not rows:
        await m.answer("❌ En az bir buton yaz.")
        return
    async with Session() as s:
        s.add(SlotButtonSet(name=lines[0].strip()[:64], rows=rows))
        await s.commit()
    await state.clear()
    await m.answer(f"✅ «{html.escape(lines[0].strip())}» seti kaydedildi.",
                   reply_markup=kb([("🔘 Buton setleri", "spsets")], BACK_SP))


# ---------------- yeni post ----------------
async def ask_media(target, state: FSMContext):
    await state.set_state(Sp.media)
    await show(target, "🖼 <b>1/4 Görsel</b>\n\nPostun görselini gönder (foto, video veya GIF).\n"
                       "Görselin altına yazı eklersen <b>başlık</b> olarak alınır.\n"
                       "Görselsiz post için <code>-</code>" + RICH_TIP + "\n\nİptal: /iptal")


@router.callback_query(F.data == "spnew")
async def cb_new(c: CallbackQuery, state: FSMContext):
    if not storage_ok():
        await show(c, NO_STORAGE, kb(BACK_SP))
        return
    if not await poster_channel_id():
        await c.answer("Önce ⚙️ Ayarlar'dan Slotolog kanalını seç.", show_alert=True)
        return
    await state.clear()
    await state.update_data(p={"above": True, "pin": False, "silent": False}, editing=False)
    await ask_media(c, state)


@router.message(Sp.media)
async def on_media(m: Message, state: FSMContext):
    data = await state.get_data()
    p = data["p"]
    if m.rich_message:
        rich = await take_rich(m)
        if rich is None:
            return
        if rich["summary"]:
            # yazılı zengin mesaj: postun tüm metni bu (görsel varsa içinde)
            p.update(rich=rich, title="", details=None, media_msg=None, media_kind=None)
            await state.update_data(p=p)
            if data.get("editing"):
                await to_confirm(m, state)
            else:
                await ask_buttons(m, state)
            return
        media = [b for b in rich["blocks"] if isinstance(b, dict) and "_store" in b]
        if not media:
            await m.answer("❌ Bu mesajda görsel bulamadım. Foto, video veya GIF gönder.")
            return
        p["media_msg"], p["media_kind"] = media[0]["_store"], media[0]["type"]
    elif m.text and m.text.strip() == "-":
        p["media_msg"], p["media_kind"] = None, None
    else:
        kind = "photo" if m.photo else "video" if m.video else "animation" if m.animation else None
        if not kind and m.document and (m.document.mime_type or "").startswith(("image/", "video/")):
            kind = "document"
        if not kind or m.media_group_id:
            await m.answer("❌ Tek bir foto, video veya GIF gönder (albüm olmaz). Görselsiz için <code>-</code>")
            return
        try:
            res = await m.bot.copy_message(chat_id=storage_id(), from_chat_id=m.chat.id, message_id=m.message_id)
        except Exception as e:
            await m.answer(f"❌ Depo kanalına kaydedilemedi: {html.escape(str(e))}")
            return
        p["media_msg"], p["media_kind"] = res.message_id, kind
        if m.caption and not data.get("editing"):
            p["title"] = m.html_text if m.caption_entities else f"<b>{m.html_text}</b>"
    await state.update_data(p=p)
    if data.get("editing"):
        await to_confirm(m, state)
    elif p.get("title"):
        await ask_details(m, state)
    else:
        await ask_title(m, state)


async def ask_title(target, state: FSMContext):
    await state.set_state(Sp.title)
    await show(target, "📝 <b>2/4 Başlık</b>\n\nBaşlığı yaz. Örnek:\n<code>🌙 GECE YATIRIMLARINA ÖZEL 2X ÇARK HAKKI! 🎡</code>\n\n"
                       "Biçim vermezsen otomatik <b>kalın</b> olur. Kalın/italik ve premium emoji kullanabilirsin."
               + RICH_TIP)


@router.message(Sp.title, F.text | F.rich_message)
async def on_title(m: Message, state: FSMContext):
    data = await state.get_data()
    p = data["p"]
    if m.rich_message:
        rich = await take_rich(m)
        if rich is None:
            return
        p.update(rich=rich, title="", details=None)
        await state.update_data(p=p)
        if data.get("editing"):
            await to_confirm(m, state)
        else:
            await ask_buttons(m, state)
        return
    p["title"] = m.html_text if m.entities else f"<b>{m.html_text}</b>"
    await state.update_data(p=p)
    if data.get("editing"):
        await to_confirm(m, state)
    else:
        await ask_details(m, state)


async def ask_details(target, state: FSMContext):
    await state.set_state(Sp.details)
    label = await details_label()
    await show(target, f"📋 <b>3/4 Detaylar</b>\n\nKampanya detaylarını yaz. Postta <b>{html.escape(label) or 'açılır kutu'}</b> "
                       "olarak kapalı durur, tıklayınca açılır.\n\nDetay istemiyorsan <code>-</code>"
               + "\n\n📊 Tablo gerekiyorsa detayı <b>zengin mesaj</b> olarak gönder; başlığın üstüne eklenir.")


@router.message(Sp.details, F.text | F.rich_message)
async def on_details(m: Message, state: FSMContext):
    data = await state.get_data()
    p = data["p"]
    if m.rich_message:
        rich = await take_rich(m)
        if rich is None:
            return
        p["rich"], p["details"] = rich, None
    else:
        p["details"] = None if m.text.strip() == "-" else m.html_text
        p["rich"] = None
    await state.update_data(p=p)
    if data.get("editing"):
        await to_confirm(m, state)
    else:
        await ask_buttons(m, state)


async def ask_buttons(target, state: FSMContext):
    await state.set_state(Sp.buttons)
    async with Session() as s:
        sets = (await s.scalars(select(SlotButtonSet).order_by(SlotButtonSet.id))).all()
    rows = [[(f"🔘 {bs.name}"[:40], f"spbs:{bs.id}")] for bs in sets]
    rows.append([("🚫 Butonsuz", "spbn")])
    await show(target, "🔘 <b>4/4 Butonlar</b>\n\n" + ("Hazır setlerden birini seç ya da butonları yaz.\n\n" if sets else "")
               + BTN_HELP, kb(*rows))


@router.callback_query(Sp.buttons, F.data.startswith("spbs:") | (F.data == "spbn"))
async def cb_pick_set(c: CallbackQuery, state: FSMContext):
    rows = []
    if c.data != "spbn":
        async with Session() as s:
            bs = await s.get(SlotButtonSet, int(c.data.split(":")[1]))
        rows = bs.rows if bs else []
    data = await state.get_data()
    p = data["p"]
    p["buttons"] = rows
    await state.update_data(p=p)
    await c.answer()
    await to_confirm(c.message, state)


@router.message(Sp.buttons, F.text)
async def on_buttons(m: Message, state: FSMContext):
    try:
        rows = parse_post_buttons(m.text)
    except ValueError as e:
        await m.answer(f"❌ {html.escape(str(e))}")
        return
    data = await state.get_data()
    p = data["p"]
    p["buttons"] = rows
    await state.update_data(p=p)
    await to_confirm(m, state)


# ---------------- önizleme & onay ----------------
async def send_preview(admin_bot: Bot, user_id: int, p: dict, manager: BotManager | None) -> str | None:
    """Önce kanal botuyla (premium emoji dahil birebir görünüm), olmazsa yönetici botla gönderir."""
    label, giris = await details_label(), await giris_url() or "https://example.com"
    bots = []
    if manager:
        cid = await poster_channel_id()
        async with Session() as s:
            ch = await s.get(Channel, cid) if cid else None
        if ch and manager.sender(ch.bot_id):
            bots.append(manager.sender(ch.bot_id))
    bots.append(admin_bot)
    err = ""
    for b in bots:
        try:
            await deliver(b, user_id, p, label, giris)
            return None
        except Exception as e:
            err = str(e)
    return err


async def confirm_screen(state: FSMContext):
    data = await state.get_data()
    p = data["p"]
    label = await details_label()
    warn = check_length(p, label)
    giris_warn = uses_giris(p.get("buttons")) and not await giris_url()
    lines = ["🎰 <b>Post hazır</b>", "",
             f"🖼 Görsel: {KIND_NAME.get(p.get('media_kind'), 'yok')}",
             f"📝 Başlık: {html.escape(short(post_title(p.get('title', ''), p.get('rich')), 50))}",
             f"📋 Detay: {'📊 zengin mesaj (tablo vb.)' if p.get('rich') else 'var' if p.get('details') else 'yok'}",
             f"🔘 Buton: {sum(len(r) for r in p.get('buttons') or [])}"]
    if warn:
        lines += ["", warn]
    if giris_warn:
        lines += ["", "⚠️ Butonlarda {giris} var ama giriş linki ayarlı değil (🔗 Giriş linki)."]
    if data.get("from_id"):
        lines += ["", f"♻️ #{data['from_id']} postundan kopyalandı."]
    rows = []
    if not warn and not giris_warn:
        rows.append([("🚀 Şimdi paylaş", "spgo"), ("⏰ Zamanla", "spsch")])
    rows += [[("👁 Önizle", "spprev")],
             [("🖼 Görsel", "spe:media"), ("📝 Başlık", "spe:title")],
             [("📋 Detay", "spe:details"), ("🔘 Butonlar", "spe:buttons")]]
    if p.get("media_msg") and p.get("media_kind") in MEDIA_KINDS:
        rows.append([("⬆️ Yazı görselin üstünde" if p.get("above", True) else "⬇️ Yazı görselin altında", "spt:above")])
    rows.append([(f"📌 Sabitle: {'Açık' if p.get('pin') else 'Kapalı'}", "spt:pin"),
                 (f"🔕 Sessiz: {'Açık' if p.get('silent') else 'Kapalı'}", "spt:silent")])
    if p.get("buttons"):
        rows.append([("💾 Butonları set olarak kaydet", "spsave")])
    rows.append([("❌ Vazgeç", "sp")])
    return "\n".join(lines), kb(*rows)


async def to_confirm(m: Message, state: FSMContext, preview=True):
    await state.set_state(Sp.confirm)
    await state.update_data(editing=True)
    if preview:
        from .settings import RUNTIME
        data = await state.get_data()
        err = await send_preview(m.bot, m.chat.id, data["p"], RUNTIME.get("manager"))
        if err:
            await m.answer(f"⚠️ Önizleme gönderilemedi: {html.escape(err[:300])}")
    await m.answer(*(await confirm_screen(state)))


@router.callback_query(Sp.confirm, F.data == "spprev")
async def cb_preview(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await to_confirm(c.message, state)


@router.callback_query(Sp.confirm, F.data.startswith("spt:"))
async def cb_toggle(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[1]
    data = await state.get_data()
    p = data["p"]
    p[key] = not p.get(key, key == "above")
    await state.update_data(p=p)
    await show(c, *(await confirm_screen(state)))


@router.callback_query(Sp.confirm, F.data.startswith("spe:"))
async def cb_edit(c: CallbackQuery, state: FSMContext):
    await c.answer()
    what = c.data.split(":")[1]
    if what == "media":
        await ask_media(c.message, state)
    elif what == "title":
        await ask_title(c.message, state)
    elif what == "details":
        await ask_details(c.message, state)
    else:
        await ask_buttons(c.message, state)


@router.callback_query(Sp.confirm, F.data == "spsave")
async def cb_save_set(c: CallbackQuery, state: FSMContext):
    await state.set_state(Sp.set_name)
    await show(c, "💾 Bu buton setine bir ad ver (ör. <code>Standart</code>):")


@router.message(Sp.set_name, F.text)
async def on_set_name(m: Message, state: FSMContext):
    data = await state.get_data()
    async with Session() as s:
        s.add(SlotButtonSet(name=m.text.strip()[:64], rows=data["p"].get("buttons") or []))
        await s.commit()
    await m.answer("✅ Set kaydedildi.")
    await to_confirm(m, state, preview=False)


async def _save_post(state: FSMContext, when: datetime | None) -> int:
    data = await state.get_data()
    p = data["p"]
    async with Session() as s:
        sp = SlotPost(channel_id=await poster_channel_id(), media_msg=p.get("media_msg"), media_kind=p.get("media_kind"),
                      title=p.get("title", ""), details=p.get("details"), rich=p.get("rich"),
                      buttons=p.get("buttons") or [],
                      above=p.get("above", True), pin=p.get("pin", False), silent=p.get("silent", False),
                      status="scheduled",
                      scheduled_at=when.astimezone(timezone.utc) if when else None)
        s.add(sp)
        await s.commit()
        return sp.id


@router.callback_query(Sp.confirm, F.data == "spgo")
async def cb_go(c: CallbackQuery, state: FSMContext, manager: BotManager):
    pid = await _save_post(state, None)
    await state.clear()
    await c.answer("Paylaşılıyor…")
    ok, err = await publish(manager, pid)
    if ok:
        await show(c, f"✅ Post #{pid} Slotolog kanalına paylaşıldı.",
                   kb([("✍️ Yeni post", "spnew"), ("📑 Son postlar", "spls:d")], BACK_SP))
    else:
        await show(c, f"❌ Paylaşılamadı: {html.escape(err)}", kb([("📑 Son postlar", "spls:d")], BACK_SP))


@router.callback_query(Sp.confirm, F.data == "spsch")
async def cb_sch(c: CallbackQuery, state: FSMContext):
    await state.set_state(Sp.schedule)
    await show(c, "⏰ Ne zaman paylaşılsın? (Türkiye saati)\n\n<code>00:05</code> → bugün (geçtiyse yarın)\n"
                  "<code>12.11 20:30</code>\n\nGeri dönmek için /iptal değil, aşağıdaki düğme:",
               kb([("⬅️ Posta dön", "spback")]))


@router.callback_query(Sp.schedule, F.data == "spback")
async def cb_sch_back(c: CallbackQuery, state: FSMContext):
    await state.set_state(Sp.confirm)
    await show(c, *(await confirm_screen(state)))


@router.message(Sp.schedule, F.text)
async def on_sch(m: Message, state: FSMContext):
    when = parse_when(m.text)
    if not when or when <= datetime.now(TZ):
        await m.answer("❌ Tarih anlaşılamadı veya geçmişte. Örnek: <code>00:05</code> ya da <code>12.11 20:30</code>")
        return
    pid = await _save_post(state, when)
    await state.clear()
    await m.answer(f"⏰ Post #{pid} zamanlandı: <b>{when:%d.%m.%Y %H:%M}</b>",
                   reply_markup=kb([("⏰ Zamanlanmış", "spls:s"), ("✍️ Yeni post", "spnew")], BACK_SP))


# ---------------- listeler ----------------
@router.callback_query(F.data.startswith("spls:"))
async def cb_list(c: CallbackQuery, state: FSMContext):
    await state.clear()
    sched = c.data.endswith(":s")
    async with Session() as s:
        q = select(SlotPost)
        if sched:
            q = q.where(SlotPost.status == "scheduled").order_by(SlotPost.scheduled_at)
        else:
            q = q.where(SlotPost.status != "scheduled").order_by(SlotPost.id.desc()).limit(20)
        posts = (await s.scalars(q)).all()
    title = "⏰ <b>Zamanlanmış postlar</b>" if sched else "📑 <b>Son postlar</b>"
    rows = []
    for sp in posts:
        when = fmt_dt(sp.scheduled_at if sched else (sp.sent_at or sp.created_at))
        rows.append([(f"{STATUS.get(sp.status, '')} #{sp.id} {when} {short(post_title(sp.title, sp.rich), 22)}"[:60], f"spv:{sp.id}")])
    if not posts:
        title += "\n\n<i>Liste boş.</i>"
    rows.append(BACK_SP)
    await show(c, title, kb(*rows))


async def post_view(pid: int):
    async with Session() as s:
        sp = await s.get(SlotPost, pid)
    if not sp:
        return "Post bulunamadı.", kb(BACK_SP)
    lines = [f"🎰 <b>Post #{sp.id}</b> {STATUS.get(sp.status, '')}", "",
             f"📝 {html.escape(short(post_title(sp.title, sp.rich), 60))}",
             f"🖼 {KIND_NAME.get(sp.media_kind, 'Görselsiz')} • 🔘 {sum(len(r) for r in sp.buttons or [])} buton"]
    if sp.status == "scheduled":
        lines.append(f"⏰ Paylaşım: {fmt_dt(sp.scheduled_at)}")
    if sp.sent_at:
        lines.append(f"✅ Paylaşıldı: {fmt_dt(sp.sent_at)}")
    if sp.error and sp.status == "failed":
        lines.append(f"❌ Hata: {html.escape(sp.error)}")
    if sp.buttons:
        lines += ["", f"<code>{html.escape(post_buttons_text(sp.buttons))}</code>"]
    rows = [[("👁 Önizle", f"spvp:{pid}"), ("♻️ Kopyala / tekrar kullan", f"spcp:{pid}")]]
    if sp.status == "scheduled":
        rows.append([("🚀 Şimdi paylaş", f"spvg:{pid}"), ("🚫 İptal et", f"spvc:{pid}")])
    if sp.status == "sent":
        rows.append([("🔘 Butonları değiştir", f"spvb:{pid}"), ("🗑 Kanaldan sil", f"spvd:{pid}")])
    if sp.status == "failed":
        rows.append([("🔁 Tekrar dene", f"spvr:{pid}")])
    back = "spls:s" if sp.status == "scheduled" else "spls:d"
    rows.append([("⬅️ Liste", back)] + BACK_SP)
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data.startswith("spv:"))
async def cb_view(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, *(await post_view(int(c.data.split(":")[1]))))


@router.callback_query(F.data.startswith("spvp:"))
async def cb_view_prev(c: CallbackQuery, manager: BotManager):
    async with Session() as s:
        sp = await s.get(SlotPost, int(c.data.split(":")[1]))
    await c.answer()
    if sp:
        err = await send_preview(c.bot, c.from_user.id, post_dict(sp), manager)
        if err:
            await c.message.answer(f"⚠️ Önizleme gönderilemedi: {html.escape(err[:300])}")


@router.callback_query(F.data.startswith("spcp:"))
async def cb_copy(c: CallbackQuery, state: FSMContext):
    async with Session() as s:
        sp = await s.get(SlotPost, int(c.data.split(":")[1]))
    if not sp:
        await c.answer("Bulunamadı")
        return
    await c.answer()
    await state.clear()
    await state.update_data(p=post_dict(sp), editing=True, from_id=sp.id)
    await to_confirm(c.message, state)


@router.callback_query(F.data.startswith(("spvg:", "spvc:", "spvr:")))
async def cb_view_action(c: CallbackQuery, manager: BotManager):
    action, pid = c.data.split(":")
    pid = int(pid)
    if action == "spvc":
        async with Session() as s:
            sp = await s.get(SlotPost, pid)
            if sp and sp.status == "scheduled":
                sp.status = "cancelled"
                await s.commit()
        await c.answer("İptal edildi")
    else:
        if action == "spvr":
            async with Session() as s:
                sp = await s.get(SlotPost, pid)
                if sp and sp.status == "failed":
                    sp.status = "scheduled"
                    await s.commit()
        await c.answer("Paylaşılıyor…")
        ok, err = await publish(manager, pid)
        if not ok:
            await c.message.answer(f"❌ {html.escape(err)}")
    await show(c, *(await post_view(pid)))


@router.callback_query(F.data.startswith("spvd:"))
async def cb_view_del(c: CallbackQuery):
    pid = c.data.split(":")[1]
    await show(c, f"🗑 Post #{pid} kanaldan silinsin mi?", kb([("✅ Evet, sil", f"spvdo:{pid}"), ("❌ Vazgeç", f"spv:{pid}")]))


@router.callback_query(F.data.startswith("spvdo:"))
async def cb_view_del_ok(c: CallbackQuery, manager: BotManager):
    from .poster import channel_bot
    pid = int(c.data.split(":")[1])
    async with Session() as s:
        sp = await s.get(SlotPost, pid)
    if sp and sp.message_id:
        _, bot = await channel_bot(manager, sp.channel_id)
        try:
            await bot.delete_message(sp.channel_id, sp.message_id)
        except Exception as e:
            await c.answer(f"Silinemedi: {str(e)[:150]}", show_alert=True)
            return
        async with Session() as s:
            sp = await s.get(SlotPost, pid)
            sp.status = "deleted"
            await s.commit()
    await c.answer("Silindi")
    await show(c, *(await post_view(pid)))


@router.callback_query(F.data.startswith("spvb:"))
async def cb_view_btns(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[1])
    await state.set_state(Sp.edit_btns)
    await state.update_data(pid=pid)
    async with Session() as s:
        sets = (await s.scalars(select(SlotButtonSet).order_by(SlotButtonSet.id))).all()
    rows = [[(f"🔘 {bs.name}"[:40], f"spvbs:{bs.id}")] for bs in sets]
    rows.append([("⬅️ Vazgeç", f"spv:{pid}")])
    await show(c, "🔘 Paylaşılmış postun butonları değişecek (kanalda anında güncellenir).\n\n"
                  "Set seç ya da yeni butonları yaz. Butonları kaldırmak için <code>-</code>\n\n" + BTN_HELP, kb(*rows))


async def _apply_btns(state: FSMContext, manager: BotManager, rows) -> str:
    pid = (await state.get_data())["pid"]
    async with Session() as s:
        sp = await s.get(SlotPost, pid)
        sp.buttons = rows
        await s.commit()
    await state.clear()
    try:
        ok, fail = await refresh_buttons(manager, [pid])
    except ValueError as e:
        return f"❌ {html.escape(str(e))}"
    return "✅ Butonlar güncellendi." if ok else "❌ Kanalda güncellenemedi (bot yetkisini kontrol et)."


@router.callback_query(Sp.edit_btns, F.data.startswith("spvbs:"))
async def cb_view_btns_set(c: CallbackQuery, state: FSMContext, manager: BotManager):
    pid = (await state.get_data())["pid"]
    async with Session() as s:
        bs = await s.get(SlotButtonSet, int(c.data.split(":")[1]))
    msg = await _apply_btns(state, manager, bs.rows if bs else [])
    await c.answer()
    await c.message.answer(msg, reply_markup=kb([("⬅️ Posta dön", f"spv:{pid}")]))


@router.message(Sp.edit_btns, F.text)
async def on_view_btns(m: Message, state: FSMContext, manager: BotManager):
    pid = (await state.get_data())["pid"]
    try:
        rows = parse_post_buttons(m.text)
    except ValueError as e:
        await m.answer(f"❌ {html.escape(str(e))}")
        return
    msg = await _apply_btns(state, manager, rows)
    await m.answer(msg, reply_markup=kb([("⬅️ Posta dön", f"spv:{pid}")]))
