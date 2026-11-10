"""Buton metni ayrıştırma ve klavye oluşturma.

Format (her satır bir buton sırası, | ile yan yana):
    🟢 Hemen Katıl - https://site.com
    🔴 Son Gün - https://site.com/bonus | 🔵 Kanal - https://t.me/kanal
Başa 🟢 (yeşil), 🔴 (kırmızı), 🔵 (mavi) koyarsan buton o renkte olur.
"""
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardMarkup)

STYLE_PREFIX = {"🟢": "success", "🔴": "danger", "🔵": "primary"}
STYLE_EMOJI = {v: k for k, v in STYLE_PREFIX.items()}
SKIP_WORDS = {"-", "yok", "geç", "gec", "skip"}


def _norm_url(url: str) -> str:
    url = url.strip()
    if url.startswith(("t.me/", "telegram.me/")):
        url = "https://" + url
    if not url.startswith(("http://", "https://", "tg://")):
        raise ValueError(f"Geçersiz link: {url}")
    return url


def parse_buttons(text: str) -> list[list[dict]]:
    """Metni [[{text,url,style}]] yapısına çevirir. Hatalı satırda ValueError."""
    if text.strip().lower() in SKIP_WORDS:
        return []
    rows: list[list[dict]] = []
    for line_no, line in enumerate(text.strip().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        row = []
        for part in line.split("|"):
            part = part.strip()
            if " - " not in part:
                raise ValueError(f"{line_no}. satır hatalı: «{part}»\nFormat: Buton yazısı - https://link")
            label, url = part.rsplit(" - ", 1)
            label = label.strip()
            style = None
            for emo, st in STYLE_PREFIX.items():
                if label.startswith(emo):
                    style = st
                    label = label[len(emo):].strip()
                    break
            if not label:
                raise ValueError(f"{line_no}. satırda buton yazısı boş.")
            btn = {"text": label, "url": _norm_url(url)}
            if style:
                btn["style"] = style
            row.append(btn)
        rows.append(row)
    if sum(len(r) for r in rows) > 100:
        raise ValueError("En fazla 100 buton eklenebilir.")
    return rows


def split_btn_text(text: str) -> tuple[str | None, str]:
    """İlk satır 'Yazı: ...' ise albüm altındaki buton mesajının metni olarak ayırır."""
    lines = text.strip().splitlines()
    if lines and lines[0].lower().startswith(("yazı:", "yazi:")):
        return lines[0].split(":", 1)[1].strip() or None, "\n".join(lines[1:]) or "-"
    return None, text


def buttons_to_text(rows: list[list[dict]] | None) -> str:
    if not rows:
        return "—"
    out = []
    for row in rows:
        out.append(" | ".join(
            f"{STYLE_EMOJI.get(b.get('style'), '')}{' ' if b.get('style') else ''}{b['text']} - {b['url']}"
            for b in row))
    return "\n".join(out)


def build_markup(rows: list[list[dict]] | None, unsub: bool = False,
                 url_fn=None) -> InlineKeyboardMarkup | None:
    """url_fn(button) verilirse link onunla değiştirilir (tıklama takibi)."""
    def url_of(b):
        return url_fn(b) if url_fn and b.get("lid") else b["url"]
    kb = [[InlineKeyboardButton(text=b["text"], url=url_of(b), style=b.get("style")) for b in row]
          for row in (rows or [])]
    if unsub:
        kb.append([InlineKeyboardButton(text="🔕 Bildirimleri kapat", callback_data="unsub")])
    return InlineKeyboardMarkup(inline_keyboard=kb) if kb else None


def parse_menu(text: str) -> list[list[str]]:
    if text.strip().lower() in SKIP_WORDS:
        return []
    rows = []
    for line in text.strip().splitlines():
        row = [p.strip()[:64] for p in line.split("|") if p.strip()]
        if row:
            rows.append(row)
    return rows


def build_reply_menu(rows: list[list[str]] | None) -> ReplyKeyboardMarkup | None:
    if not rows:
        return None
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True, is_persistent=True)
