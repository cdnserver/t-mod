#!/usr/bin/env sh
set -eu

ROOT_DIR="${CDN_ROOT_DIR:-/srv/tmod-cdn}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_FILE="${CDN_ENV_FILE:-$SCRIPT_DIR/.env}"

command -v docker >/dev/null 2>&1 || {
  echo "Docker is required before deploying the CDN node." >&2
  exit 1
}
docker compose version >/dev/null 2>&1 || {
  echo "Docker Compose v2 is required before deploying the CDN node." >&2
  exit 1
}

if [ ! -f "$ENV_FILE" ]; then
  cp "$SCRIPT_DIR/.env.example" "$ENV_FILE"
  echo "Created $ENV_FILE. Set CDN_DOMAIN, then run this command again." >&2
  exit 2
fi

mkdir -p "$ROOT_DIR/content/releases" "$ROOT_DIR/content/empty" "$ROOT_DIR/logs"
if [ ! -e "$ROOT_DIR/content/current" ]; then
  ln -s "$ROOT_DIR/content/empty" "$ROOT_DIR/content/current"
fi

docker compose --env-file "$ENV_FILE" -f "$SCRIPT_DIR/docker-compose.yml" config >/dev/null
docker compose --env-file "$ENV_FILE" -f "$SCRIPT_DIR/docker-compose.yml" up -d

for attempt in $(seq 1 40); do
  status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' tmod-cdn 2>/dev/null || true)
  [ "$status" = "healthy" ] && {
    echo "T-Mod CDN is healthy."
    exit 0
  }
  sleep 2
done

docker compose --env-file "$ENV_FILE" -f "$SCRIPT_DIR/docker-compose.yml" logs --tail 120 tmod-cdn >&2
echo "T-Mod CDN did not become healthy in time." >&2
exit 1

