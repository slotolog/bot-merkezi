"""İş Haberleri botu — Bot Merkezi'ne bağlı harici servis (A/B modu).

A modu: bu servis botu çalıştırır (haber toplama, anlık bildirim, günlük bülten, menüler).
B modu: servis bekler, botu Bot Merkezi yürütür. Haber toplamaya devam eder.
Token Bot Merkezi veritabanından okunur (ISHABER_USERNAME ile eşleşen bot).
"""
import asyncio
import html
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, ChatMemberUpdated,
                           InlineKeyboardButton as IB, InlineKeyboardMarkup, KeyboardButton,
                           Message, ReplyKeyboardMarkup)

from news import CATS, DEFAULT_CATS, ago, fetch_cat
from store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("isharberi")

DATA = Path(os.environ.get("DATA_DIR", "/data"))
DATA.mkdir(parents=True, exist_ok=True)
TZ = ZoneInfo("Europe/Istanbul")
USERNAME = os.environ.get("ISHABER_USERNAME", "").lstrip("@").lower()
DB_URL = os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
TELEGRAM_API = os.environ.get("TELEGRAM_API", "").strip().rstrip("/")
FETCH_EVERY = int(os.environ.get("FETCH_EVERY_MIN", "10")) * 60
DAILY_CAP = int(os.environ.get("DAILY_ALERT_CAP", "6"))
QUIET = (23, 8)  # 23:00–08:00 anlık bildirim yok
BULLETIN_HOURS = (8, 12, 18, 21)
BOT_NAME = "İş Haberleri | Güncel İlanlar"
DESCRIPTION = ("💼 Türkiye'nin güncel iş haberleri tek yerde!\n\n"
               "🏛 Kamu ve özel sektör personel alımları\n🎓 Staj ve yeni mezun fırsatları\n"
               "💰 Maaş, asgari ücret ve çalışma hayatı gelişmeleri\n🔔 İlgi alanına göre anlık bildirim "
               "veya günlük bülten\n🔑 Anahtar kelime alarmı (ör. şoför, İzmir)\n\n👇 Başlamak için BAŞLAT'a dokun")
ABOUT = "💼 Güncel iş ilanları, kamu alımları, staj ve maaş haberleri. Anlık bildirim ve günlük bülten."
COMMANDS = [("start", "Ana menü"), ("haberler", "Son iş haberleri"), ("kamu", "Kamu alımları"),
            ("ozel", "Özel sektör ilanları"), ("staj", "Staj & yeni mezun"), ("maas", "Maaş & asgari ücret"),
            ("ara", "Haberlerde ara"), ("alarm", "Anahtar kelime alarmı"), ("ayarlar", "Bildirim ayarları"),
            ("kaydedilenler", "Kaydettiğim haberler"), ("yardim", "Yardım"), ("gizlilik", "Gizlilik")]

store = Store(DATA / "isharberi.db")
router = Router()
awaiting: dict[int, str] = {}  # kullanıcı → "search" / "keyword"

MENU = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="📰 Son Haberler", style="primary"), KeyboardButton(text="🏛 Kamu Alımları", style="success")],
    [KeyboardButton(text="🏭 Özel Sektör"), KeyboardButton(text="🎓 Staj & Mezun")],
    [KeyboardButton(text="💰 Maaş & Ücret"), KeyboardButton(text="🔎 Haber Ara")],
    [KeyboardButton(text="🔔 Bildirim Ayarları", style="danger"), KeyboardButton(text="⭐ Kaydettiklerim")],
], resize_keyboard=True, is_persistent=True)
MENU_CATS = {"📰 Son Haberler": None, "🏛 Kamu Alımları": "kamu", "🏭 Özel Sektör": "ozel",
             "🎓 Staj & Mezun": "staj", "💰 Maaş & Ücret": "maas"}


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[IB(text=t, callback_data=d) for t, d in row] for row in rows])


def user_cats(u) -> list[str]:
    return [c for c in (u["cats"] or "").split(",") if c in CATS]


def user_keywords(u) -> list[str]:
    return [k for k in (u["keywords"] or "").split("|") if k]


def fmt_item(it, n=None) -> str:
    emoji = CATS.get(it["cat"], ("📰",))[0]
    num = f"{n}. " if n else ""
    src = f"{html.escape(it['source'])} • " if it["source"] else ""
    return (f"{num}{emoji} <a href=\"{html.escape(it['link'])}\">{html.escape(it['title'])}</a>\n"
            f"<i>{src}{ago(it['published'])}</i>")


def ensure_user(m_or_c) -> None:
    u = m_or_c.from_user
    store.upsert_user(u.id, u.username, u.first_name, u.language_code)


# ======================= başlangıç / onboarding =======================
def cats_kb(sel: list[str], done_cb="ob:done"):
    rows = []
    keys = list(CATS)
    for i in range(0, len(keys), 2):
        rows.append([(f"{'✅' if k in sel else '⬜'} {CATS[k][0]} {CATS[k][1]}", f"cat:{k}") for k in keys[i:i + 2]])
    rows.append([("✔️ Tamam", done_cb)])
    return kb(*rows)


@router.message(CommandStart())
async def on_start(m: Message, command: CommandObject):
    u = m.from_user
    new = store.upsert_user(u.id, u.username, u.first_name, u.language_code, (command.args or "organik")[:64])
    awaiting.pop(u.id, None)
    row = store.user(u.id)
    if new or not row["onboarded"]:
        await m.answer(f"👋 Merhaba {html.escape(u.first_name or '')}! <b>İş Haberleri</b>'ne hoş geldin.\n\n"
                       "Kamu ve özel sektör alımları, staj fırsatları, maaş ve çalışma hayatı haberlerini "
                       "senin için takip ediyorum.", reply_markup=MENU)
        await m.answer("🔔 <b>Hangi konularda haber almak istersin?</b>\nİstediğin kadar seç, sonra <b>Tamam</b>'a bas.",
                       reply_markup=cats_kb(user_cats(row)))
        return
    await m.answer(f"👋 Tekrar hoş geldin {html.escape(u.first_name or '')}! Aşağıdaki menüden devam edebilirsin.",
                   reply_markup=MENU)


@router.callback_query(F.data.startswith("cat:"))
async def cb_cat(c: CallbackQuery):
    ensure_user(c)
    key = c.data.split(":")[1]
    sel = user_cats(store.user(c.from_user.id))
    sel = [x for x in sel if x != key] if key in sel else sel + [key]
    store.update_user(c.from_user.id, cats=",".join(sel))
    done = "set:main" if store.user(c.from_user.id)["onboarded"] else "ob:done"
    try:
        await c.message.edit_reply_markup(reply_markup=cats_kb(sel, done))
    except TelegramBadRequest:
        pass
    await c.answer()


@router.callback_query(F.data == "ob:done")
async def cb_onboard_done(c: CallbackQuery):
    store.update_user(c.from_user.id, onboarded=1)
    sel = user_cats(store.user(c.from_user.id))
    await c.answer("Kaydedildi ✅")
    names = ", ".join(CATS[k][1] for k in sel) or "hiçbiri"
    try:
        await c.message.edit_text(f"✅ Takip ettiğin konular: <b>{names}</b>\n\n"
                                  "Yeni haber çıktığında sana bildireceğim (gece 23:00–08:00 arası rahatsız etmem). "
                                  "Bunu 🔔 Bildirim Ayarları'ndan günlük bültene çevirebilirsin.")
    except TelegramBadRequest:
        pass
    await send_list(c.message, sel or None, 0, c.from_user.id)


# ======================= haber listeleri =======================
async def send_list(target: Message, cats, offset: int, uid: int, edit=False):
    items = store.latest(cats, limit=5, offset=offset)
    title = "📰 Son İş Haberleri" if not cats or len(cats) > 1 else f"{CATS[cats[0]][0]} {CATS[cats[0]][1]}"
    if not items:
        text = f"<b>{title}</b>\n\nŞu an gösterilecek haber yok, birazdan tekrar dene. 🔄"
        return await (target.edit_text(text) if edit else target.answer(text))
    body = "\n\n".join(fmt_item(it, offset + i + 1) for i, it in enumerate(items))
    key = ",".join(cats) if cats else "all"
    rows = [[(f"⭐ {offset + i + 1}", f"sv:{it['id']}") for i, it in enumerate(items)]]
    nav = []
    if offset > 0:
        nav.append(("◀️ Önceki", f"pg:{key}:{max(0, offset - 5)}"))
    if len(items) == 5:
        nav.append(("Sonraki ▶️", f"pg:{key}:{offset + 5}"))
    if nav:
        rows.append(nav)
    text = f"<b>{title}</b>\n\n{body}"
    if edit:
        try:
            return await target.edit_text(text, reply_markup=kb(*rows), disable_web_page_preview=True)
        except TelegramBadRequest:
            return
    return await target.answer(text, reply_markup=kb(*rows), disable_web_page_preview=True)


@router.callback_query(F.data.startswith("pg:"))
async def cb_page(c: CallbackQuery):
    _, key, off = c.data.split(":")
    cats = None if key == "all" else key.split(",")
    await c.answer()
    await send_list(c.message, cats, int(off), c.from_user.id, edit=True)


@router.message(Command("haberler"))
async def cmd_news(m: Message):
    ensure_user(m)
    await send_list(m, user_cats(store.user(m.from_user.id)) or None, 0, m.from_user.id)


for _cmd in ("kamu", "ozel", "staj", "maas"):
    def _make(cat):
        async def handler(m: Message):
            ensure_user(m)
            await send_list(m, [cat], 0, m.from_user.id)
        return handler
    router.message.register(_make(_cmd), Command(_cmd))


# ======================= kaydet =======================
@router.callback_query(F.data.startswith("sv:"))
async def cb_save(c: CallbackQuery):
    ensure_user(c)
    iid = c.data.split(":")[1]
    if not store.item(iid):
        return await c.answer("Bu haber artık mevcut değil.", show_alert=True)
    added = store.save(c.from_user.id, iid)
    await c.answer("⭐ Kaydedildi" if added else "Zaten kayıtlı")


@router.message(Command("kaydedilenler"))
@router.message(F.text == "⭐ Kaydettiklerim")
async def cmd_saved(m: Message):
    ensure_user(m)
    items = store.saved(m.from_user.id)
    if not items:
        return await m.answer("⭐ Henüz kaydettiğin haber yok.\nHaber listelerindeki ⭐ numaralarına basarak kaydedebilirsin.")
    body = "\n\n".join(fmt_item(it, i + 1) for i, it in enumerate(items))
    rows = [[(f"🗑 {i + 1}", f"us:{it['id']}") for i, it in enumerate(items[:5])]]
    if len(items) > 5:
        rows.append([(f"🗑 {i + 1}", f"us:{it['id']}") for i, it in enumerate(items[5:10], 5)])
    await m.answer(f"⭐ <b>Kaydettiklerin</b>\n\n{body}", reply_markup=kb(*rows), disable_web_page_preview=True)


@router.callback_query(F.data.startswith("us:"))
async def cb_unsave(c: CallbackQuery):
    store.unsave(c.from_user.id, c.data.split(":")[1])
    await c.answer("Silindi")


# ======================= arama ve alarm =======================
@router.message(Command("ara"))
async def cmd_search(m: Message, command: CommandObject):
    ensure_user(m)
    if command.args:
        return await do_search(m, command.args)
    awaiting[m.from_user.id] = "search"
    await m.answer("🔎 Ne aramak istersin? Bir kelime yaz (ör. <code>polis alımı</code>, <code>İzmir</code>, <code>öğretmen</code>).")


@router.message(F.text == "🔎 Haber Ara")
async def btn_search(m: Message):
    ensure_user(m)
    awaiting[m.from_user.id] = "search"
    await m.answer("🔎 Ne aramak istersin? Bir kelime yaz (ör. <code>polis alımı</code>, <code>İzmir</code>, <code>öğretmen</code>).")


async def do_search(m: Message, text: str):
    words = [w for w in text.lower().split() if len(w) >= 2][:4]
    if not words:
        return await m.answer("En az 2 harfli bir kelime yaz.")
    items = store.search(words)
    if not items:
        return await m.answer(f"🔎 “{html.escape(text)}” için son 3 haftada haber bulunamadı.\n\n"
                              "💡 Bu kelime için alarm kurmak ister misin? Yeni haber çıkınca haber veririm.",
                              reply_markup=kb([("🔑 Alarm kur", f"kwq:{text[:40]}")]))
    body = "\n\n".join(fmt_item(it, i + 1) for i, it in enumerate(items))
    await m.answer(f"🔎 <b>“{html.escape(text)}” sonuçları</b>\n\n{body}",
                   reply_markup=kb([(f"⭐ {i + 1}", f"sv:{it['id']}") for i, it in enumerate(items[:5])],
                                   [("🔑 Bu kelimeye alarm kur", f"kwq:{text[:40]}")]),
                   disable_web_page_preview=True)


def keywords_screen(uid):
    kws = user_keywords(store.user(uid))
    text = ("🔑 <b>Anahtar kelime alarmları</b>\n\nBaşlığında bu kelimeler geçen yeni haberleri, "
            "konu seçiminden bağımsız olarak sana iletirim.\n\n"
            + ("\n".join(f"• {html.escape(k)}" for k in kws) if kws else "<i>Henüz alarm yok.</i>"))
    rows = [[(f"🗑 {k}"[:30], f"kwx:{i}")] for i, k in enumerate(kws)]
    rows.append([("➕ Kelime ekle", "kw+"), ("⬅️ Ayarlar", "set:main")])
    return text, kb(*rows)


async def add_keyword(uid: int, word: str) -> str:
    word = " ".join(word.lower().split())[:30]
    kws = user_keywords(store.user(uid))
    if len(word) < 2:
        return "En az 2 harf olmalı."
    if word in kws:
        return "Bu kelime zaten ekli."
    if len(kws) >= 10:
        return "En fazla 10 alarm kurabilirsin. Önce birini sil."
    store.update_user(uid, keywords="|".join(kws + [word]))
    return f"✅ “{html.escape(word)}” alarmı kuruldu."


@router.message(Command("alarm"))
async def cmd_alarm(m: Message, command: CommandObject):
    ensure_user(m)
    if command.args:
        return await m.answer(await add_keyword(m.from_user.id, command.args))
    text, markup = keywords_screen(m.from_user.id)
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("kwq:"))
async def cb_kw_quick(c: CallbackQuery):
    ensure_user(c)
    await c.answer()
    await c.message.answer(await add_keyword(c.from_user.id, c.data[4:]))


@router.callback_query(F.data == "kw+")
async def cb_kw_add(c: CallbackQuery):
    awaiting[c.from_user.id] = "keyword"
    await c.answer()
    await c.message.answer("✍️ Alarm kurmak istediğin kelimeyi yaz (ör. <code>şoför</code>, <code>Ankara</code>, <code>PTT</code>).")


@router.callback_query(F.data.startswith("kwx:"))
async def cb_kw_del(c: CallbackQuery):
    kws = user_keywords(store.user(c.from_user.id))
    i = int(c.data.split(":")[1])
    if i < len(kws):
        kws.pop(i)
        store.update_user(c.from_user.id, keywords="|".join(kws))
    text, markup = keywords_screen(c.from_user.id)
    await c.answer("Silindi")
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass


# ======================= ayarlar =======================
def settings_screen(uid):
    u = store.user(uid)
    mode = u["mode"]
    cats = ", ".join(CATS[k][1] for k in user_cats(u)) or "—"
    mode_txt = {"instant": "⚡ Anında (günde en fazla %d)" % DAILY_CAP,
                "bulletin": f"📰 Günlük bülten ({u['bulletin_hour']:02d}:00)", "off": "🔕 Kapalı"}[mode]
    text = (f"🔔 <b>Bildirim Ayarları</b>\n\n<b>Konular:</b> {cats}\n<b>Bildirim:</b> {mode_txt}\n"
            f"<b>Kelime alarmı:</b> {len(user_keywords(u))} adet\n\n"
            "Gece 23:00–08:00 arası anlık bildirim gönderilmez.")
    rows = [[("📂 Konuları seç", "set:cats"), ("🔑 Kelime alarmları", "set:kw")],
            [(("✅ " if mode == "instant" else "") + "⚡ Anında", "mode:instant"),
             (("✅ " if mode == "bulletin" else "") + "📰 Bülten", "mode:bulletin"),
             (("✅ " if mode == "off" else "") + "🔕 Kapalı", "mode:off")]]
    if mode == "bulletin":
        rows.append([(("✅ " if u["bulletin_hour"] == h else "") + f"{h:02d}:00", f"bh:{h}") for h in BULLETIN_HOURS])
    return text, kb(*rows)


@router.message(Command("ayarlar"))
@router.message(F.text == "🔔 Bildirim Ayarları")
async def cmd_settings(m: Message):
    ensure_user(m)
    text, markup = settings_screen(m.from_user.id)
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith(("set:", "mode:", "bh:")))
async def cb_settings(c: CallbackQuery):
    ensure_user(c)
    uid = c.from_user.id
    kind, val = c.data.split(":")
    if kind == "mode":
        store.update_user(uid, mode=val)
    elif kind == "bh":
        store.update_user(uid, bulletin_hour=int(val))
    elif val == "cats":
        await c.answer()
        return await c.message.edit_text("📂 <b>Takip ettiğin konular</b>",
                                         reply_markup=cats_kb(user_cats(store.user(uid)), "set:main"))
    elif val == "kw":
        await c.answer()
        text, markup = keywords_screen(uid)
        return await c.message.edit_text(text, reply_markup=markup)
    await c.answer()
    text, markup = settings_screen(uid)
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass


# ======================= yardım / gizlilik / menü metinleri =======================
@router.message(Command("yardim"))
async def cmd_help(m: Message):
    await m.answer("ℹ️ <b>Nasıl kullanılır?</b>\n\n"
                   "📰 Menüden kategori seçerek son haberleri gör, ⭐ ile kaydet.\n"
                   "🔎 <b>/ara</b> polis alımı — haberlerde ara\n"
                   "🔑 <b>/alarm</b> şoför — bu kelime geçen yeni haberleri bildir\n"
                   "🔔 <b>/ayarlar</b> — konular, anında bildirim veya günlük bülten\n\n"
                   "Haberler Türkiye'deki haber sitelerinden otomatik derlenir; başvuru öncesi "
                   "ilanın resmi kaynağını mutlaka kontrol et.", reply_markup=MENU)


@router.message(Command("gizlilik"))
async def cmd_privacy(m: Message):
    await m.answer("🔒 <b>Gizlilik</b>\n\nSadece Telegram kullanıcı adın, adın, dil tercihin ve bot içindeki "
                   "ayarların (konular, alarm kelimeleri, kaydettiğin haberler) saklanır. Bu bilgiler yalnızca sana "
                   "haber göndermek için kullanılır, üçüncü kişilerle paylaşılmaz.\n\n"
                   "Bildirimleri durdurmak için 🔔 Bildirim Ayarları → 🔕 Kapalı. Botu engellemen de yeterlidir.")


@router.message(F.text.in_(set(MENU_CATS)))
async def menu_cat(m: Message):
    ensure_user(m)
    cat = MENU_CATS[m.text]
    await send_list(m, [cat] if cat else (user_cats(store.user(m.from_user.id)) or None), 0, m.from_user.id)


@router.message(F.text & ~F.text.startswith("/"))
async def free_text(m: Message):
    ensure_user(m)
    mode = awaiting.pop(m.from_user.id, None)
    if mode == "keyword":
        return await m.answer(await add_keyword(m.from_user.id, m.text))
    # varsayılan: yazılanı arama kabul et
    await do_search(m, m.text)


@router.my_chat_member(F.chat.type == "private")
async def on_member(ev: ChatMemberUpdated):
    st = ev.new_chat_member.status
    if st == "kicked":
        store.update_user(ev.from_user.id, blocked=1)
    elif st == "member":
        store.update_user(ev.from_user.id, blocked=0)


# ======================= gönderim yardımcıları =======================
async def safe_send(bot: Bot, uid: int, text: str, markup=None) -> bool:
    for _ in range(3):
        try:
            await bot.send_message(uid, text, reply_markup=markup, disable_web_page_preview=True)
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            store.update_user(uid, blocked=1)
            return False
        except TelegramBadRequest as e:
            if "chat not found" in str(e).lower():
                store.update_user(uid, blocked=1)
            return False
        except Exception as e:
            log.warning("Gönderim hatası %s: %s", uid, e)
            await asyncio.sleep(1)
    return False


def matches(u, it) -> bool:
    if it["cat"] in user_cats(u):
        return True
    t = it["title"].lower()
    return any(k in t for k in user_keywords(u))


# ======================= servis =======================
class Service:
    def __init__(self):
        self.bot: Bot | None = None
        self.dp: Dispatcher | None = None
        self.poll_task: asyncio.Task | None = None
        self.token = None
        self.bot_id = None

    # ---- token / kimlik ----
    async def resolve_token(self):
        """Hangi bot? Önce Merkez'deki '🔗 servise bağla' ayarı (settings.ext_isharberi), yoksa ISHABER_USERNAME."""
        if not DB_URL:
            return None
        try:
            conn = await asyncpg.connect(DB_URL)
            try:
                linked = None
                try:
                    linked = await conn.fetchval("SELECT value FROM settings WHERE key='ext_isharberi'")
                except Exception:
                    pass
                if linked and linked.strip().lstrip("-").isdigit():
                    row = await conn.fetchrow("SELECT id, token, username FROM bots WHERE id=$1", int(linked))
                    if row:
                        return row
                if USERNAME:
                    return await conn.fetchrow("SELECT id, token, username FROM bots WHERE lower(username)=$1", USERNAME)
                return None
            finally:
                await conn.close()
        except Exception as e:
            log.warning("Merkez veritabanına ulaşılamadı: %s", e)
            return None

    def make_bot(self, token):
        kw = {"default": DefaultBotProperties(parse_mode="HTML")}
        if TELEGRAM_API:
            kw["session"] = AiohttpSession(api=TelegramAPIServer.from_base(TELEGRAM_API))
        return Bot(token, **kw)

    def mode(self) -> str:
        try:
            return "B" if (DATA / "MODE").read_text().strip().upper() == "B" else "A"
        except FileNotFoundError:
            return "A"

    # ---- profil ----
    async def setup_profile(self):
        """İsim, açıklama, about, komutlar ve profil fotoğrafı (sadece A modunda; fotoğraf bir kez)."""
        b = self.bot
        try:
            if store.get("profile_v") != "1":
                await b.set_my_name(name=BOT_NAME)
                await b.set_my_description(description=DESCRIPTION)
                await b.set_my_short_description(short_description=ABOUT)
                store.set("profile_v", "1")
            await b.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
        except Exception as e:
            log.warning("Profil metinleri ayarlanamadı: %s", e)
        if store.get("photo_v") != "1":
            try:
                from aiogram.types import InputProfilePhotoStatic
                from avatar import make_avatar
                await b.set_my_profile_photo(photo=InputProfilePhotoStatic(
                    photo=BufferedInputFile(make_avatar(), filename="avatar.png")))
                store.set("photo_v", "1")
                log.info("Profil fotoğrafı ayarlandı.")
            except Exception as e:
                log.warning("Profil fotoğrafı ayarlanamadı: %s", e)

    # ---- polling ----
    async def start_polling(self):
        if self.poll_task and not self.poll_task.done():
            return
        self.dp = Dispatcher()
        self.dp.include_router(router)
        await self.setup_profile()
        self.poll_task = asyncio.create_task(self.dp.start_polling(
            self.bot, handle_signals=False, close_bot_session=False,
            allowed_updates=["message", "callback_query", "my_chat_member"]))
        log.info("A modu: @%s dinleniyor.", USERNAME)

    async def stop_polling(self):
        if self.poll_task and not self.poll_task.done():
            try:
                await asyncio.wait_for(self.dp.stop_polling(), timeout=10)
            except Exception:
                self.poll_task.cancel()
            log.info("B modu: dinleme bırakıldı, Bot Merkezi yürütüyor.")
        self.poll_task = None

    # ---- Merkez ile dosya iletişimi ----
    def write(self, name, text):
        tmp = DATA / (name + ".tmp")
        tmp.write_text(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, DATA / name)

    def export_users(self):
        data = [{"user_id": u["user_id"], "username": u["username"], "first_name": u["first_name"],
                 "lang": u["lang"], "joined_at": u["joined_at"], "ref": u["ref"], "blocked": bool(u["blocked"])}
                for u in store.all_users()]
        self.write("users.json", json.dumps(data, ensure_ascii=False))

    async def import_merkez_subscribers(self, bot_id: int):
        """Bot ilk bağlandığında Merkez'deki mevcut abonelerini bir kez içe aktarır."""
        if store.get(f"imported_{bot_id}") == "1":
            return
        try:
            conn = await asyncpg.connect(DB_URL)
            try:
                rows = await conn.fetch("SELECT user_id, username, first_name, lang, source, status, joined_at "
                                        "FROM subscribers WHERE bot_id=$1", bot_id)
            finally:
                await conn.close()
        except Exception as e:
            log.warning("Merkez aboneleri okunamadı: %s", e)
            return
        n = sum(store.import_user(r["user_id"], r["username"], r["first_name"], r["lang"], r["status"] != "active",
                                  int(r["joined_at"].timestamp()) if r["joined_at"] else None, r["source"])
                for r in rows)
        store.set(f"imported_{bot_id}", "1")
        log.info("Merkez'deki %d abone içe aktarıldı (%d yeni).", len(rows), n)

    def import_from_merkezi(self):
        p = DATA / "from_merkezi.json"
        if not p.exists():
            return
        try:
            n = sum(store.import_user(int(x["user_id"]), x.get("username"), x.get("first_name"), x.get("lang"),
                                      x.get("status") != "active") for x in json.loads(p.read_text()))
            log.info("Merkez'den %d yeni abone alındı.", n)
        except Exception as e:
            log.warning("from_merkezi.json okunamadı: %s", e)
        p.rename(DATA / "from_merkezi.imported.json")

    # ---- haber toplama ve bildirim ----
    async def fetch_loop(self):
        async with aiohttp.ClientSession() as http:
            while True:
                new = []
                for cat in CATS:
                    for it in await fetch_cat(http, cat):
                        if store.add_item(it):
                            new.append(it)
                    await asyncio.sleep(2)
                store.prune()
                first = store.get("init_done") != "1"
                store.set("init_done", "1")
                log.info("Haber taraması: %d yeni%s", len(new), " (ilk tarama, bildirim yok)" if first else "")
                if new and not first and self.bot and self.mode() == "A":
                    try:
                        await self.instant_alerts(new)
                    except Exception:
                        log.exception("Anlık bildirim hatası")
                await asyncio.sleep(FETCH_EVERY)

    async def instant_alerts(self, new: list[dict]):
        now = datetime.now(TZ)
        if now.hour >= QUIET[0] or now.hour < QUIET[1]:
            return
        today = now.strftime("%Y-%m-%d")
        new = sorted(new, key=lambda x: -x["published"])
        sent = 0
        for u in store.active_users():
            if u["mode"] != "instant" or not u["onboarded"]:
                continue
            count = u["alerts_today"] if u["alert_day"] == today else 0
            if count >= DAILY_CAP:
                continue
            mine = [it for it in new if matches(u, it)][:3]
            if not mine:
                continue
            head = "🔔 <b>Yeni iş haberi</b>" if len(mine) == 1 else f"🔔 <b>{len(mine)} yeni iş haberi</b>"
            text = head + "\n\n" + "\n\n".join(fmt_item(it) for it in mine)
            markup = kb([(f"⭐ Kaydet{' ' + str(i + 1) if len(mine) > 1 else ''}", f"sv:{it['id']}") for i, it in enumerate(mine)])
            if await safe_send(self.bot, u["user_id"], text, markup):
                store.update_user(u["user_id"], alert_day=today, alerts_today=count + 1)
                sent += 1
            await asyncio.sleep(0.05)
        if sent:
            log.info("Anlık bildirim: %d kullanıcı", sent)

    async def bulletin_loop(self):
        while True:
            await asyncio.sleep(60)
            if not self.bot or self.mode() != "A":
                continue
            now = datetime.now(TZ)
            today = now.strftime("%Y-%m-%d")
            for u in store.active_users():
                if u["mode"] != "bulletin" or u["bulletin_hour"] != now.hour or u["last_bulletin"] == today:
                    continue
                store.update_user(u["user_id"], last_bulletin=today)
                items = [it for it in store.latest(None, limit=60, since=int(time.time()) - 86400) if matches(u, it)][:8]
                if not items:
                    continue
                text = (f"📰 <b>Günlük İş Bülteni</b> • {now:%d.%m.%Y}\n\n"
                        + "\n\n".join(fmt_item(it, i + 1) for i, it in enumerate(items)))
                markup = kb([(f"⭐ {i + 1}", f"sv:{it['id']}") for i, it in enumerate(items[:5])])
                await safe_send(self.bot, u["user_id"], text, markup)
                await asyncio.sleep(0.05)

    # ---- ana döngü ----
    async def run(self):
        asyncio.create_task(self.fetch_loop())
        asyncio.create_task(self.bulletin_loop())
        last_export = 0
        while True:
            row = await self.resolve_token()
            if not row:
                log.info("Henüz bir bota bağlanmadı (Merkez → bot paneli → 🔗 İş Haberleri servisine bağla). "
                         "Bu sırada haber toplanıyor.")
                await asyncio.sleep(20)
                continue
            if row["token"] != self.token:  # ilk açılış veya token yenilendi
                await self.stop_polling()
                if self.bot:
                    await self.bot.session.close()
                self.token, self.bot_id = row["token"], row["id"]
                self.bot = self.make_bot(self.token)
                await self.import_merkez_subscribers(row["id"])
                self.write("BOT_ID", f"{row['id']} {row['username']}")
                log.info("Bot bağlandı: @%s (%s)", row["username"], row["id"])
            for _ in range(12):  # ~1 dk boyunca 5 sn'de bir mod kontrolü, sonra bağlantı/token tekrar okunur
                self.import_from_merkezi()
                if self.mode() == "A":
                    await self.start_polling()
                else:
                    await self.stop_polling()
                if time.time() - last_export > 60:
                    self.export_users()
                    last_export = time.time()
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(Service().run())
