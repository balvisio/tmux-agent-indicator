#!/usr/bin/env bash
# Notification delivery and window accounting. Source through state-lib.sh.

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
        TMUX_PANE="$pane_id" AGENT_NAME="$agent" AGENT_STATE="$state" \
        AGENT_SESSION="$session_name" AGENT_WINDOW="$window_name" \
        bash -c "$ext_cmd" 2>/dev/null &
    fi
}

# Caller holds the state lock. Emit one completion after all work is accounted for.
reconcile_notifications() {
    local script_dir events pane agent skipped=false input_agent input_state input_notify input_pid
    notification_silent=false
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    events=$(python3 "$script_dir/notification-state.py" "$@") || return 1
    while read -r pane agent input_agent input_state input_notify input_pid; do
        [ -n "$pane" ] || continue
        if [ "$pane" = STATE ]; then
            state="$agent"
            continue
        fi
        if [ "$pane" = QUIET ]; then
            notification_silent=true
            continue
        fi
        if [ "$pane" = INPUT ]; then
            if apply_agent_state "$agent" "$input_agent" "$input_state" "$input_pid"; then
                sync_animation
                if [ "$input_notify" = 1 ]; then
                    notify_state_change "$input_agent" needs-input "$agent"
                fi
            fi
            continue
        fi
        if [ "$pane" = RENDER ]; then
            render_window_state "$agent"
            continue
        fi
        if [ "$pane" = SKIP ]; then
            skipped=true
            continue
        fi
        notify_state_change "$agent" done "$pane"
    done <<< "$events"
    [ "$skipped" = false ]
}
