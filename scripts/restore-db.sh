#!/usr/bin/env bash
# Restore the production database from a dump made by backup-db.sh.
#
#   scripts/restore-db.sh backups/places-20261119T031500Z.dump --yes
#   scripts/restore-db.sh s3://<bucket>/db/places-20261119T031500Z.dump --yes
#   aws s3 ls s3://<bucket>/db/        # list what's in S3
#
# Replaces everything in the database with the dump's contents. The backend
# and frontend are stopped meanwhile, so nothing writes halfway through,
# and started again afterwards.
set -euo pipefail

source_path="${1:-}"
if [[ -z "$source_path" || "${2:-}" != "--yes" ]]; then
  echo "usage: $0 <dump file or s3:// URI> --yes   (replaces the current database)" >&2
  exit 2
fi

cd "$(dirname "$0")/.."
ENV_FILE="${ENV_FILE:-.env}"
COMPOSE=(docker compose -f docker-compose.prod.yml --env-file "$ENV_FILE")
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [[ "$source_path" == s3://* ]]; then
  mkdir -p backups
  file="backups/$(basename "$source_path")"
  aws s3 cp --only-show-errors "$source_path" "$file"
else
  file="$source_path"
fi
[[ -f "$file" ]] || { echo "no such file: $file" >&2; exit 1; }

echo "--- checking $file"
"${COMPOSE[@]}" exec -T db pg_restore --list < "$file" > /dev/null

echo "--- stopping the app"
"${COMPOSE[@]}" stop backend frontend

echo "--- restoring"
# --clean --if-exists: drop each object before recreating it.
# --single-transaction: all or nothing, so a failure leaves the old data.
"${COMPOSE[@]}" exec -T db pg_restore --clean --if-exists --no-owner --single-transaction \
  -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$file"

echo "--- starting the app"
"${COMPOSE[@]}" up -d --wait backend frontend

"${COMPOSE[@]}" exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc \
  "select 'users: ' || count(*) from users union all select 'places: ' || count(*) from places
   union all select 'migration: ' || version_num from alembic_version"
echo "restored from $source_path"
