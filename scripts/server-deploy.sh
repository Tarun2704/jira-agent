#!/bin/bash
# Runs ON the server: deploy GitHub's $BRANCH (default main) and restart only the app (Redis data and ngrok stay).
#
# Used by scripts/vm-deploy.sh (from a laptop) and by the GitHub Action .github/workflows/deploy.yml (after the
# tests pass on main). The Action's SSH key is pinned to this script in ~/.ssh/authorized_keys (command="..."), so
# that key can only ever deploy main, whatever it asks for.
#
# It waits (up to WAIT_IDLE_SECONDS, default 30 min) until no job is running, so a deploy doesn't cut a coding run off
# and the image build doesn't compete with OpenCode for the small server's memory; after that it goes ahead (the
# watchdog re-runs a job that was cut off).
set -euo pipefail
BRANCH=${BRANCH:-main}
WAIT_IDLE_SECONDS=${WAIT_IDLE_SECONDS:-1800}
cd ~/jira-agent

waited=0
while :; do
  busy=$(curl -fs localhost:8000/health 2>/dev/null | python3 -c 'import sys,json; print(",".join(json.load(sys.stdin).get("in_flight", [])))' 2>/dev/null || true)
  [ -z "$busy" ] && break
  if [ "$waited" -ge "$WAIT_IDLE_SECONDS" ]; then
    echo "Still busy with $busy after ${waited}s; deploying anyway (the watchdog re-runs it)"
    break
  fi
  [ "$waited" -eq 0 ] && echo "Waiting for running jobs to finish: $busy"
  sleep 30
  waited=$((waited + 30))
done

git fetch -q origin "$BRANCH"
git checkout -q -B "$BRANCH" "origin/$BRANCH"
git reset -q --hard "origin/$BRANCH"
echo "Deploying $(git log --oneline -1)"
sudo docker build -q -t jira-agent . >/dev/null
extra=""
[ "$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)" -lt 1800 ] && extra="-e SEPARATE_PLAN_QUEUE=false -e PLAN_WORKERS=1"
sudo docker rm -f jira-agent >/dev/null 2>&1 || true
sudo docker run -d --name jira-agent --network jira-agent-net --restart unless-stopped -p 127.0.0.1:8000:8000 \
  --env-file .env -e REDIS_URL=redis://jira-agent-redis:6379 $extra jira-agent >/dev/null
for _ in $(seq 1 60); do curl -fs localhost:8000/health >/dev/null 2>&1 && break; sleep 2; done
health=$(curl -fs localhost:8000/health) || { echo "The app did not come up: $(sudo docker logs --tail 20 jira-agent 2>&1)"; exit 1; }
echo "$health"
