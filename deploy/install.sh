#!/usr/bin/env bash
# Bot Kontrol Merkezi — sunucu kurulumu (Ubuntu 24.04). Root olarak çalıştır.
set -euo pipefail
DIR=/opt/botmerkezi
cd "$DIR"
say() { echo -e "\n\033[1;36m▶ $*\033[0m"; }

say "Sistem paketleri güncelleniyor…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -qq
apt-get install -y -qq curl git ca-certificates jq >/dev/null

if ! command -v docker >/dev/null; then
  say "Docker kuruluyor…"
  curl -fsSL https://get.docker.com | sh >/dev/null
fi

say "Docker log boyutu sınırlanıyor…"
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'J'
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
J
systemctl restart docker

if ! swapon --show | grep -q /swapfile; then
  say "1 GB swap ekleniyor…"
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

if [ ! -f .env ]; then
  say "Ayarlar"
  while true; do
    read -rp "Yönetici bot token'ı (BotFather): " TOKEN </dev/tty
    NAME=$(curl -s "https://api.telegram.org/bot${TOKEN}/getMe" | jq -r '.result.username // empty')
    [ -n "$NAME" ] && { echo "✔ @$NAME"; break; } || echo "✖ Token geçersiz, tekrar dene."
  done
  read -rp "Senin Telegram ID'n (@userinfobot; birden fazlaysa virgülle): " IDS </dev/tty
  PGPASS=$(openssl rand -hex 16)
  SECRET=$(openssl rand -hex 24)
  cat > .env <<E
ADMIN_BOT_TOKEN=${TOKEN}
ADMIN_IDS=${IDS}
STORAGE_CHAT_ID=
POSTGRES_PASSWORD=${PGPASS}
DATABASE_URL=postgresql+asyncpg://botmerkezi:${PGPASS}@db:5432/botmerkezi
SEND_RATE=25
TZ_NAME=Europe/Istanbul
PUBLIC_URL=
WEB_PORT=8080
TRACK_SECRET=${SECRET}
E
  chmod 600 .env
fi

say "Uygulama derleniyor ve başlatılıyor (birkaç dakika sürebilir)…"
docker compose up -d --build

say "Otomatik güncelleme ve günlük yedek zamanlanıyor…"
chmod +x deploy/*.sh
cat > /etc/systemd/system/botmerkezi-update.service <<U
[Unit]
Description=Bot Merkezi otomatik güncelleme
[Service]
Type=oneshot
ExecStart=$DIR/deploy/update.sh
U
cat > /etc/systemd/system/botmerkezi-update.timer <<U
[Unit]
Description=Bot Merkezi güncelleme kontrolü (3 dk)
[Timer]
OnBootSec=2min
OnUnitActiveSec=3min
[Install]
WantedBy=timers.target
U
cat > /etc/systemd/system/botmerkezi-backup.service <<U
[Unit]
Description=Bot Merkezi günlük yedek
[Service]
Type=oneshot
ExecStart=$DIR/deploy/backup.sh
U
cat > /etc/systemd/system/botmerkezi-backup.timer <<U
[Unit]
Description=Bot Merkezi günlük yedek (04:00)
[Timer]
OnCalendar=*-*-* 04:00:00 Europe/Istanbul
Persistent=true
[Install]
WantedBy=timers.target
U
systemctl daemon-reload
systemctl enable --now botmerkezi-update.timer botmerkezi-backup.timer >/dev/null

OK=0
for _ in $(seq 1 24); do
  sleep 5
  curl -fs http://127.0.0.1:8080/health >/dev/null && { OK=1; break; }
done
if [ "$OK" = 1 ]; then
  source <(grep -E '^(ADMIN_BOT_TOKEN|ADMIN_IDS)=' .env)
  for id in ${ADMIN_IDS//,/ }; do
    curl -s "https://api.telegram.org/bot${ADMIN_BOT_TOKEN}/sendMessage" -d chat_id="$id" \
      --data-urlencode text="✅ Bot Kontrol Merkezi kuruldu ve çalışıyor! /start yaz." >/dev/null || true
  done
  say "KURULUM TAMAM ✅  Yönetici bota Telegram'dan /start yaz."
else
  say "Uygulama henüz yanıt vermiyor. Şu çıktıyı Claude'a gönder:"
  docker compose logs --tail 40 app
fi
