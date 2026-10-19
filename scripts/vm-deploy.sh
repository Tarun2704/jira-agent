#!/bin/bash
# Deploy GitHub's main branch to the server by hand (merges to main also deploy by themselves, see
# .github/workflows/deploy.yml). The server side is scripts/server-deploy.sh, sent from here, so this also works
# before the server's checkout has it.
#
#     ./scripts/vm-deploy.sh                       # deploys origin/main; server from ~/.oci/jira-agent-micro.ip
#     BRANCH=feature/x ./scripts/vm-deploy.sh      # try a pushed branch on the server (deploy main again after)
#     WAIT_IDLE_SECONDS=0 ./scripts/vm-deploy.sh   # don't wait for running jobs
#     VM=ubuntu@1.2.3.4 ./scripts/vm-deploy.sh
#
# Only pushed commits can be deployed, so production is always something that exists on GitHub.
set -euo pipefail
cd "$(dirname "$0")/.."
VM=${VM:-ubuntu@$(cat ~/.oci/jira-agent-micro.ip)}
KEY=${KEY:-$HOME/.ssh/oracle_jira_agent}
ssh -i "$KEY" "$VM" BRANCH="${BRANCH:-main}" WAIT_IDLE_SECONDS="${WAIT_IDLE_SECONDS:-1800}" 'bash -s' < scripts/server-deploy.sh
