#!/usr/bin/env bash
# Acknowledge completed work in the focused window without touching active work.
set -euo pipefail

command -v tmux >/dev/null 2>&1 || exit 0
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/state-lib.sh"
lock_agent_state "$@"
shift
pane_id="${1:-$(tmux display-message -p '#{pane_id}')}"
window_id="${2:-$(tmux display-message -p -t "$pane_id" '#{window_id}')}"

# Recheck focus after acquiring the lock: queued events can outlive user navigation.
[ "$(tmux display-message -p -t "$pane_id" '#{pane_active}:#{window_active}:#{window_id}' 2>/dev/null || true)" = "1:1:$window_id" ] || exit 0
while IFS= read -r completed_pane; do
    if [ "$(tmux_get_env "TMUX_AGENT_PANE_${completed_pane}_STATE")" = done ]; then
        clear_pane_state "$completed_pane"
        reset_pane_style "$completed_pane"
    fi
done < <(tmux list-panes -t "$window_id" -F '#{pane_id}')
render_window_state "$window_id"
sync_animation
tmux refresh-client -S >/dev/null 2>&1 || true
