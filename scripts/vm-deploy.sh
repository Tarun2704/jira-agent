#!/bin/bash
# Deploy GitHub's main branch to the server and restart the app there (Redis data and the ngrok tunnel stay).
#
#     ./scripts/vm-deploy.sh                       # deploys origin/main; server from ~/.oci/jira-agent-micro.ip
#     BRANCH=feature/x ./scripts/vm-deploy.sh      # try a pushed branch on the server (deploy main again after)
#     VM=ubuntu@1.2.3.4 ./scripts/vm-deploy.sh
#
# The server has a git clone in ~/jira-agent (plus its own .env, which git ignores and this never touches): it fetches,
# checks out exactly what is on GitHub, builds the image (a few minutes on the small VM when requirements.txt changed,
# seconds otherwise) and restarts only the app container. Running jobs are cut off and picked up again by the watchdog.
# Only pushed commits can be deployed, so production is always something that exists on GitHub.
set -euo pipefail
VM=${VM:-ubuntu@$(cat ~/.oci/jira-agent-micro.ip)}
KEY=${KEY:-$HOME/.ssh/oracle_jira_agent}
BRANCH=${BRANCH:-main}

ssh -i "$KEY" "$VM" BRANCH="$BRANCH" 'bash -s' <<'REMOTE'
set -euo pipefail
cd ~/jira-agent
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
curl -s localhost:8000/health; echo
REMOTE
