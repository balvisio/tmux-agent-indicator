#!/usr/bin/env bash
# Set pane/window state for an AI agent.

set -euo pipefail

# Publish arrival before interpreter startup or tmux queries. Other hooks cannot
# mistake a question still entering the writer for an idle pane.
if [ "${1:-}" != --agent-state-lock-held ] && [ "${1:-}" != --agent-event-prepared ]; then
    unset TMUX_AGENT_ARRIVAL TMUX_AGENT_WRITER_MARKER
    export TMUX_AGENT_RECEIVED_AT="${EPOCHREALTIME:-}"
    arrival_dir="${TMUX_AGENT_RUNTIME_ROOT:-/tmp/tmux-agent-indicator-$UID}/arrivals"
    if [ -d "$arrival_dir" ] && [ ! -L "$arrival_dir" ] && [ -O "$arrival_dir" ]; then
        export TMUX_AGENT_ARRIVAL="$arrival_dir/$$-$RANDOM"
        printf '%s' "$$" > "$TMUX_AGENT_ARRIVAL"
    fi
    entry_dir="${BASH_SOURCE[0]%/*}"
    [ "$entry_dir" != "${BASH_SOURCE[0]}" ] || entry_dir=.
    exec python3 "$entry_dir/hook-entry.py" "$0" "$@"
fi
if [ "${1:-}" = --agent-event-prepared ]; then
    shift
fi

usage() {
    cat <<'EOF' >&2
Usage: agent-state.sh --agent <name> --state <running|needs-input|done|off> [--if-state <state>]
  --if-state  only act when the pane is currently in that state (off = no state recorded)
EOF
}

if ! command -v tmux >/dev/null 2>&1 || [ -z "${TMUX:-}" ]; then
    exit 0
fi

script_dir="${BASH_SOURCE[0]%/*}"
[ "$script_dir" != "${BASH_SOURCE[0]}" ] || script_dir=.
script_dir="$(cd "$script_dir" && pwd)"
source "$script_dir/state-lib.sh"
locked=false
if [ "${1:-}" = --agent-state-lock-held ]; then
    locked=true
    shift
fi
original_args=("$@")

agent=""
state=""
if_state=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --hook)
            shift
            ;;
        --agent)
            [ "$#" -lt 2 ] && usage && exit 1
            agent="$2"
            shift 2
            ;;
        --state)
            [ "$#" -lt 2 ] && usage && exit 1
            state="$2"
            shift 2
            ;;
        --if-state)
            [ "$#" -lt 2 ] && usage && exit 1
            if_state="$2"
            shift 2
            ;;
        *)
            usage
            exit 1
            ;;
    esac
done

if [ -z "$agent" ] || [ -z "$state" ]; then
    usage
    exit 1
fi

case "$state" in
    running|needs-input|done|off) ;;
    *)
        usage
        exit 1
        ;;
esac
case "$if_state" in
    ""|running|needs-input|done|off) ;;
    *)
        usage
        exit 1
        ;;
esac

if [ "$locked" = true ]; then
    load_state_snapshot
    prune_pane_states
fi
pane_id="$TMUX_AGENT_EVENT_PANE"
if [ -z "$pane_id" ]; then
    # Ignore hooks that cannot be attributed to a live pane.
    exit 0
fi

if [ "$locked" = false ]; then
    # Resolve Codex ancestry while the hook still belongs to its original process
    # tree. A contended writer may continue after its hook runner has returned.
    export TMUX_AGENT_EVENT_PANE="$pane_id"
    export TMUX_AGENT_EVENT_PANE_PID
    lock_agent_state "${original_args[@]}"
fi
# Ignore deferred events if the pane was closed or replaced while they waited.
pane_exists "$pane_id" || exit 0
[ "$(tmux display-message -p -t "$pane_id" '#{pane_pid}:#{pane_dead}')" = "$TMUX_AGENT_EVENT_PANE_PID:0" ] || exit 0
if [ -n "$if_state" ]; then
    current_state=$(tmux_get_env "TMUX_AGENT_PANE_${pane_id}_STATE")
    if [ "${current_state:-off}" != "$if_state" ]; then
        # PostToolUse must also reach a question whose alert is still delayed.
        [ "$if_state" = needs-input ] && [ "$current_state" = running ] &&
            [ -n "$(tmux show-option -pqv -t "$pane_id" @agent-indicator-input-pending)" ] || exit 0
    fi
fi
if [ "$state" != off ] && ! is_enabled "$(tmux_get_option_or_default "@agent-indicator-${state}-enabled" on)"; then
    state=off
fi
# Reject delayed completion hooks from an interrupted or superseded turn before
# changing colors. Completion accounting shares this writer lock.
if ! reconcile_notifications state "$pane_id" "$agent" "$state"; then
    exit 0
fi
pane_session=$(tmux display-message -p -t "$pane_id" '#{session_id}' 2>/dev/null || true)
if [ -n "$pane_session" ]; then
    tmux set-option -qu -t "$pane_session" @agent-indicator-session-seen 2>/dev/null || true
fi
apply_agent_state "$pane_id" "$agent" "$state" "$TMUX_AGENT_EVENT_PANE_PID" || exit 0
sync_animation
unlock_agent_state
if [ "$state" != off ] && [ "$state" != done ] && [ "$notification_silent" = false ]; then
    notify_state_change "$agent" "$state" "$pane_id"
fi
tmux refresh-client -S >/dev/null 2>&1 || true
