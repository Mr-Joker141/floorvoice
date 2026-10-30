#!/usr/bin/env bash
# Expose the demo ERPNext desk over a public HTTPS quick tunnel, so you can watch the
# documents FloorVoice posts (log in as Administrator, password ERP_ADMIN_PASSWORD).
#   scripts/erp_tunnel.sh        # start (or show) the tunnel
#   scripts/erp_tunnel.sh stop
set -euo pipefail
cd "$(dirname "$0")/.."
NAME=floorvoice-erp-tunnel
URL_FILE=data/erp_public_url
set -a; . ./.env; set +a

if [[ "${1:-}" == "stop" ]]; then
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  rm -f "$URL_FILE"
  echo "ERPNext tunnel stopped"; exit 0
fi

if [[ -z "$(docker ps -q -f name="^${NAME}$")" ]]; then
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" --restart unless-stopped --network host \
    cloudflare/cloudflared:2025.9.1 tunnel --no-autoupdate --url "http://127.0.0.1:${ERP_PORT:-8090}" >/dev/null
fi
for _ in $(seq 30); do
  URL=$(docker logs "$NAME" 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)
  [[ -n "$URL" ]] && break
  sleep 1
done
[[ -z "${URL:-}" ]] && { echo "tunnel not ready: docker logs $NAME" >&2; exit 1; }
mkdir -p data && echo "$URL" > "$URL_FILE"
echo "ERPNext desk: $URL/app   (user: Administrator)"
