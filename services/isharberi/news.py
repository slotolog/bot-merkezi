"""Haber kaynakları: Google News RSS (Türkiye), yedek olarak Bing News RSS."""
import calendar
import hashlib
import html
import logging
import re
import time
import urllib.parse

import aiohttp
import feedparser

log = logging.getLogger("isharberi.news")

# key: (emoji, ad, arama sorgusu)
CATS = {
    "kamu":  ("🏛", "Kamu Alımları", '"personel alımı" OR "memur alımı" OR "sözleşmeli personel" OR "kamu işçi alımı"'),
    "ozel":  ("🏭", "Özel Sektör", '"iş ilanı" OR "işçi alımı" OR "eleman alımı" OR "istihdam edecek"'),
    "iskur": ("🏢", "İŞKUR & Kurslar", 'İŞKUR OR "iş kursu" OR "işbaşı eğitim programı" OR "TYP"'),
    "staj":  ("🎓", "Staj & Yeni Mezun", '"staj programı" OR "stajyer alımı" OR "yeni mezun" OR "MT programı"'),
    "maas":  ("💰", "Maaş & Asgari Ücret", '"asgari ücret" OR "memur maaşı" OR "maaş zammı" OR "kıdem tazminatı"'),
    "gundem": ("📊", "Çalışma Hayatı", 'işsizlik OR "çalışma hayatı" OR "istihdam" OR "uzaktan çalışma"'),
}
DEFAULT_CATS = ["kamu", "ozel", "iskur", "staj", "maas"]
UA = {"User-Agent": "Mozilla/5.0 (compatible; IsHaberleriBot/1.0)"}


def norm(title: str) -> str:
    t = title.lower()
    t = re.sub(r"[^\wçğıöşü ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def item_id(title: str) -> str:
    return hashlib.sha1(norm(title).encode()).hexdigest()[:16]


def google_url(q: str) -> str:
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q + " when:3d", "hl": "tr", "gl": "TR", "ceid": "TR:tr"})


def bing_url(q: str) -> str:
    return "https://www.bing.com/news/search?" + urllib.parse.urlencode({"q": q, "format": "rss", "cc": "tr", "setlang": "tr"})


def parse_feed(text: str, cat: str, max_age_days=3) -> list[dict]:
    feed = feedparser.parse(text)
    now = int(time.time())
    out = []
    for e in feed.entries:
        title = html.unescape(e.get("title", "")).strip()
        if not title:
            continue
        source = ""
        src = e.get("source")
        if isinstance(src, dict):
            source = src.get("title", "")
        if " - " in title:  # Google: "Başlık - Kaynak"
            head, tail = title.rsplit(" - ", 1)
            if len(tail) <= 40:
                title, source = head.strip(), source or tail.strip()
        pp = e.get("published_parsed") or e.get("updated_parsed")
        published = calendar.timegm(pp) if pp else now
        if published > now + 3600:
            published = now
        if now - published > max_age_days * 86400:
            continue
        link = e.get("link", "")
        if not link.startswith("http"):
            continue
        out.append({"id": item_id(title), "cat": cat, "title": title[:300], "source": source[:60],
                    "link": link, "published": published, "fetched": now})
    return out


async def fetch_cat(session: aiohttp.ClientSession, cat: str) -> list[dict]:
    q = CATS[cat][2]
    for url in (google_url(q), bing_url(q)):
        try:
            async with session.get(url, headers=UA, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                items = parse_feed(await r.text(), cat)
                if items:
                    return items
        except Exception as e:
            log.warning("%s kaynağı alınamadı (%s): %s", cat, url.split("/")[2], e)
    return []


def ago(ts: int) -> str:
    d = int(time.time()) - ts
    if d < 3600:
        return f"{max(1, d // 60)} dk önce"
    if d < 86400:
        return f"{d // 3600} saat önce"
    return f"{d // 86400} gün önce"
