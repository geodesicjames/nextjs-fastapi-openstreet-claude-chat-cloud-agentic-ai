#!/usr/bin/env bash
# Back up the production database to a compressed dump (pg_dump custom format).
#
#   scripts/backup-db.sh                 # writes backups/places-<UTC time>.dump
#   BACKUP_S3_URI=s3://bucket/path scripts/backup-db.sh   # ...and uploads it
#
# On the server, systemd runs this nightly (ops/systemd/places-backup.*) with
# BACKUP_S3_URI from the env file, which the deploy writes from Parameter
# Store. Restore with scripts/restore-db.sh.
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE="${ENV_FILE:-.env}"
COMPOSE=(docker compose -f docker-compose.prod.yml --env-file "$ENV_FILE")
# Keep this many days of dumps on the server too (S3 keeps them longer).
KEEP_LOCAL_DAYS="${KEEP_LOCAL_DAYS:-7}"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

mkdir -p backups
file="backups/places-$(date -u +%Y%m%dT%H%M%SZ).dump"
"${COMPOSE[@]}" exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom > "$file"

# A dump that pg_restore can't read is not a backup: check it before trusting it.
"${COMPOSE[@]}" exec -T db pg_restore --list < "$file" > /dev/null
echo "Wrote $file ($(du -h "$file" | cut -f1)), readable by pg_restore"

if [[ -n "${BACKUP_S3_URI:-}" ]]; then
  aws s3 cp --only-show-errors "$file" "${BACKUP_S3_URI%/}/$(basename "$file")"
  echo "Uploaded to ${BACKUP_S3_URI%/}/"
fi

find backups -name 'places-*.dump' -mtime +"$KEEP_LOCAL_DAYS" -print -delete
