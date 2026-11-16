#!/usr/bin/env bash
# Reconcile window colors after pane layout changes or a presence sweep.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Full reconciliations batch their reads and writes in one short critical section.
[ "$#" -ne 0 ] || set -- --all
source "$script_dir/state-lib.sh"
case "${1:-}:${2:-}" in
    --all:*|--sweep:*|--agent-state-lock-held:--all|--agent-state-lock-held:--sweep) agent_batch=true ;;
esac
lock_agent_state "$@"
shift

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
