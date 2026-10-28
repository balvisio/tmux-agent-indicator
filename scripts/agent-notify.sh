#!/usr/bin/env bash
# Receive Kibitz handoff decisions using the same lock as pane state hooks.
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/state-lib.sh"
if [ "${1:-}" != --agent-state-lock-held ]; then
    TMUX_AGENT_NOTIFICATION_MESSAGE=$(cat)
    export TMUX_AGENT_NOTIFICATION_MESSAGE
fi
lock_agent_state "$@"
reconcile_notifications handoff
