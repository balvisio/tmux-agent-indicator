#!/usr/bin/env bash
# Claude has no turn-interruption hook. Called after tmux forwards Ctrl+C.

set -euo pipefail

pane_id="${1:-}"
[ -n "$pane_id" ] || exit 0
agent=$(tmux show-environment -g "TMUX_AGENT_PANE_${pane_id}_AGENT" 2>/dev/null | sed 's/^[^=]*=//' || true)
# Codex handles cancellation through its native Interrupt hook.
[ "$agent" = claude ] || exit 0
state=$(tmux show-environment -g "TMUX_AGENT_PANE_${pane_id}_STATE" 2>/dev/null | sed 's/^[^=]*=//' || true)
case "$state" in
    running|needs-input)
        script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        TMUX_PANE="$pane_id" "$script_dir/agent-state.sh" --agent claude --state off --if-state "$state"
        ;;
esac
