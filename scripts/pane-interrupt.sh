#!/usr/bin/env bash
# Claude has no turn-interruption hook. Called after tmux forwards Ctrl+C.

set -euo pipefail

pane_id="${1:-}"
token="${2:-}"
[[ "$pane_id" =~ ^%[0-9]+$ && "$token" =~ ^[0-9a-f]{32}$ ]] || exit 0
# Capture the work identity at the keypress, then validate it under the writer
# lock. Cleanup may wait, but must never clear a turn started after Ctrl+C.
script_dir="${BASH_SOURCE[0]%/*}"
[ "$script_dir" != "${BASH_SOURCE[0]}" ] || script_dir=.
script_dir="$(cd "$script_dir" && pwd)"
TMUX_AGENT_HOOK_DATA="{\"interrupt_token\":\"$token\",\"hook_event_name\":\"Interrupt\"}" \
    TMUX_PANE="$pane_id" "$script_dir/agent-state.sh" --agent claude --state off || true
