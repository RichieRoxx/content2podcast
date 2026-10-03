#!/usr/bin/env bash
# Validate both Caddyfiles and smoke-test the LAN one against a real Caddy container.
#
# Needs Docker on Linux (the container uses the host network). Usage: deploy/verify-caddy.sh
set -euo pipefail

dir=$(cd "$(dirname "$0")" && pwd)
image=${CADDY_IMAGE:-caddy:2}
port=${PODCAST_TEST_PORT:-18089}
name=podcast-caddy-verify
site=$(mktemp -d)
failures=0

cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
    rm -rf "$site"
}
trap cleanup EXIT

echo "== caddy validate"
for file in Caddyfile.example Caddyfile.tailscale.example; do
    docker run --rm -v "$dir:/deploy:ro" "$image" caddy validate \
        --config "/deploy/$file" --adapter caddyfile >/dev/null 2>&1 \
        || { echo "FAIL: $file is not valid" >&2; docker run --rm -v "$dir:/deploy:ro" "$image" \
             caddy validate --config "/deploy/$file" --adapter caddyfile >&2 || true; exit 1; }
    echo "ok: $file"
done

echo "== smoke test"
mkdir -p "$site/episodes"
python3 -c "print('<?xml version=\"1.0\"?><rss>' + '<item>x</item>' * 500 + '</rss>')" > "$site/feed.xml"
head -c 100000 /dev/urandom > "$site/episodes/e.mp3"
printf 'png' > "$site/cover.png"
printf 'secret' > "$site/.hidden"

docker run -d --rm --name "$name" --network host \
    -v "$dir:/deploy:ro" -v "$site:/srv/podcast:ro" -e "PODCAST_PORT=$port" "$image" \
    caddy run --config /deploy/Caddyfile.example --adapter caddyfile >/dev/null

base="http://127.0.0.1:$port"
for _ in $(seq 1 30); do
    curl -s -o /dev/null --noproxy '*' "$base/feed.xml" && break
    sleep 0.5
done

headers() { curl -s -D- -o /dev/null --noproxy '*' "$@" | tr -d '\r'; }

expect() { # expect <description> <pattern> <headers>
    if printf '%s\n' "$3" | grep -q -i -E "$2"; then
        echo "ok: $1"
    else
        echo "FAIL: $1 (wanted /$2/)" >&2
        printf '%s\n' "$3" | sed 's/^/    /' >&2
        failures=$((failures + 1))
    fi
}

feed=$(headers "$base/feed.xml")
expect "feed is served" '^HTTP/[0-9.]+ 200' "$feed"
expect "feed content type" '^content-type: application/rss\+xml; charset=utf-8' "$feed"
expect "feed must be revalidated" '^cache-control: no-cache' "$feed"
etag=$(printf '%s\n' "$feed" | grep -i '^etag:' | cut -d' ' -f2)
expect "feed answers If-None-Match with 304" '^HTTP/[0-9.]+ 304' "$(headers -H "If-None-Match: $etag" "$base/feed.xml")"
expect "feed is compressed" '^content-encoding: gzip' "$(headers -H 'Accept-Encoding: gzip' "$base/feed.xml")"

audio=$(headers "$base/episodes/e.mp3")
expect "mp3 content type" '^content-type: audio/mpeg' "$audio"
expect "mp3 is cached for a long time" '^cache-control: public, max-age=31536000, immutable' "$audio"
expect "mp3 advertises ranges" '^accept-ranges: bytes' "$audio"
ranged=$(headers -H 'Range: bytes=10-19' "$base/episodes/e.mp3")
expect "range requests give 206" '^HTTP/[0-9.]+ 206' "$ranged"
expect "range response has Content-Range" '^content-range: bytes 10-19/100000' "$ranged"
if printf '%s\n' "$(headers -H 'Accept-Encoding: gzip' "$base/episodes/e.mp3")" | grep -q -i '^content-encoding'; then
    echo "FAIL: mp3 must not be compressed" >&2
    failures=$((failures + 1))
fi

expect "cover is cached for an hour" '^cache-control: public, max-age=3600' "$(headers "$base/cover.png")"
expect "dotfiles are hidden" '^HTTP/[0-9.]+ 404' "$(headers "$base/.hidden")"
expect "no directory listing" '^HTTP/[0-9.]+ 404' "$(headers "$base/episodes/")"
expect "unknown files give 404" '^HTTP/[0-9.]+ 404' "$(headers "$base/missing.mp3")"

if [ "$failures" -ne 0 ]; then
    echo "$failures check(s) failed" >&2
    exit 1
fi
echo "caddy configuration OK"
