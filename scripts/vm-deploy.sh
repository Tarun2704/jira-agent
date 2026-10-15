#!/bin/bash
# Deploy the code in this folder to the server and restart the app there (Redis data and the ngrok tunnel stay).
#
#     ./scripts/vm-deploy.sh                 # server address from ~/.oci/jira-agent-micro.ip
#     VM=ubuntu@1.2.3.4 ./scripts/vm-deploy.sh
#
# Copies the code (never .env: the server keeps its own; copy it by hand when settings change), builds the image on
# the server (a few minutes on the small VM; the Python step is cached unless requirements.txt changed) and restarts
# only the app container. Running jobs are cut off and picked up again by the watchdog after the restart.
set -euo pipefail
cd "$(dirname "$0")/.."
VM=${VM:-ubuntu@$(cat ~/.oci/jira-agent-micro.ip)}
KEY=${KEY:-$HOME/.ssh/oracle_jira_agent}
ssh_vm() { ssh -i "$KEY" "$VM" "$@"; }

echo "Copying code to $VM..."
rsync -az --delete -e "ssh -i $KEY" --exclude .venv --exclude .env --exclude .git --exclude __pycache__ \
  --exclude .pytest_cache --exclude .DS_Store ./ "$VM":~/jira-agent/
echo "Building on the server..."
ssh_vm 'cd ~/jira-agent && sudo docker build -q -t jira-agent . >/dev/null && echo built'
echo "Restarting the app..."
ssh_vm 'cd ~/jira-agent && extra=""; [ $(awk "/MemTotal/ {print int(\$2/1024)}" /proc/meminfo) -lt 1800 ] && extra="-e SEPARATE_PLAN_QUEUE=false -e PLAN_WORKERS=1";
  sudo docker rm -f jira-agent >/dev/null 2>&1 || true
  sudo docker run -d --name jira-agent --network jira-agent-net --restart unless-stopped -p 127.0.0.1:8000:8000 \
    --env-file .env -e REDIS_URL=redis://jira-agent-redis:6379 $extra jira-agent >/dev/null
  for i in $(seq 1 60); do curl -fs localhost:8000/health >/dev/null 2>&1 && break; sleep 2; done; curl -s localhost:8000/health; echo'
