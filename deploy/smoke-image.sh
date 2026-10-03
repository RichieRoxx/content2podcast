#!/usr/bin/env bash
# Smoke-test a built content2podcast image: non-root user, version, ffmpeg, a dry run with a
# mounted config, a healthy daemon that stops cleanly on SIGTERM, and an unhealthy one without
# configuration.
#
# Usage: deploy/smoke-image.sh [IMAGE]    (default: content2podcast:ci)
set -euo pipefail

image=${1:-content2podcast:ci}
work=$(mktemp -d)
name=podcast-smoke-$$
trap 'docker rm -f "$name" >/dev/null 2>&1 || true; docker volume rm -f "$name-data" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT

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
chmod -R a+rX "$work"

fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo "== $*"; }

step "runs as a non-root user"
uid=$(docker run --rm --entrypoint id "$image" -u)
[ "$uid" != "0" ] || fail "the container runs as root"
echo "uid $uid"

step "podcast --version and ffmpeg"
docker run --rm "$image" --version
docker run --rm --entrypoint ffmpeg "$image" -version | head -1

step "default environment"
env_out=$(docker run --rm --entrypoint env "$image")
for expected in C2P_CONFIG=/config/config.yaml C2P_PATHS__DATA_DIR=/data \
                C2P_PATHS__OUTPUT_DIR=/srv/podcast TZ=UTC; do
    printf '%s\n' "$env_out" | grep -qx "$expected" || fail "missing $expected"
done

step "run --dry-run with a mounted read-only config"
docker volume create "$name-data" >/dev/null
docker run --rm -v "$work/config:/config:ro" -v "$name-data:/data" "$image" run --dry-run

step "doctor"
docker run --rm -v "$work/config:/config:ro" -v "$name-data:/data" "$image" doctor

step "the daemon becomes healthy and stops cleanly on SIGTERM"
docker run -d --name "$name" --health-interval 3s --health-start-period 1s --health-retries 2 \
    -v "$work/config:/config:ro" -v "$name-data:/data" "$image" >/dev/null
status=starting
for _ in $(seq 1 40); do
    status=$(docker inspect --format '{{.State.Health.Status}}' "$name")
    [ "$status" = healthy ] && break
    sleep 2
done
[ "$status" = healthy ] || { docker logs "$name" >&2; fail "the container did not become healthy ($status)"; }
echo "healthy"
docker stop --time 20 "$name" >/dev/null
code=$(docker inspect --format '{{.State.ExitCode}}' "$name")
[ "$code" = 0 ] || { docker logs "$name" >&2; fail "the daemon exited with $code after SIGTERM"; }
docker logs "$name" 2>&1 | grep -q "Daemon stopped" || fail "no clean shutdown message"
echo "stopped cleanly"

step "health fails without a configuration"
if docker run --rm "$image" health; then
    fail "health succeeded without any configuration"
fi
echo "unhealthy, as expected"

echo "image OK"
