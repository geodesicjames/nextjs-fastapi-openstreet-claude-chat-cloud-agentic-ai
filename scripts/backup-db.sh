#!/usr/bin/env bash
# Back up the production database to a compressed dump (pg_dump custom format).
#
#   scripts/backup-db.sh                 # writes backups/places-<UTC time>.dump
#   BACKUP_S3_URI=s3://bucket/path scripts/backup-db.sh   # ...and uploads it
#
# Restore into the running stack (replaces existing objects):
#   docker compose -f docker-compose.prod.yml exec -T db \
#     pg_restore --clean --if-exists --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB" < backups/<file>.dump
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f docker-compose.prod.yml)
set -a
# shellcheck disable=SC1090
source "${ENV_FILE:-.env}"
set +a

mkdir -p backups
file="backups/places-$(date -u +%Y%m%dT%H%M%SZ).dump"
"${COMPOSE[@]}" exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom > "$file"
echo "Wrote $file ($(du -h "$file" | cut -f1))"

if [[ -n "${BACKUP_S3_URI:-}" ]]; then
  aws s3 cp "$file" "${BACKUP_S3_URI%/}/$(basename "$file")"
  echo "Uploaded to ${BACKUP_S3_URI%/}/"
fi
