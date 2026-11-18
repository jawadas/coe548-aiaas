#!/usr/bin/env bash
# Deploy the current checkout on the VM. Run from the repo root (~/aiaas):
#
#   git pull && scripts/deploy.sh
#
# Safe to run again and again. It adds the portal's settings to .env if they
# are missing, backs up the databases and .env, rebuilds and restarts what
# changed, and checks the result. It never runs "down" and never touches volumes.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "No .env here. Run this from the repo root on the VM." >&2; exit 1; }
for name in POSTGRES_PASSWORD LITELLM_MASTER_KEY; do
  grep -q "^${name}=." .env || { echo ".env has no ${name}." >&2; exit 1; }
done

# 1. Settings the portal needs, generated once. Never printed, except the invite code.
add_setting() {
  grep -q "^$1=" .env || { printf '%s=%s\n' "$1" "$2" >> .env; echo "Added $1 to .env"; }
}
add_setting PORTAL_DB_PASSWORD "$(openssl rand -hex 24)"
add_setting PORTAL_SECRET_KEY "$(openssl rand -hex 32)"
add_setting PORTAL_INVITE_CODE "coe548-$(openssl rand -hex 4)"
chmod 600 .env
docker compose config -q

# 2. Back up before changing anything.
backup=~/aiaas-backups/$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$backup" && chmod 700 ~/aiaas-backups "$backup"
cp .env "$backup/env" && chmod 600 "$backup/env"
git rev-parse HEAD > "$backup/commit" 2>/dev/null || true
for database in litellm portal; do
  if docker compose exec -T postgres psql -U aiaas -d postgres -Atc \
       "SELECT 1 FROM pg_database WHERE datname='${database}'" | grep -q 1; then
    docker compose exec -T postgres pg_dump -U aiaas "$database" | gzip > "$backup/${database}.sql.gz"
  fi
done
echo "Backup: $backup"

# 3. Build and start. Caddy and LiteLLM read bind-mounted files only when they
#    start, so restart them to pick up a changed Caddyfile or config.yaml.
docker compose up -d --build
docker compose restart caddy

# 4. Wait for the portal and the gateway (it takes a while to start after a
#    restart), then let the portal check itself.
for _ in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q portal)" 2>/dev/null)" = healthy ] && break
  sleep 2
done
for _ in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:4000/health/readiness)" = 200 ] && break
  sleep 3
done
docker compose ps --format 'table {{.Name}}\t{{.Status}}'
docker compose exec -T portal python -m app.cli selfcheck </dev/null

echo
echo "Deployed. Invite code for sign-up: $(grep '^PORTAL_INVITE_CODE=' .env | cut -d= -f2-)"
previous=$(cat ~/aiaas-backups/last-deployed 2>/dev/null || true)
current=$(git rev-parse HEAD)
if [ -n "$previous" ] && [ "$previous" != "$current" ]; then
  echo "Roll back with: git checkout $previous && scripts/deploy.sh"
fi
echo "$current" > ~/aiaas-backups/last-deployed
