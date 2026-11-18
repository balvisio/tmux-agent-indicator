#!/usr/bin/env bash
# Acknowledge completed work only if the original pane/window is still focused.
set -euo pipefail

command -v tmux >/dev/null 2>&1 || exit 0
script_dir="${BASH_SOURCE[0]%/*}"
[ "$script_dir" != "${BASH_SOURCE[0]}" ] || script_dir=.
script_dir="$(cd "$script_dir" && pwd)"
locked=false
if [ "${1:-}" = --agent-state-lock-held ]; then
    locked=true
    shift
fi
pane_id="${1:-$(tmux display-message -p '#{pane_id}')}"
window_id="${2:-$(tmux display-message -p -t "$pane_id" '#{window_id}')}"
request="${3:-}"
cleanup_focus_request() {
    local status="$1" command
    [ "$BASHPID" = "$$" ] || return 0
    if [[ "$request" =~ ^[0-9]+$ ]]; then
        command="set-option -wqu -t $window_id @agent-indicator-focus-request"
        # Only successful/stale navigation may recheck for a newer focus event.
        # An error must not turn a persistent failure into a tight retry loop.
        if [ "$status" -eq 0 ]; then
            command+=" ; run-shell -C -t $window_id '#{@agent-indicator-focus-launch}'"
        fi
        tmux if-shell -F -t "$window_id" "#{==:#{@agent-indicator-focus-request},$request}" \
            "$command" 2>/dev/null || true
    fi
}
trap 'cleanup_focus_request "$?"' EXIT
# Avoid joining a busy lock for navigation that has already been superseded.
if [ "$locked" = false ]; then
    focus=$(tmux display-message -p -t "$pane_id" '#{pane_active}:#{window_active}:#{window_id}' 2>/dev/null)
    [ "$focus" = "1:1:$window_id" ] || exit 0
fi
source "$script_dir/state-lib.sh"
trap 'cleanup_focus_request "$?"; cleanup_state_markers' EXIT
if [ "$locked" = true ]; then
    lock_agent_state --agent-state-lock-held
else
    lock_agent_state "$pane_id" "$window_id" "$request"
fi
# Focus may change while this request waits behind earlier agent events.
focus=$(tmux display-message -p -t "$pane_id" '#{pane_active}:#{window_active}:#{window_id}' 2>/dev/null)
[ "$focus" = "1:1:$window_id" ] || exit 0
while IFS= read -r completed_pane; do
    if [ "$(tmux_get_env "TMUX_AGENT_PANE_${completed_pane}_STATE")" = done ]; then
        clear_pane_state "$completed_pane"
        reset_pane_style "$completed_pane"
    fi
done < <(tmux list-panes -t "$window_id" -F '#{pane_id}')
reconcile_notifications ack "$window_id"
render_window_state "$window_id"
sync_animation
tmux refresh-client -S >/dev/null 2>&1 || true
