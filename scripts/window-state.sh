#!/usr/bin/env bash
# Reconcile window colors after pane layout changes or a presence sweep.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Full sweeps acquire one short-lived lock per window, letting hooks run between windows.
if [ "$#" -eq 0 ]; then
    while IFS= read -r window_id; do
        "$0" "$window_id"
    done < <(tmux list-windows -a -F '#{window_id}' | sort -u)
    exit 0
fi
source "$script_dir/state-lib.sh"
lock_agent_state "$@"
shift

if [ "${1:-}" = --idle-style ]; then
    # The presence integration owns idle styling; active styling always comes from pane states.
    window_id="$2"
    value="$3"
    for entry in 'window-status-style ORIG_STATUS_STYLE' 'window-status-current-style ORIG_STATUS_CURRENT_STYLE'; do
        read -r option suffix <<< "$entry"
        key="TMUX_AGENT_WINDOW_${window_id}_${suffix}"
        if [ -n "$(tmux_get_env "$key")" ]; then
            tmux_set_env "$key" "$value"
        elif [ "$value" = __UNSET__ ]; then
            tmux set-window-option -qt "$window_id" -u "$option"
        else
            tmux set-window-option -qt "$window_id" "$option" "$value"
        fi
    done
    render_window_state "$window_id"
else
    render_window_state "$1"
fi
sync_animation
reconcile_notifications sweep
