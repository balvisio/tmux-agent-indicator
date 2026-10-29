#!/usr/bin/env bash
# Set pane/window state for an AI agent.

set -euo pipefail

usage() {
    cat <<'EOF' >&2
Usage: agent-state.sh --agent <name> --state <running|needs-input|done|off> [--if-state <state>]
  --if-state  only act when the pane is currently in that state (off = no state recorded)
EOF
}

if ! command -v tmux >/dev/null 2>&1 || [ -z "${TMUX:-}" ]; then
    exit 0
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/state-lib.sh"
locked=false
if [ "${1:-}" = --agent-state-lock-held ]; then
    locked=true
    shift
fi
original_args=("$@")

# Codex --no-daemon hooks descend from their pane's starting process (pane_pid).
# TMUX_PANE can be missing or stale, so find that process in the hook's ancestry.
resolve_pane_by_ancestry() {
    local panes cur match hops=0
    panes=$(tmux list-panes -a -F '#{pane_pid} #{pane_id}' 2>/dev/null || true)
    [ -n "$panes" ] || return 0
    cur=$$
    while [ "$hops" -lt 40 ]; do
        case "$cur" in ""|0|1|*[!0-9]*) break ;; esac
        match=$(printf '%s\n' "$panes" | awk -v p="$cur" '$1 == p { print $2; exit }')
        if [ -n "$match" ]; then
            printf '%s\n' "$match"
            return 0
        fi
        cur=$(ps -o ppid= -p "$cur" 2>/dev/null | tr -d '[:space:]' || true)
        hops=$((hops + 1))
    done
    return 0
}

resolve_target_pane() {
    local agent="$1"

    if [ "$agent" = codex ]; then
        resolve_pane_by_ancestry
        return
    fi

    # Other agents supply their pane directly (Claude does this).
    if [ -n "${TMUX_PANE:-}" ] && pane_exists "$TMUX_PANE"; then
        printf '%s\n' "$TMUX_PANE"
    fi
}


agent=""
state=""
if_state=""
hook=false
while [ "$#" -gt 0 ]; do
    case "$1" in
        --hook)
            hook=true
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

if [ "$hook" = true ] && [ "$locked" = false ] && [ ! -t 0 ]; then
    TMUX_AGENT_HOOK_DATA=$(python3 "$script_dir/notification-state.py" hook-data)
    export TMUX_AGENT_HOOK_DATA
fi

if [ "$locked" = true ]; then
    load_state_snapshot
    prune_pane_states
    pane_id="$TMUX_AGENT_EVENT_PANE"
else
    pane_id=$(resolve_target_pane "$agent")
fi
if [ -z "$pane_id" ]; then
    # Ignore hooks that cannot be attributed to a live pane.
    exit 0
fi

if [ "$locked" = false ]; then
    # Resolve Codex ancestry while the hook still belongs to its original process
    # tree. A contended writer may continue after its hook runner has returned.
    export TMUX_AGENT_EVENT_PANE="$pane_id"
    TMUX_AGENT_EVENT_PANE_PID="$(tmux display-message -p -t "$pane_id" '#{pane_pid}')"
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
pane_session=$(tmux display-message -p -t "$pane_id" '#{session_name}' 2>/dev/null || true)
if [ -n "$pane_session" ]; then
    tmux_unset_env "TMUX_AGENT_SESSION_SEEN_${pane_session}"
fi

if [ "$state" != off ] && ! is_enabled "$(tmux_get_option_or_default "@agent-indicator-${state}-enabled" on)"; then
    state=off
fi
# Reject delayed completion hooks from an interrupted or superseded turn before
# changing colors. Completion accounting shares this writer lock.
if ! reconcile_notifications state "$pane_id" "$agent" "$state"; then
    exit 0
fi
apply_agent_state "$pane_id" "$agent" "$state" "$TMUX_AGENT_EVENT_PANE_PID" || exit 0
sync_animation
unlock_agent_state
if [ "$state" != off ] && [ "$state" != done ] && [ "$notification_silent" = false ]; then
    notify_state_change "$agent" "$state" "$pane_id"
fi
tmux refresh-client -S >/dev/null 2>&1 || true
