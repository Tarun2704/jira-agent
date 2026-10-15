#!/bin/bash
# Start (or restart) the app on a server: Redis, the app and the ngrok tunnel, each restarting by itself (also after a
# reboot). Run from the repo folder on the server, after `docker build -t jira-agent .`:
#
#     NGROK_AUTHTOKEN=... NGROK_URL=https://<your-static-domain>.ngrok-free.dev ./scripts/vm-start.sh
#
# Needs .env next to the Dockerfile. On a 1 GB server (Oracle's free AMD micro VM) it runs one job at a time and the
# server should have swap; see docs/plan-first.md "Running on a server".
set -euo pipefail
cd "$(dirname "$0")/.."
: "${NGROK_AUTHTOKEN:?set NGROK_AUTHTOKEN}" "${NGROK_URL:?set NGROK_URL, e.g. https://name.ngrok-free.dev}"
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)

docker network inspect jira-agent-net >/dev/null 2>&1 || docker network create jira-agent-net >/dev/null

# Redis also listens on the server's own localhost (never the internet), for Redis Insight over an SSH tunnel.
docker inspect jira-agent-redis >/dev/null 2>&1 || docker run -d --name jira-agent-redis --network jira-agent-net \
  --restart unless-stopped -p 127.0.0.1:6379:6379 -v jira-agent-redis-data:/data redis:7-alpine redis-server --appendonly yes >/dev/null

extra=()
if [ "$MEM_MB" -lt 1800 ]; then  # small server: one job at a time (a plan and a coding run together don't fit)
  extra+=(-e SEPARATE_PLAN_QUEUE=false -e PLAN_WORKERS=1)
fi
docker rm -f jira-agent >/dev/null 2>&1 || true
docker run -d --name jira-agent --network jira-agent-net --restart unless-stopped -p 127.0.0.1:8000:8000 \
  --env-file .env -e REDIS_URL=redis://jira-agent-redis:6379 "${extra[@]}" jira-agent >/dev/null

docker rm -f jira-agent-ngrok >/dev/null 2>&1 || true
docker run -d --name jira-agent-ngrok --network jira-agent-net --restart unless-stopped \
  -e NGROK_AUTHTOKEN="$NGROK_AUTHTOKEN" ngrok/ngrok:latest http jira-agent:8000 --url "$NGROK_URL" >/dev/null

for _ in $(seq 1 60); do curl -fs localhost:8000/health >/dev/null 2>&1 && break; sleep 2; done
curl -s localhost:8000/health; echo
echo "Memory: ${MEM_MB} MB$([ "$MEM_MB" -lt 1800 ] && echo ' (one job at a time)')"
