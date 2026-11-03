#!/usr/bin/env bash
# Nightly demo reset for the judging window: put the ERPNext demo data back to its seeded
# state (scripts/demo_reset) and restart the FloorVoice server so it starts clean.
# Does nothing after RESET_UNTIL (default 2026-11-09, Sri Lanka date).
#
# crontab (04:00 Asia/Colombo = 22:30 UTC):
#   30 22 * * * /path/to/floorvoice/scripts/nightly_reset.sh >> /path/to/floorvoice/data/nightly_reset.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
UNTIL="${RESET_UNTIL:-2026-11-09}"
TODAY="$(TZ=Asia/Colombo date +%F)"
echo "== $(date -u +%FT%TZ) nightly reset (Colombo date $TODAY, until $UNTIL)"
if [[ "$TODAY" > "$UNTIL" ]]; then
  echo "past $UNTIL: nothing to do"
  exit 0
fi

uv run python -m scripts.demo_reset --yes

pkill -f "bin/uvicorn server[.]app" || true
sleep 2
mkdir -p data
setsid nohup scripts/dev.sh >> data/server.log 2>&1 < /dev/null &
for _ in $(seq 60); do
  if curl -sf http://127.0.0.1:8765/healthz >/dev/null; then
    echo "server up"
    exit 0
  fi
  sleep 1
done
echo "server did not come back within 60 s" >&2
exit 1
