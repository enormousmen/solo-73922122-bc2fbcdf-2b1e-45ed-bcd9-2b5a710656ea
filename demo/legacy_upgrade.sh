#!/usr/bin/env bash
# Demonstrate upgrading a mirror whose database predates receipt content
# digests, then re-sending packages with the same sequence number/path.
#
#   ./demo/legacy_upgrade.sh
#
# Uses Docker Compose with its own compose file and volume so it never touches
# the main demo's data.  It builds one image that can both seed the legacy
# database (standard library only) and run the current service.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$HERE/legacy_upgrade.compose.yml"
BASE_URL="${BASE_URL:-http://127.0.0.1:18000}"
WORK="$HERE/work-legacy"
rm -rf "$WORK"
mkdir -p "$WORK"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
hr() { printf '%s\n' '------------------------------------------------------------'; }
compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

cleanup() { compose down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

printf 'live-package-v1' > "$WORK/live-ok.tgz"
printf 'TAMPERED-CONTENT' > "$WORK/live-wrong.tgz"
printf 'lost-package-v1' > "$WORK/lost-guess.tgz"
printf 'something-else'  > "$WORK/gone-guess.tgz"

step "0. Start from an empty volume and seed a pre-digest database"
cleanup
compose build

# Create the legacy database inside a fresh volume before the service ever
# opens it, then start the service so init_db migrates the old schema.
compose run --rm --no-deps --entrypoint python mirror \
  /seed_legacy_db.py /data/mirror.db
compose up -d
for _ in $(seq 1 30); do
  if curl -sS -o /dev/null "$BASE_URL/healthz" 2>/dev/null; then break; fi
  sleep 0.5
done

step "1. The upgrade preserved the live catalog and withdrawals"
curl -sS "$BASE_URL/artifacts" | jq .
hr
printf 'GET /artifacts/releases/gone.tgz -> %s (withdrawn before upgrade)\n' \
  "$(curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/artifacts/releases/gone.tgz")"
curl -sS "$BASE_URL/events" | jq '.events[] | {seq, path, content_sha256}'

step "2. Live package: identical resend verifies as a match"
curl -sS -X POST "$BASE_URL/events" \
  -F 'manifest=[{"seq":1,"path":"releases/live.tgz","operation":"publish","file":"f0"}]' \
  -F 'f0=@'"$WORK"'/live-ok.tgz;filename=live.tgz' \
  | jq '.results[0] | {status, conflict, verification, content_verified}'

step "3. Live package: different bytes are reported as a conflict"
curl -sS -X POST "$BASE_URL/events" \
  -F 'manifest=[{"seq":1,"path":"releases/live.tgz","operation":"publish","file":"f0"}]' \
  -F 'f0=@'"$WORK"'/live-wrong.tgz;filename=live.tgz' \
  | jq '.results[0] | {status, conflict, verification, original_sha256, received_sha256}'
hr
printf 'GET /artifacts/releases/live.tgz -> %s (original bytes kept)\n' \
  "$(curl -sS "$BASE_URL/artifacts/releases/live.tgz")"

step "4. Lost original (superseded before upgrade): reported as unverifiable"
curl -sS -X POST "$BASE_URL/events" \
  -F 'manifest=[{"seq":2,"path":"releases/lost.tgz","operation":"publish","file":"f0"}]' \
  -F 'f0=@'"$WORK"'/lost-guess.tgz;filename=lost.tgz' \
  | jq '.results[0] | {status, conflict, verification, content_verified, original_sha256}'

step "5. Lost original (withdrawn before upgrade): unverifiable, stays withdrawn"
curl -sS -X POST "$BASE_URL/events" \
  -F 'manifest=[{"seq":4,"path":"releases/gone.tgz","operation":"publish","file":"f0"}]' \
  -F 'f0=@'"$WORK"'/gone-guess.tgz;filename=gone.tgz' \
  | jq '.results[0] | {status, conflict, verification, content_verified}'
hr
printf 'GET /artifacts/releases/gone.tgz -> %s (still withdrawn)\n' \
  "$(curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/artifacts/releases/gone.tgz")"

step "6. The unverifiable resend was not adopted as the new original"
# A second, different body for seq 2 is still unverifiable -- it does not
# match the first resend (which would happen if that resend had become the
# baseline).
printf 'a-second-guess' > "$WORK/lost-guess2.tgz"
curl -sS -X POST "$BASE_URL/events" \
  -F 'manifest=[{"seq":2,"path":"releases/lost.tgz","operation":"publish","file":"f0"}]' \
  -F 'f0=@'"$WORK"'/lost-guess2.tgz;filename=lost.tgz' \
  | jq '.results[0] | {status, conflict, verification, original_sha256}'

step "7. Restart and verify the same basis is used again"
compose restart mirror >/dev/null
for _ in $(seq 1 30); do
  if curl -sS -o /dev/null "$BASE_URL/healthz" 2>/dev/null; then break; fi
  sleep 0.5
done
curl -sS -X POST "$BASE_URL/events" \
  -F 'manifest=[{"seq":1,"path":"releases/live.tgz","operation":"publish","file":"f0"}]' \
  -F 'f0=@'"$WORK"'/live-wrong.tgz;filename=live.tgz' \
  | jq '.results[0] | {status, conflict, verification}'

step "8. GC reclaims the digest with the receipt"
curl -sS -X POST "$BASE_URL/gc" -H 'Content-Type: application/json' \
  -d '{"limit":10}' | jq .
hr
curl -sS "$BASE_URL/events" | jq .
# seq 1 is now cleaned: its original basis is gone, so a resend can only be
# acknowledged, never applied or verified.
curl -sS -X POST "$BASE_URL/events" \
  -F 'manifest=[{"seq":1,"path":"releases/live.tgz","operation":"publish","file":"f0"}]' \
  -F 'f0=@'"$WORK"'/live-ok.tgz;filename=live.tgz' \
  | jq '.results[0] | {status, conflict, verification, acknowledged}'
hr
printf 'GET /artifacts/releases/live.tgz -> %s (still served, untouched)\n' \
  "$(curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/artifacts/releases/live.tgz")"
