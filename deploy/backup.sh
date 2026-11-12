#!/usr/bin/env bash
# Veritabanı yedeğini alıp yönetici bota dosya olarak gönderir.
set -uo pipefail
cd /opt/botmerkezi || exit 1
source <(grep -E '^(ADMIN_BOT_TOKEN|ADMIN_IDS|TELEGRAM_API)=' .env)
F=/tmp/botmerkezi_$(date +%F).sql.gz
docker compose exec -T db pg_dump -U botmerkezi botmerkezi | gzip > "$F"
SIZE=$(du -h "$F" | cut -f1)
for id in ${ADMIN_IDS//,/ }; do
  curl -s "${TELEGRAM_API:-https://api.telegram.org}/bot${ADMIN_BOT_TOKEN}/sendDocument" -F chat_id="$id" \
    -F document=@"$F" -F caption="💾 Günlük yedek ($SIZE)" -F disable_notification=true >/dev/null
done
rm -f "$F"
