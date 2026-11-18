#!/usr/bin/env bash
# Reconcile window colors after pane layout changes or a presence sweep.
set -euo pipefail

script_dir="${BASH_SOURCE[0]%/*}"
[ "$script_dir" != "${BASH_SOURCE[0]}" ] || script_dir=.
script_dir="$(cd "$script_dir" && pwd)"
# Full reconciliations batch their reads and writes in one short critical section.
[ "$#" -ne 0 ] || set -- --all
source "$script_dir/state-lib.sh"
case "${1:-}:${2:-}" in
    --all:*|--sweep:*|--agent-state-lock-held:--all|--agent-state-lock-held:--sweep) agent_batch=true ;;
esac
lock_agent_state "$@"
shift

if [ "${1:-}" = --layout ]; then
    window_id="$2"
    layout_request="${3:-}"
    cleanup_layout_request() {
        local status="$1" command
        [ "$BASHPID" = "$$" ] || return 0
        if [[ "$layout_request" =~ ^[0-9]+$ ]]; then
            command="set-option -wqu -t $window_id @agent-indicator-layout-request"
            if [ "$status" -eq 0 ]; then
                command+=" ; run-shell -C -t $window_id '#{@agent-indicator-layout-launch}'"
            fi
            tmux if-shell -F -t "$window_id" "#{==:#{@agent-indicator-layout-request},$layout_request}" \
                "$command" 2>/dev/null || true
        fi
        cleanup_state_markers
    }
    trap 'cleanup_layout_request "$?"' EXIT
    set -- "$window_id"
fi

if [ "$agent_batch" = true ]; then
    [ "$1" != --sweep ] || apply_presence_snapshot
    for window_id in "${agent_window_ids[@]}"; do
        render_window_state "$window_id"
    done
    flush_sweep
    # Notification reconciliation can publish new overlays or pane states. Read
    # those live instead of reusing the pre-reconciliation window snapshot.
    agent_batch=false
    sync_animation
    reconcile_notifications sweep
    exit 0
fi

if [ "${1:-}" = --input-result ]; then
    shift
    reconcile_notifications input "$@"
    exit 0
fi
if [ "${1:-}" = --notifications ]; then
    sync_animation
    reconcile_notifications sweep
    exit 0
fi
style_only=false
if [ "${1:-}" = --style-only ]; then
    style_only=true
    shift
fi
if [ "${1:-}" = --idle-style ]; then
    # The presence integration owns idle styling; active styling always comes from pane states.
    window_id="$2"
    value="$3"
    set_idle_style "$window_id" "$value"
    render_window_state "$window_id"
else
    render_window_state "$1"
fi
if [ "$style_only" = false ]; then
    sync_animation
    reconcile_notifications sweep
fi
