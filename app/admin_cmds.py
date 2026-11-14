"""Yönetici bot: botların '/' komut listesi (Menü düğmesi) ve komut yanıtları."""
import html
import re

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .admin import ALBUM_HELP, BUTTON_HELP, kb, preview, show, take_content
from .automsg import sync_commands
from .config import ADMIN_IDS
from .db import ManagedBot, Session
from .keyboards import parse_buttons, split_btn_text
from .manager import BotManager

router = Router()
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

RESERVED = {"start", "stop", "promo"}
CMD_RE = re.compile(r"^[a-z0-9_]{1,32}$")
TR_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


class CmdSt(StatesGroup):
    name = State()
    msg = State()
    buttons = State()


async def _bot(bid: int) -> ManagedBot:
    async with Session() as s:
        return await s.get(ManagedBot, bid)


async def _save(bid: int, cmds: list, manager: BotManager):
    async with Session() as s:
        b = await s.get(ManagedBot, bid)
        b.commands = cmds or None
        await s.commit()
    if manager.sender(bid):
        await sync_commands(manager.sender(bid))


async def cmd_screen(bid: int):
    b = await _bot(bid)
    from .external import ext_dir, label, mode as ext_mode
    xd = ext_dir(b)
    cmds = b.commands or []
    lines = [f"⌨️ <b>@{html.escape(b.username)} — Komutlar</b>",
             "Botun <b>Menü</b> düğmesinde ve / yazınca çıkan liste.", ""]
    if xd and ext_mode(xd) == "A":
        lines += [f"🅰️ Bot A modunda: bu liste şu an {label(xd)} servisine ait. "
                  "Burada hazırladıkların <b>B moduna geçince</b> devreye girer.", ""]
    lines.append("/start — Başlat <i>(sabit)</i>")
    rows = []
    for i, c in enumerate(cmds):
        kind = "🎁 sıradaki promo" if c.get("action") == "promo" else "💬 mesaj"
        lines.append(f"/{c['cmd']} — {html.escape(c['desc'])}  <i>({kind})</i>")
        rows.append([(f"/{c['cmd']}", f"cmdv:{bid}:{i}")])
    lines += ["/promo — Güncel promo <i>(havuzda promo varsa, sabit)</i>",
              "/stop — Bildirimleri kapat <i>(sabit)</i>"]
    rows.append([("➕ Komut ekle", f"cmd+:{bid}")])
    rows.append([("⬅️ Bot paneli", f"bot:{bid}")])
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data.startswith("cmd:"))
async def cb_cmds(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, *(await cmd_screen(int(c.data.split(":")[1]))))


# ---------------- ekleme ----------------
@router.callback_query(F.data.startswith("cmd+:"))
async def cb_add(c: CallbackQuery, state: FSMContext):
    bid = int(c.data.split(":")[1])
    await state.set_state(CmdSt.name)
    await state.update_data(bot_id=bid)
    await show(c, "➕ Komutu ve açıklamasını yaz:\n\n<code>giris - Güncel giriş adresi</code>\n\n"
                  "• Komut: küçük harf, rakam ve _ (Türkçe karakterler otomatik düzeltilir)\n"
                  "• Açıklama: Menü listesinde komutun yanında görünür\nİptal: /iptal")


@router.message(CmdSt.name, F.text)
async def on_name(m: Message, state: FSMContext):
    if " - " not in m.text:
        await m.answer("❌ Format: <code>komut - açıklama</code>")
        return
    cmd, desc = m.text.split(" - ", 1)
    cmd = cmd.strip().lstrip("/").translate(TR_MAP).lower().replace(" ", "_")
    desc = desc.strip()
    if not CMD_RE.match(cmd):
        await m.answer("❌ Komut sadece küçük harf, rakam ve _ içerebilir (en fazla 32 karakter).")
        return
    if cmd in RESERVED:
        await m.answer(f"❌ /{cmd} sistemde zaten var, başka bir ad seç.")
        return
    if not 1 <= len(desc) <= 256:
        await m.answer("❌ Açıklama 1–256 karakter olmalı.")
        return
    b = await _bot((await state.get_data())["bot_id"])
    if any(x["cmd"] == cmd for x in (b.commands or [])):
        await m.answer(f"❌ /{cmd} zaten ekli.")
        return
    await state.update_data(cmd=cmd, desc=desc)
    await state.set_state(CmdSt.msg)
    await m.answer(f"/{cmd} yazılınca ne gönderilsin?\n\nMesajı gönder (metin, foto, video, albüm).\n"
                   "🎁 Sıradaki promoyu göstersin istiyorsan <code>promo</code> yaz.")


@router.message(CmdSt.msg)
async def on_msg(m: Message, state: FSMContext, manager: BotManager):
    data = await state.get_data()
    if m.text and m.text.strip().lower() in ("promo", "/promo"):
        b = await _bot(data["bot_id"])
        cmds = list(b.commands or []) + [{"cmd": data["cmd"], "desc": data["desc"], "action": "promo"}]
        await _save(data["bot_id"], cmds, manager)
        await state.clear()
        await m.answer(f"✅ /{data['cmd']} eklendi: sıradaki promoyu gösterecek.")
        text, markup = await cmd_screen(data["bot_id"])
        await m.answer(text, reply_markup=markup)
        return
    msg_ids = await take_content(m, state)
    if msg_ids is None:
        return
    await state.update_data(msgs=msg_ids)
    await state.set_state(CmdSt.buttons)
    await m.answer("🔘 Altına butonları ekle.\n\n" + BUTTON_HELP + (ALBUM_HELP if len(msg_ids) > 1 else ""))


@router.message(CmdSt.buttons, F.text)
async def on_buttons(m: Message, state: FSMContext, bot: Bot, manager: BotManager):
    btn_text, rest = split_btn_text(m.text)
    try:
        buttons = parse_buttons(rest)
    except ValueError as e:
        await m.answer(f"❌ {html.escape(str(e))}")
        return
    data = await state.get_data()
    b = await _bot(data["bot_id"])
    cmds = list(b.commands or []) + [{"cmd": data["cmd"], "desc": data["desc"], "msgs": data["msgs"],
                                      "buttons": buttons or None, "btn_text": btn_text}]
    await _save(data["bot_id"], cmds, manager)
    await state.clear()
    await m.answer(f"✅ /{data['cmd']} eklendi. Kullanıcı yazınca şunu görecek:")
    await preview(bot, m.chat.id, data["msgs"], buttons, btn_text=btn_text)
    text, markup = await cmd_screen(data["bot_id"])
    await m.answer(text, reply_markup=markup)


# ---------------- tek komut ----------------
@router.callback_query(F.data.startswith(("cmdv:", "cmdp:", "cmdu:", "cmdx:", "cmdxo:")))
async def cb_item(c: CallbackQuery, bot: Bot, manager: BotManager):
    action, bid, idx = c.data.split(":")
    bid, idx = int(bid), int(idx)
    b = await _bot(bid)
    cmds = list(b.commands or [])
    if idx >= len(cmds):
        await show(c, *(await cmd_screen(bid)))
        return
    cmd = cmds[idx]
    if action == "cmdxo":
        cmds.pop(idx)
        await _save(bid, cmds, manager)
        await show(c, *(await cmd_screen(bid)))
        return
    if action == "cmdu" and idx > 0:
        cmds[idx - 1], cmds[idx] = cmds[idx], cmds[idx - 1]
        await _save(bid, cmds, manager)
        await show(c, *(await cmd_screen(bid)))
        return
    if action == "cmdx":
        await show(c, f"🗑 /{cmd['cmd']} silinsin mi?", kb([("✅ Evet", f"cmdxo:{bid}:{idx}"), ("❌ Vazgeç", f"cmdv:{bid}:{idx}")]))
        return
    if action == "cmdp":
        await c.answer()
        if cmd.get("action") == "promo":
            await c.message.answer("🎁 Bu komut botun /promo havuzundan sıradaki promoyu gösterir.")
        else:
            await preview(bot, c.message.chat.id, cmd["msgs"], cmd.get("buttons"), btn_text=cmd.get("btn_text"))
    kind = "🎁 Sıradaki promoyu gösterir" if cmd.get("action") == "promo" else "💬 Mesaj gönderir"
    text = f"⌨️ <b>/{cmd['cmd']}</b> — {html.escape(cmd['desc'])}\n{kind}"
    markup = kb([("👁 Önizle", f"cmdp:{bid}:{idx}"), ("⬆️ Yukarı", f"cmdu:{bid}:{idx}")],
                [("🗑 Sil", f"cmdx:{bid}:{idx}"), ("⬅️ Komutlar", f"cmd:{bid}")])
    if action == "cmdp":
        await c.message.answer(text, reply_markup=markup)
    else:
        await show(c, text, markup)
