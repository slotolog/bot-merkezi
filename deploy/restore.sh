#!/usr/bin/env bash
# Kullanım: ./deploy/restore.sh botmerkezi_2026-11-10.sql.gz
set -euo pipefail
cd /opt/botmerkezi
[ -f "${1:-}" ] || { echo "Yedek dosyası ver: ./deploy/restore.sh dosya.sql.gz"; exit 1; }
docker compose stop app
docker compose exec -T db psql -U botmerkezi -d postgres -c "DROP DATABASE botmerkezi WITH (FORCE);" -c "CREATE DATABASE botmerkezi OWNER botmerkezi;"
gunzip -c "$1" | docker compose exec -T db psql -q -U botmerkezi botmerkezi
docker compose start app
echo "✅ Yedek geri yüklendi."
