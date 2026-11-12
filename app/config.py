import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
_storage = os.environ.get("STORAGE_CHAT_ID", "").strip()
STORAGE_CHAT_ID = int(_storage) if _storage else 0
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///data.db")
SEND_RATE = float(os.environ.get("SEND_RATE", "25"))
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Istanbul"))

# Tıklama takibi: linkler bu adres üzerinden yönlendirilir (örn. https://t.site.com). Boşsa takip kapalı.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
# Telegram'a doğrudan ulaşılamıyorsa aracı adres (ör. Cloudflare Worker): https://tg.ornek.workers.dev
TELEGRAM_API = os.environ.get("TELEGRAM_API", "").strip().rstrip("/")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
TRACK_SECRET = os.environ.get("TRACK_SECRET", "") or ADMIN_BOT_TOKEN
