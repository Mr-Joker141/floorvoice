#!/usr/bin/env bash
# Run FloorVoice: ERPNext gateway (if not up), API server and a public HTTPS tunnel.
#   scripts/dev.sh            # server + cloudflared quick tunnel
#   NO_TUNNEL=1 scripts/dev.sh
#   scripts/dev.sh stop       # remove the tunnel
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8765}"
TUNNEL_NAME=floorvoice-tunnel

set -a; . ./.env; set +a

docker compose -f infra/erp-gateway.compose.yml --env-file .env up -d >/dev/null
until curl -sf "${ERP_URL}/api/method/ping" >/dev/null; do echo "waiting for ERPNext…"; sleep 2; done

if [[ "${1:-}" == "stop" ]]; then
  docker rm -f "$TUNNEL_NAME" >/dev/null 2>&1 || true
  echo "tunnel stopped"; exit 0
fi

if [[ -z "${NO_TUNNEL:-}" ]]; then
  # The quick-tunnel URL changes when cloudflared restarts, so keep it running across
  # server restarts. `scripts/dev.sh stop` removes it.
  if [[ -z "$(docker ps -q -f name="^${TUNNEL_NAME}$")" ]]; then
    docker rm -f "$TUNNEL_NAME" >/dev/null 2>&1 || true
    docker run -d --name "$TUNNEL_NAME" --restart unless-stopped --network host \
      cloudflare/cloudflared:2025.9.1 tunnel --no-autoupdate --url "http://127.0.0.1:${PORT}" >/dev/null
  fi
  for _ in $(seq 30); do
    URL=$(docker logs "$TUNNEL_NAME" 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)
    [[ -n "$URL" ]] && break
    sleep 1
  done
  echo
  echo "  Phone URL: ${URL:-<tunnel not ready, see: docker logs $TUNNEL_NAME>}/?k=${FLOORVOICE_KEY:-}"
  echo
fi

exec uv run uvicorn server.app:app --host 127.0.0.1 --port "$PORT" --log-level info
