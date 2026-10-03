#!/usr/bin/env bash
# Smoke-test deploy/docker-compose.example.yml: the stack comes up, the podcast service becomes
# healthy, the feed is served by Caddy, a dry run works, and `down -v` cleans everything up.
#
# Usage: deploy/smoke-compose.sh [IMAGE]    (default: content2podcast:ci; must already be built)
set -euo pipefail

image=${1:-content2podcast:ci}
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
export COMPOSE_PROJECT_NAME=podcast-smoke-$$
export PODCAST_IMAGE=$image
export PODCAST_PORT=${PODCAST_PORT:-18080}
compose() { docker compose -f "$work/docker-compose.yml" "$@"; }
trap 'compose down -v --remove-orphans >/dev/null 2>&1 || true; rm -rf "$work"' EXIT

fail() { echo "FAIL: $*" >&2; compose logs --tail 40 >&2 || true; exit 1; }
step() { echo "== $*"; }

cp "$here/docker-compose.example.yml" "$work/docker-compose.yml"
cp "$here/Caddyfile.example" "$work/Caddyfile.example"
mkdir -p "$work/config"
cat > "$work/config/config.yaml" <<'YAML'
podcast:
  title: Smoke Test
llm:
  provider: fake
tts:
  provider: fake
YAML
printf 'sources: []\n' > "$work/config/sources.yaml"
: > "$work/.env"
chmod -R a+rX "$work"

step "docker compose config"
compose config --quiet

step "up"
compose up -d --no-build

step "the podcast service becomes healthy"
cid=$(compose ps -q podcast)
status=starting
for _ in $(seq 1 60); do
    status=$(docker inspect --format '{{.State.Health.Status}}' "$cid")
    [ "$status" = healthy ] && break
    sleep 2
done
[ "$status" = healthy ] || fail "podcast is $status"

step "one-off commands share the volumes"
compose exec -T podcast podcast feed rebuild
compose exec -T podcast podcast run --dry-run

step "Caddy serves the feed"
code=000
for _ in $(seq 1 20); do
    code=$(curl -s -o "$work/feed.xml" -w '%{http_code}' "http://127.0.0.1:$PODCAST_PORT/feed.xml" || true)
    [ "$code" = 200 ] && break
    sleep 1
done
[ "$code" = 200 ] || fail "feed.xml returned $code"
grep -q '<rss' "$work/feed.xml" || fail "feed.xml is not an RSS feed"

step "down -v removes containers and volumes"
compose down -v --remove-orphans
[ -z "$(docker volume ls -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME")" ] \
    || fail "volumes were left behind"
echo "compose smoke test passed"
