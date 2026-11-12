#!/usr/bin/env bash
# GitHub'da yeni sürüm varsa günceller; açılmazsa eski sürüme döner. Telegram'dan haber verir.
set -uo pipefail
DIR=/opt/botmerkezi
cd "$DIR" || exit 1
exec 9>/tmp/botmerkezi-update.lock
flock -n 9 || exit 0

notify() {
  source <(grep -E '^(ADMIN_BOT_TOKEN|ADMIN_IDS|TELEGRAM_API)=' .env)
  for id in ${ADMIN_IDS//,/ }; do
    curl -s "${TELEGRAM_API:-https://api.telegram.org}/bot${ADMIN_BOT_TOKEN}/sendMessage" -d chat_id="$id" \
      --data-urlencode text="$1" >/dev/null || true
  done
}
healthy() {
  for _ in $(seq 1 12); do
    sleep 5
    curl -fs http://127.0.0.1:8080/health >/dev/null && return 0
  done
  return 1
}

git fetch -q origin main || exit 0
OLD=$(git rev-parse HEAD)
NEW=$(git rev-parse origin/main)
[ "$OLD" = "$NEW" ] && exit 0

CHANGES=$(git log --format='• %s' "$OLD..$NEW" | head -15)
git reset -q --hard "$NEW"
chmod +x deploy/*.sh
if docker compose up -d --build >/tmp/botmerkezi-build.log 2>&1 && healthy; then
  notify "✅ Sistem güncellendi ($(git rev-parse --short HEAD))
$CHANGES"
else
  ERR=$(docker compose logs --tail 15 app 2>&1 | tail -15)
  git reset -q --hard "$OLD"
  docker compose up -d --build >/dev/null 2>&1
  if healthy; then
    notify "⚠️ Yeni sürüm açılamadı, eski sürüme geri dönüldü. Sistem çalışıyor.
Claude'a ilet:
$ERR"
  else
    notify "🚨 Güncelleme başarısız ve eski sürüm de açılmadı. Claude'a ilet:
$ERR"
  fi
fi
