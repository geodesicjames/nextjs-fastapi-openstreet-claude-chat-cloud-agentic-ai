#!/usr/bin/env bash
# Runs ON the app server, as root, via SSM Run Command (see
# .github/workflows/deploy.yml). The workflow first copies this script,
# docker-compose.prod.yml, the backup/restore scripts and ops/systemd/ into
# APP_DIR.
#
#   BACKEND_IMAGE=<ecr>/places-backend:<sha> FRONTEND_IMAGE=<ecr>/places-frontend:<sha> \
#     scripts/deploy-on-server.sh
#
# Steps: write the env file from SSM Parameter Store, log in to ECR, pull,
# migrate, start, wait until every service is healthy, install the nightly
# backup and cleanup timers, prune old images.
set -euo pipefail

: "${BACKEND_IMAGE:?}" "${FRONTEND_IMAGE:?}"
APP_DIR="${APP_DIR:-/srv/places/app}"
SSM_PREFIX="${SSM_PREFIX:-/places/prod/}"
ENV_FILE="$APP_DIR/.env"
# Ubuntu installs the AWS CLI as a snap; SSM's environment may not include it.
export PATH="$PATH:/snap/bin"
export AWS_REGION="${AWS_REGION:-$(cloud-init query region 2>/dev/null || echo eu-west-2)}"

cd "$APP_DIR"

echo "--- settings from SSM ($SSM_PREFIX)"
# One NAME=value line per parameter (the name's last path part). The file is
# readable by root only: it holds the database password and API key.
umask 077
aws ssm get-parameters-by-path --path "$SSM_PREFIX" --recursive --with-decryption \
  --query 'Parameters[].[Name,Value]' --output text |
  while IFS=$'\t' read -r name value; do
    printf '%s=%s\n' "${name##*/}" "$value"
  done > "$ENV_FILE.new"
echo "wrote $(wc -l < "$ENV_FILE.new") settings: $(cut -d= -f1 "$ENV_FILE.new" | sort | tr '\n' ' ')"
# Record the images too, so later commands on the server (docker compose
# exec, backups) see the same configuration without extra variables.
printf 'BACKEND_IMAGE=%s\nFRONTEND_IMAGE=%s\n' "$BACKEND_IMAGE" "$FRONTEND_IMAGE" >> "$ENV_FILE.new"
mv "$ENV_FILE.new" "$ENV_FILE"

echo "--- registry login"
registry="${BACKEND_IMAGE%%/*}"
aws ecr get-login-password | docker login --username AWS --password-stdin "$registry"

compose=(docker compose -f docker-compose.prod.yml --env-file "$ENV_FILE")

echo "--- pull"
"${compose[@]}" pull --quiet db backend frontend

echo "--- migrate"
# The database must be up for migrations; this starts only db if needed.
"${compose[@]}" run --rm migrate

echo "--- start"
# Recreates only the containers whose image or settings changed, then waits
# for their health checks.
"${compose[@]}" up -d --remove-orphans --wait --wait-timeout 180

echo "--- scheduled jobs"
install -m 0644 ops/systemd/*.service ops/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now places-backup.timer places-cleanup.timer
systemctl list-timers 'places-*' --no-pager

echo "--- tidy"
docker image prune -af --filter "until=168h" > /dev/null
"${compose[@]}" ps --format 'table {{.Service}}\t{{.Status}}'
echo "deployed $BACKEND_IMAGE and $FRONTEND_IMAGE"
