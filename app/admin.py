"""Yönetici bot: tüm kontrol Telegram içinden."""
import asyncio
import csv
import html
import io
import logging
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton as IB,
                           InlineKeyboardMarkup, Message, ReplyKeyboardRemove)
from sqlalchemy import delete, select

from .tg import make_bot
from .broadcast import BroadcastRunner, active_user_ids, fmt
from .config import ADMIN_IDS, PUBLIC_URL, TZ
from .settings import set_storage, storage_id
from .db import Broadcast, Delivery, ManagedBot, Session, Subscriber, aware, utcnow
from .content import send_content
from .db import AutoQueue, Item, Link
from .keyboards import (buttons_to_text, build_markup, parse_buttons, split_style,
                        parse_menu, split_btn_text)
from .manager import BotManager
from .stats import bot_detail_text, overview_text, report_text

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

BUTTON_HELP = (
    "Butonları şu formatta yaz (her satır bir sıra, <code>|</code> ile yan yana):\n\n"
    "<code>🟢 Hemen Katıl - https://site.com\n"
    "🔴 Son Gün! - https://site.com/bonus | 🔵 Kanal - https://t.me/kanal</code>\n\n"
    "🟢 yeşil • 🔴 kırmızı • 🔵 mavi (renk istemezsen emoji koyma)\n"
    "Buton istemiyorsan <code>-</code> yaz."
)
ALBUM_HELP = ("\n\n📎 Albüm gönderdin: Telegram albümlere buton eklemeye izin vermez, butonlar albümün "
              "hemen altına ayrı bir mesajla gider. O mesajın yazısını ilk satıra "
              "<code>Yazı: Fırsatı kaçırma 👇</code> şeklinde yazabilirsin.")
_albums: dict[str, list[Message]] = {}


class AddBot(StatesGroup):
    token = State()


class Welcome(StatesGroup):
    msg = State()
    buttons = State()


class MenuSt(StatesGroup):
    layout = State()
    resp_msg = State()
    resp_buttons = State()


class Bc(StatesGroup):
    select = State()
    msg = State()
    buttons = State()
    confirm = State()
    schedule = State()


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[IB(text=t, callback_data=d) for t, d in row] for row in rows])


HOME = kb([("🤖 Botlarım", "bots"), ("➕ Bot Ekle", "addbot")],
          [("📢 Yayın Yap", "bc:new"), ("📊 Analiz", "stats")],
          [("🎁 Promolar", "lib"), ("📨 Otomatik Mesajlar", "autoall")],
          [("📢 Kanal Paylaşımları", "ch"), ("📑 Yayın Raporları", "reports")],
          [("🎰 Slotolog Post", "sp")],
          [("🖥 Sistem durumu", "sys")])
BACK_HOME = [("🏠 Ana menü", "home")]


async def show(target: Message | CallbackQuery, text: str, markup=None):
    if isinstance(target, CallbackQuery):
        await target.answer()
        try:
            await target.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
            return
        except TelegramBadRequest:
            target = target.message
    await target.answer(text, reply_markup=markup, disable_web_page_preview=True)


def storage_ok() -> bool:
    return storage_id() != 0


NO_STORAGE = ("⚠️ Depo kanalı ayarlı değil.\n\nÖzel bir kanal aç, bu botu ve tüm bağlı botları "
              "kanala <b>admin</b> yap, sonra kanala <code>/depo</code> yaz. Kanal otomatik ayarlanır.")


# ---------------- ana menü ----------------
@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    if not storage_ok():
        await m.answer("👋 <b>Kurulum için son adım</b>\n\n" + NO_STORAGE)
    # önceki sürümden kalmış olabilecek alt klavye önizlemesini temizle
    await m.answer("🎛 <b>Bot Kontrol Merkezi</b>", reply_markup=ReplyKeyboardRemove())
    await m.answer("Ne yapmak istersin?", reply_markup=HOME)


@router.message(Command("durum"))
async def cmd_status(m: Message):
    from .health import status_text
    await m.answer(await status_text(), reply_markup=kb([("🔄 Yenile", "sys")], BACK_HOME))


@router.callback_query(F.data == "sys")
async def cb_status(c: CallbackQuery):
    from .health import status_text
    await show(c, await status_text(), kb([("🔄 Yenile", "sys")], BACK_HOME))


@router.message(Command("iptal"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("İptal edildi.", reply_markup=HOME)


@router.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "🎛 <b>Bot Kontrol Merkezi</b>\nNe yapmak istersin?", HOME)


# ---------------- bot ekleme ----------------
@router.callback_query(F.data == "addbot")
async def cb_addbot(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddBot.token)
    await show(c, "➕ Eklemek istediğin botun <b>token</b>'ını gönder.\n(BotFather → /mybots → API Token)\n\n"
                  "İptal: /iptal")


@router.message(AddBot.token, F.text)
async def on_token(m: Message, state: FSMContext, manager: BotManager):
    token = m.text.strip()
    try:
        await m.delete()  # token sohbette kalmasın
    except Exception:
        pass
    test = make_bot(token)
    try:
        me = await test.get_me()
    except Exception:
        await test.session.close()
        await m.answer("❌ Token geçersiz. Tekrar gönder veya /iptal.")
        return
    warn = ""
    try:
        wh = await test.get_webhook_info()
        if wh.url:
            warn += ("\n\n⚠️ Bu botta başka bir sistemin <b>webhook</b>'u ayarlı. Bu sistem botu dinleyemez. "
                     "Bot panelinde <b>🩺 Teşhis → 🔧 Webhook'u kaldır</b>'a bas.")
    except Exception:
        pass
    if storage_ok():
        try:
            await test.get_chat(storage_id())
        except Exception:
            warn += ("\n\n⚠️ Bu bot depo kanalına erişemiyor. Medyalı yayın ve karşılama mesajı için "
                    f"@{me.username} botunu depo kanalına <b>admin</b> olarak ekle.")
    await test.session.close()
    async with Session() as s:
        row = await s.get(ManagedBot, me.id)
        if row:
            row.token, row.username, row.name = token, me.username, me.full_name
        else:
            row = ManagedBot(id=me.id, token=token, username=me.username, name=me.full_name)
            s.add(row)
        await s.commit()
    await manager.remove(me.id)
    manager.bots[me.id] = make_bot(token)
    await manager.start_polling(row)
    await state.clear()
    await m.answer(f"✅ @{html.escape(me.username)} bağlandı ve çalışıyor.{warn}",
                   reply_markup=kb([("⚙️ Botu yönet", f"bot:{me.id}")], BACK_HOME))


# ---------------- bot listesi / bot paneli ----------------
@router.callback_query(F.data == "bots")
async def cb_bots(c: CallbackQuery, state: FSMContext):
    await state.clear()
    async with Session() as s:
        bots = (await s.scalars(select(ManagedBot).order_by(ManagedBot.created_at))).all()
    rows = [[(f"{'🟢' if b.enabled else '⏸'} @{b.username}", f"bot:{b.id}")] for b in bots]
    rows.append([("➕ Bot Ekle", "addbot")])
    rows.append(BACK_HOME)
    await show(c, f"🤖 <b>Botlarım</b> ({len(bots)})", kb(*rows))


async def bot_panel(bot_id: int):
    async with Session() as s:
        b = await s.get(ManagedBot, bot_id)
        if not b:
            return "Bot bulunamadı.", kb(BACK_HOME)
        from .stats import EMPTY, bot_counts
        c = (await bot_counts(s, bot_id)).get(bot_id, EMPTY)
    menu_rows = (b.menu or {}).get("rows") or []
    from .external import ext_dir, label, mode as ext_mode
    xd = ext_dir(b)
    hb = bool(xd)
    if hb and ext_mode(xd) == "A":
        status = (f"🅰️ {label(xd)} otomatik sistemi yürütüyor (A modu)\n"
                  "📢 Buradan yayın yapabilirsin; aboneler her dakika aktarılır.")
    else:
        status = f"Durum: {'🟢 Çalışıyor' if b.enabled else '⏸ Durduruldu'}" + (" (🅱️ B modu)" if hb else "")
    text = (f"⚙️ <b>@{html.escape(b.username)}</b> — {html.escape(b.name)}\n"
            f"{status}\n\n"
            f"👥 {fmt(c['total'])} start • ✅ {fmt(c['active'])} aktif • 📈 bugün +{fmt(c['today'])}\n"
            f"👋 Karşılama: {'özel' if b.welcome else 'varsayılan'} • 📋 Alt menü: "
            f"{sum(len(r) for r in menu_rows)} buton")
    markup = kb(
        [("📊 Detaylı analiz", f"bst:{bot_id}"), ("📢 Bu bottan yayın", f"bcb:{bot_id}")],
        [("👋 Karşılama mesajı", f"bwel:{bot_id}"), ("📋 Alt menü", f"bmenu:{bot_id}")],
        [("📨 Otomatik zincir", f"itl:auto:{bot_id}"), ("🎁 /promo havuzu", f"itl:promo:{bot_id}")],
        [("💬 Menü yanıtları", f"bmr:{bot_id}"), ("🔄 Aktiflik kontrolü", f"bchk:{bot_id}")],
        [("📥 Aboneleri indir (CSV)", f"bcsv:{bot_id}"), ("🩺 Teşhis", f"bdoc:{bot_id}")],
        [("⏸ Durdur" if b.enabled else "▶️ Başlat", f"btog:{bot_id}"), ("🗑 Botu kaldır", f"bdel:{bot_id}")],
        [("⬅️ Botlarım", "bots")] + BACK_HOME,
    )
    if hb:
        cur = ext_mode(xd)
        markup.inline_keyboard.insert(0, [IB(
            text=f"🔀 Mod: {'🅰️ ' + label(xd) + ' otomatik' if cur == 'A' else '🅱️ Bot Merkezi'} — değiştir",
            callback_data=f"hbm:{bot_id}")])
        if cur == "A":  # A modunda başlat/durdur Haberolog'a ait; karışıklık olmasın
            markup.inline_keyboard = [r for r in markup.inline_keyboard
                                      if not any((x.callback_data or "").startswith("btog:") for x in r)] + []
            markup.inline_keyboard.insert(-1, [IB(text="🗑 Botu kaldır", callback_data=f"bdel:{bot_id}")])
    return text, markup


@router.callback_query(F.data.startswith("hbm:"))
async def cb_hb_mode(c: CallbackQuery):
    from .external import ext_dir, label, mode as ext_mode
    bid = int(c.data.split(":")[1])
    async with Session() as s:
        xd = ext_dir(await s.get(ManagedBot, bid))
    if not xd:
        return await show(c, "❌ Bu bot için harici sistem bulunamadı.", kb([("⬅️ Geri", f"bot:{bid}")]))
    name = label(xd)
    cur = ext_mode(xd)
    if cur == "A":
        text = ("🔀 <b>B moduna geçilsin mi?</b>\n\n"
                f"• {name} sisteminin otomatik özellikleri (bülten, bildirimler, menüler) <b>durur</b>.\n"
                "• Bot tamamen buradan yürütülür: karşılama mesajı, alt menü, zincir, promo, yayın.\n"
                f"• {name} kullanıcıları buraya aktarılır, kimse kaybolmaz.\n"
                "• İstediğin an tekrar A'ya dönebilirsin.")
        target = "B"
    else:
        text = (f"🔀 <b>A moduna ({name} otomatik) dönülsün mü?</b>\n\n"
                f"• {name} sisteminin otomatik özellikleri tekrar başlar.\n"
                f"• Bu sırada gelen aboneler {name} sistemine de aktarılır.\n"
                "• A modunda da buradan yayın yapmaya devam edebilirsin.")
        target = "A"
    await show(c, text, kb([(f"✅ Evet, {target} moduna geç", f"hbs:{bid}:{target}")], [("❌ Vazgeç", f"bot:{bid}")]))


@router.callback_query(F.data.startswith("hbs:"))
async def cb_hb_switch(c: CallbackQuery, manager: BotManager):
    from .external import switch
    _, bid, target = c.data.split(":")
    bid = int(bid)
    await c.answer("Geçiş yapılıyor…")
    await show(c, "⏳ Mod değiştiriliyor, ~15 saniye sürebilir…")
    msg = await switch(manager, bid, target)
    _, markup = await bot_panel(bid)
    await c.message.answer(msg, reply_markup=markup)


@router.callback_query(F.data.startswith("bot:"))
async def cb_bot(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = await bot_panel(int(c.data.split(":")[1]))
    await show(c, text, markup)


@router.callback_query(F.data.startswith(("bdoc:", "bdocwh:", "bdocrs:")))
async def cb_doctor(c: CallbackQuery, manager: BotManager):
    from .doctor import POLL_ERRORS, diagnose
    action, bid = c.data.split(":")
    bid = int(bid)
    note = ""
    if action == "bdocwh":
        bot = manager.sender(bid)
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            note = "🔧 Webhook kaldırıldı.\n\n"
        except Exception as e:
            note = f"❌ Kaldırılamadı: {html.escape(str(e))}\n\n"
    if action in ("bdocwh", "bdocrs"):
        async with Session() as s:
            b = await s.get(ManagedBot, bid)
        await manager.stop_polling(bid)
        POLL_ERRORS.pop(bid, None)
        if b and b.enabled:
            await manager.start_polling(b)
        await asyncio.sleep(3)
        note += "🔄 Bot yeniden başlatıldı.\n\n" if action == "bdocrs" else ""
    await c.answer("Kontrol ediliyor…")
    text, has_wh = await diagnose(manager, bid)
    rows = []
    if has_wh:
        rows.append([("🔧 Webhook'u kaldır", f"bdocwh:{bid}")])
    rows.append([("🔄 Yeniden başlat", f"bdocrs:{bid}"), ("🔁 Tekrar kontrol", f"bdoc:{bid}")])
    rows.append([("⬅️ Bot paneli", f"bot:{bid}")])
    await show(c, note + text, kb(*rows))


@router.callback_query(F.data.startswith("bst:"))
async def cb_bot_stats(c: CallbackQuery):
    bid = int(c.data.split(":")[1])
    await show(c, await bot_detail_text(bid), kb([("🔄 Yenile", f"bst:{bid}"), ("⬅️ Geri", f"bot:{bid}")]))


@router.callback_query(F.data.startswith("btog:"))
async def cb_toggle(c: CallbackQuery, manager: BotManager):
    bid = int(c.data.split(":")[1])
    async with Session() as s:
        b = await s.get(ManagedBot, bid)
        b.enabled = not b.enabled
        await s.commit()
    if b.enabled:
        await manager.start_polling(b)
    else:
        await manager.stop_polling(bid)
    text, markup = await bot_panel(bid)
    await show(c, text, markup)


@router.callback_query(F.data.startswith("bdel:"))
async def cb_del(c: CallbackQuery):
    bid = int(c.data.split(":")[1])
    await show(c, "⚠️ Bot sistemden kaldırılsın mı? <b>Tüm abone kayıtları da silinir.</b>\n"
                  "(Bot Telegram'dan silinmez.)",
               kb([("✅ Evet, kaldır", f"bdelok:{bid}"), ("❌ Vazgeç", f"bot:{bid}")]))


@router.callback_query(F.data.startswith("bdelok:"))
async def cb_del_ok(c: CallbackQuery, manager: BotManager):
    bid = int(c.data.split(":")[1])
    await manager.remove(bid)
    async with Session() as s:
        await s.execute(delete(Subscriber).where(Subscriber.bot_id == bid))
        await s.execute(delete(Item).where(Item.bot_id == bid))
        await s.execute(delete(AutoQueue).where(AutoQueue.bot_id == bid))
        await s.execute(delete(ManagedBot).where(ManagedBot.id == bid))
        await s.commit()
    await show(c, "🗑 Bot kaldırıldı.", kb([("⬅️ Botlarım", "bots")]))


@router.callback_query(F.data.startswith("bchk:"))
async def cb_check(c: CallbackQuery, runner: BroadcastRunner):
    bid = int(c.data.split(":")[1])
    n = len(await active_user_ids(bid))
    await c.answer()
    await c.message.answer(f"🔄 {fmt(n)} aktif abone kontrol ediliyor (kullanıcılara mesaj gitmez)…")

    async def job():
        alive, dead = await runner.check_active(bid)
        await c.message.answer(f"✅ Kontrol bitti.\nAktif: {fmt(alive)} • Engellemiş çıkan: {fmt(dead)}",
                               reply_markup=kb([("⚙️ Bota dön", f"bot:{bid}")]))
    asyncio.create_task(job())


@router.callback_query(F.data.startswith("bcsv:"))
async def cb_csv(c: CallbackQuery):
    bid = int(c.data.split(":")[1])
    async with Session() as s:
        b = await s.get(ManagedBot, bid)
        subs = (await s.scalars(select(Subscriber).where(Subscriber.bot_id == bid))).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "username", "ad", "dil", "kaynak", "durum", "katilim", "son_gorulme"])
    for x in subs:
        w.writerow([x.user_id, x.username or "", x.first_name or "", x.lang or "", x.source, x.status,
                    aware(x.joined_at).astimezone(TZ).strftime("%Y-%m-%d %H:%M"),
                    aware(x.last_seen).astimezone(TZ).strftime("%Y-%m-%d %H:%M")])
    await c.answer()
    await c.message.answer_document(
        BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename=f"{b.username}_aboneler.csv"),
        caption=f"📥 @{html.escape(b.username)} — {fmt(len(subs))} kayıt")


# ---------------- içerik kaydetme yardımcıları ----------------
async def preview(bot: Bot, chat_id: int, msg_ids: list[int], buttons, unsub=False, btn_text=None):
    await send_content(bot, chat_id, msg_ids, build_markup(buttons, unsub), btn_text)


async def take_content(m: Message, state: FSMContext) -> list[int] | None:
    """Mesajı (veya albümün tamamını) depo kanalına kopyalar, ID listesini döndürür.
    Albümde her parça ayrı gelir: ilk parça 1.5 sn bekleyip hepsini birlikte işler."""
    if not storage_ok():
        await m.answer(NO_STORAGE)
        return None
    try:
        if m.media_group_id:
            key = m.media_group_id
            if key in _albums:
                _albums[key].append(m)
                return None
            _albums[key] = [m]
            await asyncio.sleep(1.5)
            parts = sorted(_albums.pop(key), key=lambda x: x.message_id)
            res = await m.bot.copy_messages(chat_id=storage_id(), from_chat_id=m.chat.id,
                                            message_ids=[x.message_id for x in parts])
            return [r.message_id for r in res]
        res = await m.copy_to(storage_id())
        return [res.message_id]
    except Exception as e:
        await m.answer(f"❌ Depo kanalına kaydedilemedi: {html.escape(str(e))}\n"
                       "Yönetici botun depo kanalında admin olduğundan emin ol.")
        return None


# ---------------- karşılama mesajı ----------------
@router.callback_query(F.data.startswith("bwel:"))
async def cb_welcome(c: CallbackQuery, state: FSMContext):
    bid = int(c.data.split(":")[1])
    await state.set_state(Welcome.msg)
    await state.update_data(bot_id=bid)
    await show(c, "👋 Kullanıcılar /start dediğinde gidecek mesajı gönder.\n"
                  "Metin, foto, video, GIF olabilir (formatlama ve premium emoji korunur).\n\n"
                  "Varsayılana dönmek için <code>sıfırla</code> yaz. İptal: /iptal")


@router.message(Welcome.msg)
async def on_welcome_msg(m: Message, state: FSMContext):
    bid = (await state.get_data())["bot_id"]
    if m.text and m.text.strip().lower() == "sıfırla":
        async with Session() as s:
            b = await s.get(ManagedBot, bid)
            b.welcome = None
            await s.commit()
        await state.clear()
        await m.answer("✅ Karşılama varsayılana döndü.", reply_markup=kb([("⚙️ Bota dön", f"bot:{bid}")]))
        return
    msg_ids = await take_content(m, state)
    if msg_ids is None:
        return
    await state.update_data(msgs=msg_ids)
    await state.set_state(Welcome.buttons)
    await m.answer("Şimdi mesajın altına butonları ekle.\n\n" + BUTTON_HELP + (ALBUM_HELP if len(msg_ids) > 1 else ""))


@router.message(Welcome.buttons, F.text)
async def on_welcome_buttons(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    btn_text, rest = split_btn_text(m.text)
    try:
        buttons = parse_buttons(rest)
    except ValueError as e:
        await m.answer(f"❌ {html.escape(str(e))}")
        return
    async with Session() as s:
        b = await s.get(ManagedBot, data["bot_id"])
        b.welcome = {"msgs": data["msgs"], "buttons": buttons, "btn_text": btn_text}
        await s.commit()
    await state.clear()
    await m.answer("✅ Karşılama mesajı kaydedildi. Önizleme:")
    await preview(bot, m.chat.id, data["msgs"], buttons, btn_text=btn_text)
    await m.answer("Artık yeni /start diyenler bunu görecek.", reply_markup=kb([("⚙️ Bota dön", f"bot:{data['bot_id']}")]))


# ---------------- alt menü (reply klavye) ----------------
@router.callback_query(F.data.startswith("bmenu:"))
async def cb_menu(c: CallbackQuery, state: FSMContext):
    bid = int(c.data.split(":")[1])
    b = (await _get(bid))
    rows = (b.menu or {}).get("rows") or []
    current = "\n".join(" | ".join(r) for r in rows) or "—"
    await state.set_state(MenuSt.layout)
    await state.update_data(bot_id=bid)
    await show(c, "📋 <b>Alt menü</b> (sohbetin altında sabit duran butonlar)\n\n"
                  f"Şu anki:\n<code>{html.escape(current)}</code>\n\n"
                  "Yeni menüyü yaz (her satır bir sıra, <code>|</code> ile yan yana):\n"
                  "<code>🔵 ✅ GÜNCEL GİRİŞ | 🟢 🎁 DENEME BONUSU\n🔴 🔥 PROMOSYON KODU | 🟢 📩 CANLI DESTEK</code>\n\n"
                  "🟢 yeşil • 🔴 kırmızı • 🔵 mavi (renk istemezsen başa koyma)\n"
                  "Menüyü kaldırmak için <code>-</code> yaz. İptal: /iptal")


async def _get(bid) -> ManagedBot:
    async with Session() as s:
        return await s.get(ManagedBot, bid)


@router.message(MenuSt.layout, F.text)
async def on_menu_layout(m: Message, state: FSMContext):
    bid = (await state.get_data())["bot_id"]
    rows = parse_menu(m.text)
    async with Session() as s:
        b = await s.get(ManagedBot, bid)
        old = (b.menu or {}).get("responses") or {}
        texts = {split_style(t)[0] for r in rows for t in r}
        b.menu = {"rows": rows, "responses": {k: v for k, v in old.items() if k in texts}} if rows else None
        await s.commit()
    await state.clear()
    if not rows:
        await m.answer("✅ Alt menü kaldırıldı. (Kullanıcılarda bir sonraki /start'ta güncellenir.)",
                       reply_markup=kb([("⚙️ Bota dön", f"bot:{bid}")]))
        return
    layout = "\n".join("  ".join(f"[ {html.escape(t)} ]" for t in r) for r in rows)
    await m.answer(f"✅ Alt menü kaydedildi. Kullanıcılar şöyle görecek:\n\n{layout}\n\n"
                   "Şimdi her butona basılınca ne gönderileceğini ayarla:",
                   reply_markup=kb([("💬 Menü yanıtlarını ayarla", f"bmr:{bid}")], [("⚙️ Bota dön", f"bot:{bid}")]))


@router.callback_query(F.data.startswith("bmr:"))
async def cb_menu_resp(c: CallbackQuery, state: FSMContext):
    await state.clear()
    bid = int(c.data.split(":")[1])
    b = await _get(bid)
    menu = b.menu or {}
    flat = [t for r in (menu.get("rows") or []) for t in r]
    if not flat:
        await show(c, "Önce alt menü oluştur.", kb([("📋 Alt menü", f"bmenu:{bid}")], [("⬅️ Geri", f"bot:{bid}")]))
        return
    resp = menu.get("responses") or {}
    rows = [[(f"{'✅' if split_style(t)[0] in resp else '⬜'} {t}", f"mri:{bid}:{i}")] for i, t in enumerate(flat)]
    rows.append([("⬅️ Geri", f"bot:{bid}")])
    await show(c, "💬 Yanıtını ayarlamak istediğin menü butonunu seç:\n✅ ayarlı • ⬜ boş", kb(*rows))


@router.callback_query(F.data.startswith("mri:"))
async def cb_menu_item(c: CallbackQuery, state: FSMContext):
    _, bid, idx = c.data.split(":")
    b = await _get(int(bid))
    flat = [t for r in ((b.menu or {}).get("rows") or []) for t in r]
    text = split_style(flat[int(idx)])[0]  # kullanıcının bastığı yazı (renk emojisi olmadan)
    await state.set_state(MenuSt.resp_msg)
    await state.update_data(bot_id=int(bid), item=text)
    await show(c, f"«{html.escape(text)}» butonuna basılınca gidecek mesajı gönder (metin/foto/video/albüm).\n\n"
                  "🎁 Bu buton sıradaki promoyu göstersin istiyorsan <code>promo</code> yaz.\n"
                  "Yanıtı kaldırmak için <code>-</code> yaz. İptal: /iptal")


@router.message(MenuSt.resp_msg)
async def on_menu_resp_msg(m: Message, state: FSMContext):
    data = await state.get_data()
    if m.text and m.text.strip() == "-":
        async with Session() as s:
            b = await s.get(ManagedBot, data["bot_id"])
            menu = dict(b.menu or {})
            resp = dict(menu.get("responses") or {})
            resp.pop(data["item"], None)
            menu["responses"] = resp
            b.menu = menu
            await s.commit()
        await state.clear()
        await m.answer("✅ Yanıt kaldırıldı.", reply_markup=kb([("💬 Menü yanıtları", f"bmr:{data['bot_id']}")]))
        return
    if m.text and m.text.strip().lower() in ("promo", "/promo"):
        async with Session() as s:
            b = await s.get(ManagedBot, data["bot_id"])
            menu = dict(b.menu or {})
            resp = dict(menu.get("responses") or {})
            resp[data["item"]] = {"action": "promo"}
            menu["responses"] = resp
            b.menu = menu
            await s.commit()
        await state.clear()
        await m.answer(f"✅ «{html.escape(data['item'])}» butonu artık sıradaki promoyu gösterecek.",
                       reply_markup=kb([("🎁 /promo havuzu", f"itl:promo:{data['bot_id']}")],
                                       [("💬 Menü yanıtları", f"bmr:{data['bot_id']}")]))
        return
    msg_ids = await take_content(m, state)
    if msg_ids is None:
        return
    await state.update_data(msgs=msg_ids)
    await state.set_state(MenuSt.resp_buttons)
    await m.answer("Bu yanıtın altına buton ekle.\n\n" + BUTTON_HELP + (ALBUM_HELP if len(msg_ids) > 1 else ""))


@router.message(MenuSt.resp_buttons, F.text)
async def on_menu_resp_buttons(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    btn_text, rest = split_btn_text(m.text)
    try:
        buttons = parse_buttons(rest)
    except ValueError as e:
        await m.answer(f"❌ {html.escape(str(e))}")
        return
    async with Session() as s:
        b = await s.get(ManagedBot, data["bot_id"])
        menu = dict(b.menu or {})
        resp = dict(menu.get("responses") or {})
        resp[data["item"]] = {"msgs": data["msgs"], "buttons": buttons, "btn_text": btn_text}
        menu["responses"] = resp
        b.menu = menu
        await s.commit()
    await state.clear()
    await m.answer(f"✅ «{html.escape(data['item'])}» yanıtı kaydedildi. Önizleme:")
    await preview(bot, m.chat.id, data["msgs"], buttons, btn_text=btn_text)
    await m.answer("—", reply_markup=kb([("💬 Diğer yanıtlar", f"bmr:{data['bot_id']}")],
                                        [("⚙️ Bota dön", f"bot:{data['bot_id']}")]))


# ---------------- yayın ----------------
async def select_kb(sel: list[int]):
    async with Session() as s:
        bots = (await s.scalars(select(ManagedBot).order_by(ManagedBot.created_at))).all()
    rows = [[(f"{'☑️' if b.id in sel else '⬜'} @{b.username}", f"bcs:{b.id}")] for b in bots]
    rows.append([("✅ Hepsini seç", "bcs:all"), ("🔲 Temizle", "bcs:none")])
    rows.append([("➡️ Devam", "bcs:done"), ("❌ İptal", "home")])
    return kb(*rows), [b.id for b in bots]


@router.callback_query(F.data == "bc:new")
@router.callback_query(F.data.startswith("bcb:"))
async def cb_bc_new(c: CallbackQuery, state: FSMContext):
    await state.clear()
    sel = [int(c.data.split(":")[1])] if c.data.startswith("bcb:") else []
    await state.set_state(Bc.select)
    await state.update_data(sel=sel, unsub=True)
    markup, _ = await select_kb(sel)
    await show(c, "📢 <b>Yeni yayın</b>\nHangi botlardan gönderilsin?", markup)


@router.callback_query(Bc.select, F.data.startswith("bcs:"))
async def cb_bc_select(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sel = list(data.get("sel", []))
    arg = c.data.split(":")[1]
    _, all_ids = await select_kb(sel)
    if arg == "done":
        if not sel:
            await c.answer("En az bir bot seç.", show_alert=True)
            return
        if not storage_ok():
            await show(c, NO_STORAGE, kb(BACK_HOME))
            return
        await state.set_state(Bc.msg)
        await show(c, "✍️ Yayın mesajını gönder.\nMetin, foto, video, GIF, dosya veya albüm olabilir. "
                      "Telegram'daki formatlama ve premium emojiler korunur.\n\nİptal: /iptal")
        return
    if arg == "all":
        sel = all_ids
    elif arg == "none":
        sel = []
    else:
        bid = int(arg)
        sel = [x for x in sel if x != bid] if bid in sel else sel + [bid]
    await state.update_data(sel=sel)
    markup, _ = await select_kb(sel)
    await show(c, "📢 <b>Yeni yayın</b>\nHangi botlardan gönderilsin?", markup)


@router.message(Bc.msg)
async def on_bc_msg(m: Message, state: FSMContext):
    msg_ids = await take_content(m, state)
    if msg_ids is None:
        return
    await state.update_data(msgs=msg_ids)
    await state.set_state(Bc.buttons)
    await m.answer("🔘 Mesajın altına butonları ekle.\n\n" + BUTTON_HELP + (ALBUM_HELP if len(msg_ids) > 1 else ""))


async def confirm_screen(state: FSMContext):
    data = await state.get_data()
    lines = ["📋 <b>Yayın özeti</b>", ""]
    total = 0
    async with Session() as s:
        for bid in data["sel"]:
            b = await s.get(ManagedBot, bid)
            n = len(await active_user_ids(bid))
            total += n
            lines.append(f"• @{html.escape(b.username)} → {fmt(n)} aktif abone")
    lines += ["", f"<b>Toplam hedef:</b> {fmt(total)} kişi",
              f"İçerik: {'📎 albüm (' + str(len(data['msgs'])) + ' parça)' if len(data['msgs']) > 1 else 'tek mesaj'}",
              f"👆 Tıklama takibi: {'Açık' if PUBLIC_URL else 'Kapalı (PUBLIC_URL ayarlı değil)'}",
              f"Butonlar:\n<code>{html.escape(buttons_to_text(data.get('buttons')))}</code>",
              f"🔕 'Bildirimleri kapat' butonu: {'Açık' if data.get('unsub') else 'Kapalı'}"]
    markup = kb([("🚀 Şimdi gönder", "bcgo"), ("⏰ Zamanla", "bcsch")],
                [(f"🔕 Çık butonu: {'Açık' if data.get('unsub') else 'Kapalı'}", "bcunsub"),
                 ("👁 Önizleme", "bcprev")],
                [("❌ İptal", "home")])
    return "\n".join(lines), markup


@router.message(Bc.buttons, F.text)
async def on_bc_buttons(m: Message, state: FSMContext, bot: Bot):
    btn_text, rest = split_btn_text(m.text)
    try:
        buttons = parse_buttons(rest)
    except ValueError as e:
        await m.answer(f"❌ {html.escape(str(e))}")
        return
    await state.update_data(buttons=buttons, btn_text=btn_text)
    await state.set_state(Bc.confirm)
    data = await state.get_data()
    await m.answer("👁 <b>Önizleme</b> (kullanıcılar tam olarak bunu görecek):")
    await preview(bot, m.chat.id, data["msgs"], buttons, data.get("unsub"), btn_text)
    text, markup = await confirm_screen(state)
    await m.answer(text, reply_markup=markup)


@router.callback_query(Bc.confirm, F.data == "bcunsub")
async def cb_bc_unsub(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(unsub=not data.get("unsub"))
    text, markup = await confirm_screen(state)
    await show(c, text, markup)


@router.callback_query(Bc.confirm, F.data == "bcprev")
async def cb_bc_prev(c: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await c.answer()
    await preview(bot, c.message.chat.id, data["msgs"], data.get("buttons"), data.get("unsub"), data.get("btn_text"))
    text, markup = await confirm_screen(state)
    await c.message.answer(text, reply_markup=markup)


async def _create_bc(state: FSMContext, user_id: int, when: datetime | None) -> int:
    data = await state.get_data()
    async with Session() as s:
        bc = Broadcast(created_by=user_id, msg=data["msgs"][0], msg_ids=data["msgs"],
                       btn_text=data.get("btn_text"), buttons=None,
                       unsub_btn=bool(data.get("unsub")), bot_ids=data["sel"],
                       status="scheduled" if when else "queued", scheduled_at=when)
        s.add(bc)
        await s.flush()
        # her URL butonuna takip ID'si ver
        rows = []
        for row in data.get("buttons") or []:
            new_row = []
            for btn in row:
                link = Link(broadcast_id=bc.id, text=btn["text"][:128], url=btn["url"])
                s.add(link)
                await s.flush()
                new_row.append({**btn, "lid": link.id})
            rows.append(new_row)
        bc.buttons = rows or None
        await s.commit()
        return bc.id


@router.callback_query(Bc.confirm, F.data == "bcgo")
async def cb_bc_go(c: CallbackQuery, state: FSMContext, runner: BroadcastRunner):
    bc_id = await _create_bc(state, c.from_user.id, None)
    await state.clear()
    runner.start(bc_id)
    await show(c, f"🚀 Yayın #{bc_id} başlatıldı. İlerleme aşağıda canlı güncellenecek.", kb(BACK_HOME))


@router.callback_query(Bc.confirm, F.data == "bcsch")
async def cb_bc_sch(c: CallbackQuery, state: FSMContext):
    await state.set_state(Bc.schedule)
    await show(c, "⏰ Ne zaman gönderilsin? (Türkiye saati)\n\n"
                  "<code>20:30</code> → bugün (geçtiyse yarın)\n"
                  "<code>12.11 20:30</code> veya <code>12.11.2026 20:30</code>\n\nİptal: /iptal")


def parse_when(text: str) -> datetime | None:
    text = text.strip()
    now = datetime.now(TZ)
    for f in ("%d.%m.%Y %H:%M", "%d.%m %H:%M", "%H:%M"):
        try:
            dt = datetime.strptime(text, f)
        except ValueError:
            continue
        if f == "%H:%M":
            dt = now.replace(hour=dt.hour, minute=dt.minute, second=0, microsecond=0)
            if dt <= now:
                dt += timedelta(days=1)
            return dt
        if f == "%d.%m %H:%M":
            dt = dt.replace(year=now.year)
        return dt.replace(tzinfo=TZ)
    return None


@router.message(Bc.schedule, F.text)
async def on_bc_sch(m: Message, state: FSMContext):
    when = parse_when(m.text)
    if not when or when <= datetime.now(TZ):
        await m.answer("❌ Tarih anlaşılamadı veya geçmişte. Örnek: <code>20:30</code> ya da <code>12.11 20:30</code>")
        return
    bc_id = await _create_bc(state, m.from_user.id, when)
    await state.clear()
    await m.answer(f"⏰ Yayın #{bc_id} zamanlandı: <b>{when:%d.%m.%Y %H:%M}</b>",
                   reply_markup=kb([("📑 Rapor", f"rep:{bc_id}")], BACK_HOME))


# ---------------- analiz & raporlar ----------------
@router.callback_query(F.data == "stats")
async def cb_stats(c: CallbackQuery):
    async with Session() as s:
        bots = (await s.scalars(select(ManagedBot).order_by(ManagedBot.created_at))).all()
    rows = [[(f"📊 @{b.username}", f"bst:{b.id}")] for b in bots]
    rows.append([("🔄 Yenile", "stats")] + BACK_HOME)
    await show(c, await overview_text(), kb(*rows))


@router.callback_query(F.data == "reports")
async def cb_reports(c: CallbackQuery):
    async with Session() as s:
        items = (await s.scalars(select(Broadcast).order_by(Broadcast.id.desc()).limit(15))).all()
    icons = {"scheduled": "⏰", "queued": "⏳", "running": "📤", "done": "✅", "cancelled": "❌", "deleted": "🗑"}
    rows = []
    for b in items:
        t = aware(b.scheduled_at or b.created_at).astimezone(TZ)
        sent = sum(p["sent"] for p in (b.stats or {}).values())
        rows.append([(f"{icons.get(b.status, '•')} #{b.id} • {t:%d.%m %H:%M} • {fmt(sent)} ulaştı", f"rep:{b.id}")])
    rows.append(BACK_HOME)
    await show(c, "📑 <b>Son yayınlar</b>" if items else "Henüz yayın yok.", kb(*rows))


@router.callback_query(F.data.startswith("rep:"))
async def cb_report(c: CallbackQuery):
    bc_id = int(c.data.split(":")[1])
    async with Session() as s:
        bc = await s.get(Broadcast, bc_id)
    rows = []
    if bc and bc.status == "scheduled":
        rows.append([("❌ Zamanlamayı iptal et", f"repcancel:{bc_id}")])
    if bc and bc.status == "done":
        rows.append([("🗑 Tüm botlardan sil", f"repdel:{bc_id}")])
    rows.append([("🔄 Yenile", f"rep:{bc_id}"), ("⬅️ Raporlar", "reports")])
    await show(c, await report_text(bc_id), kb(*rows))


@router.callback_query(F.data.startswith("repcancel:"))
async def cb_rep_cancel(c: CallbackQuery):
    bc_id = int(c.data.split(":")[1])
    async with Session() as s:
        bc = await s.get(Broadcast, bc_id)
        if bc and bc.status == "scheduled":
            bc.status = "cancelled"
            await s.commit()
    await show(c, f"❌ Yayın #{bc_id} iptal edildi.", kb([("⬅️ Raporlar", "reports")]))


@router.callback_query(F.data.startswith("repdel:"))
async def cb_rep_del(c: CallbackQuery):
    bc_id = int(c.data.split(":")[1])
    await c.answer()
    await c.message.answer(f"⚠️ Yayın #{bc_id} tüm kullanıcılardan silinsin mi?\n"
                           "(Telegram sadece son 48 saatteki mesajların silinmesine izin verir.)",
                           reply_markup=kb([("✅ Evet, sil", f"repdelok:{bc_id}"), ("❌ Vazgeç", f"rep:{bc_id}")]))


@router.callback_query(F.data.startswith("repdelok:"))
async def cb_rep_del_ok(c: CallbackQuery, runner: BroadcastRunner):
    bc_id = int(c.data.split(":")[1])
    await show(c, f"🗑 Yayın #{bc_id} siliniyor…")

    async def job():
        ok, fail = await runner.delete_broadcast(bc_id)
        await c.message.answer(f"🗑 Yayın #{bc_id}: {fmt(ok)} mesaj silindi, {fmt(fail)} silinemedi.",
                               reply_markup=kb(BACK_HOME))
    asyncio.create_task(job())


# ---------------- depo kanalı ID'si öğrenme ----------------
channel_router = Router()


@channel_router.channel_post(F.text.in_({"/depo", "/id"}))
async def on_channel_storage(m: Message, bot: Bot):
    """Kanala /depo yazılınca, kanal yöneticilerinden biri sistem admini ise depo kanalı yapılır.
    (Başkası botu kendi kanalına ekleyip depoyu ele geçiremesin diye kontrol ediliyor.)"""
    try:
        admins = await bot.get_chat_administrators(m.chat.id)
    except Exception:
        return
    if not any(a.user.id in ADMIN_IDS for a in admins):
        await m.answer("⛔ Bu kanalın yöneticileri arasında sistem admini yok.")
        return
    await set_storage(m.chat.id)
    try:
        await m.delete()
    except Exception:
        pass
    for uid in ADMIN_IDS:
        try:
            await bot.send_message(uid, f"✅ Depo kanalı ayarlandı: <b>{html.escape(m.chat.title or '')}</b>\n"
                                        "Bağlayacağın tüm botları bu kanala <b>admin</b> olarak eklemeyi unutma.")
        except Exception:
            pass
