#!/usr/bin/env bash
# Snapshot and batched writes for full sweeps. State hooks keep their FIFO order.
agent_batch=false
agent_commands=""
declare -A agent_windows agent_panes
agent_window_ids=()
agent_active_window=""

load_sweep_snapshot() {
    local kind id key value ready=false
    agent_windows=()
    agent_panes=()
    agent_window_ids=()
    while IFS= read -r -d '' kind && IFS= read -r -d '' id &&
          IFS= read -r -d '' key && IFS= read -r -d '' value; do
        case "$kind" in
            window)
                agent_windows["$id|$key"]="$value"
                [ "$key" != members ] || agent_window_ids+=("$id")
                ;;
            pane) agent_panes["$id|$key"]="$value" ;;
            active) agent_active_window="$value" ;;
            ready) ready=true ;;
        esac
    done < <(python3 "$script_dir/agent-present.py" snapshot)
    [ "$ready" = true ]
}

queue_tmux() {
    local value
    for value in "$@"; do
        # tmux source-file quoting, not shell quoting. Keep names completely literal.
        value="${value//\\/\\\\}"
        value="${value//\"/\\\"}"
        value="${value//\$/\\\$}"
        value="${value//$'\n'/\\n}"
        value="${value//$'\r'/\\r}"
        value="${value//$'\t'/\\t}"
        agent_commands+="\"$value\" "
    done
    agent_commands+=$'\n'
}

flush_sweep() {
    if [ -n "$agent_commands" ]; then
        printf '%s' "$agent_commands" | tmux source-file -
        agent_commands=""
    fi
}

set_window_option() {
    local window="$1" option="$2" value="$3"
    if [ "$agent_batch" = true ]; then
        [ "${agent_windows[$window|$option]-}" != "$value" ] || return 0
        if [ "$value" = __UNSET__ ] && [ -z "${agent_windows[$window|$option]-}" ]; then
            return 0
        fi
        agent_windows["$window|$option"]="$value"
        if [ "$value" = __UNSET__ ]; then
            queue_tmux set-window-option -qut "$window" "$option"
            agent_windows["$window|$option"]=""
        else
            queue_tmux set-window-option -qt "$window" "$option" "$value"
        fi
    elif [ "$value" = __UNSET__ ]; then
        tmux set-window-option -qut "$window" "$option"
    else
        tmux set-window-option -qt "$window" "$option" "$value"
    fi
}

set_idle_style() {
    local window_id="$1" value="$2" option suffix key
    for option in window-status-style window-status-current-style; do
        suffix=ORIG_STATUS_STYLE
        [ "$option" != window-status-current-style ] || suffix=ORIG_STATUS_CURRENT_STYLE
        key="TMUX_AGENT_WINDOW_${window_id}_${suffix}"
        if [ -n "$(tmux_get_env "$key")" ]; then
            tmux_set_env "$key" "$value"
        else
            set_window_option "$window_id" "$option" "$value"
        fi
    done
}

apply_presence_snapshot() {
    local kind id identity label host
    while IFS= read -r -d '' kind && IFS= read -r -d '' id &&
          IFS= read -r -d '' identity && IFS= read -r -d '' label && IFS= read -r -d '' host; do
        if [ "$kind" = stale ]; then
            # Do not erase a new reconnect owner that appeared during the scan.
            if [ "${agent_panes[$id|pid]-}" = "$identity" ] &&
               [ "${agent_panes[$id|marker]-}" = "$label" ] &&
               [ "${agent_panes[$id|marker_pid]-}" = "$host" ]; then
                queue_tmux set-option -pqu -t "$id" @ssh_reconnect_host
                queue_tmux set-option -pqu -t "$id" @ssh_reconnect_pid
            fi
            continue
        fi
        # A moved, closed or respawned pane invalidates that window's detection.
        # Hooks still reconcile its current state; the next scan refreshes labels.
        [ "${agent_windows[$id|signature]-}" = "$identity" ] || continue
        if [ -n "$label" ]; then
            set_idle_style "$id" 'bg=blue,fg=white'
            set_window_option "$id" @agent_present 1
            set_window_option "$id" @agent_label "$label"
        elif [ "${agent_windows[$id|@agent_present]-}" = 1 ]; then
            set_idle_style "$id" __UNSET__
            set_window_option "$id" @agent_present __UNSET__
            set_window_option "$id" @agent_label __UNSET__
        fi
        if [ -n "$host" ]; then
            set_window_option "$id" @ssh_host "$host"
        elif [ -n "${agent_windows[$id|@ssh_host]-}" ]; then
            set_window_option "$id" @ssh_host __UNSET__
        fi
    done
}
