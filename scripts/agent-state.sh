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

notify_state_change() {
    local agent="$1" state="$2" pane_id="$3"

    local notif_enabled
    notif_enabled=$(tmux_get_option_or_default "@agent-indicator-notification-enabled" "on")
    is_enabled "$notif_enabled" || return 0

    local notif_states
    notif_states=$(tmux_get_option_or_default "@agent-indicator-notification-states" "needs-input,done")
    case ",$notif_states," in
        *",${state},"*) ;;
        *) return 0 ;;
    esac

    local fmt duration
    fmt=$(tmux_get_option_or_default "@agent-indicator-notification-format" \
        "[#{agent_name}] #{agent_state} (#{session_name}:#{window_name})")
    duration=$(tmux_get_option_or_default "@agent-indicator-notification-duration" "5000")

    local session_name window_name window_index
    session_name=$(tmux display-message -p -t "$pane_id" '#S')
    window_name=$(tmux display-message -p -t "$pane_id" '#W')
    window_index=$(tmux display-message -p -t "$pane_id" '#I')

    local message="$fmt"
    message="${message//\#\{agent_name\}/$agent}"
    message="${message//\#\{agent_state\}/$state}"
    message="${message//\#\{session_name\}/$session_name}"
    message="${message//\#\{window_name\}/$window_name}"
    message="${message//\#\{window_index\}/$window_index}"

    local notif_message
    notif_message=$(tmux_get_option_or_default "@agent-indicator-notification-message" "on")
    if is_enabled "$notif_message"; then
        if [ -n "$duration" ] && [ "$duration" != "0" ]; then
            tmux display-message -d "$duration" "$message" || true
        else
            tmux display-message "$message" || true
        fi
    fi

    local ext_cmd
    ext_cmd=$(tmux_get_option_or_default "@agent-indicator-notification-command" "")
    if [ -n "$ext_cmd" ]; then
        AGENT_NAME="$agent" AGENT_STATE="$state" \
        AGENT_SESSION="$session_name" AGENT_WINDOW="$window_name" \
        bash -c "$ext_cmd" 2>/dev/null &
    fi
}

agent=""
state=""
if_state=""
while [ "$#" -gt 0 ]; do
    case "$1" in
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
    [ "${current_state:-off}" = "$if_state" ] || exit 0
fi
window_id=$(tmux display-message -p -t "$pane_id" '#{window_id}')
active_pane_id=$(tmux display-message -p '#{pane_id}')
pane_session=$(tmux display-message -p -t "$pane_id" '#{session_name}' 2>/dev/null || true)
if [ -n "$pane_session" ]; then
    tmux_unset_env "TMUX_AGENT_SESSION_SEEN_${pane_session}"
fi

if [ "$state" != off ] && ! is_enabled "$(tmux_get_option_or_default "@agent-indicator-${state}-enabled" on)"; then
    state=off
fi
clear_pane_state "$pane_id"
if [ "$state" != off ]; then
    tmux_set_env "TMUX_AGENT_PANE_${pane_id}_STATE" "$state"
    tmux_set_env "TMUX_AGENT_PANE_${pane_id}_AGENT" "$agent"
    tmux_set_env "TMUX_AGENT_PANE_${pane_id}_PID" "$TMUX_AGENT_EVENT_PANE_PID"
    tmux_set_env "TMUX_AGENT_ACTIVE_PANE_${agent}" "$pane_id"
    state_bg=$(tmux_get_option_or_default "@agent-indicator-${state}-bg" "$(default_state_bg "$state")")
    if ! is_enabled "$(tmux_get_option_or_default @agent-indicator-background-enabled on)"; then
        reset_pane_style "$pane_id"
    elif [ "$state_bg" = default ]; then
        reset_pane_style "$pane_id"
    elif [ -n "$state_bg" ] && [ "$pane_id" != "$active_pane_id" ]; then
        apply_pane_style "$pane_id" "$state_bg"
    fi
    if [ "$state" = done ] && ! is_enabled "$(tmux_get_option_or_default @agent-indicator-reset-on-focus on)"; then
        clear_pane_state "$pane_id"
        reset_pane_style "$pane_id"
    fi
else
    reset_pane_style "$pane_id"
fi
render_window_state "$window_id"
sync_animation
unlock_agent_state
if [ "$state" != off ]; then
    notify_state_change "$agent" "$state" "$pane_id"
fi
tmux refresh-client -S >/dev/null 2>&1 || true
