#!/usr/bin/env bash
# Verify the systemd units with `systemd-analyze verify` (needs no root and no installation).
#
# Usage: deploy/verify-units.sh [UNIT_DIR]    (default: the directory of this script)
#
# `systemd-analyze verify` only prints typos and unknown settings as warnings and still exits
# with 0, so any output other than the expected noise of the fake root counts as a failure.
set -euo pipefail

dir=${1:-$(dirname "$0")}
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT

mkdir -p "$root/etc/systemd/system"
cp "$dir/podcast.service" "$dir/podcast.timer" "$root/etc/systemd/system/"

# A stub for the program named in ExecStart= (verify checks that it exists and is executable).
binary=$(sed -n 's/^ExecStart=\([^ ]*\).*/\1/p' "$dir/podcast.service")
if [ -z "$binary" ]; then
    echo "no ExecStart= found in $dir/podcast.service" >&2
    exit 1
fi
mkdir -p "$root$(dirname "$binary")"
printf '#!/bin/sh\nexit 0\n' > "$root$binary"
chmod +x "$root$binary"

output=$(systemd-analyze verify --root="$root" \
    /etc/systemd/system/podcast.service /etc/systemd/system/podcast.timer 2>&1 || true)

# The fake root has no system targets (sysinit.target, ...): that message is expected.
problems=$(printf '%s\n' "$output" | grep -v -E 'Failed to create .*/start: Unit .*\.target not found' || true)
if [ -n "$problems" ]; then
    printf '%s\n' "$problems" >&2
    echo "systemd unit verification FAILED" >&2
    exit 1
fi
echo "systemd units OK"
