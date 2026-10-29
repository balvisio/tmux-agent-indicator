#!/usr/bin/env bash
# Shared state and styling helpers. Source this file; acquire the lock before mutations.

declare -A agent_options agent_environment
agent_snapshot_loaded=false

# One snapshot per writer; environment writes below keep the local copy current.
load_state_snapshot() {
    local script_dir name value line
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    agent_options=()
    agent_environment=()
    agent_snapshot_loaded=false
    while IFS= read -r -d '' name && IFS= read -r -d '' value; do
        if [ "$name" = __SNAPSHOT_OK__ ]; then
            agent_snapshot_loaded=true
            continue
        fi
        agent_options["$name"]="$value"
    done < <(python3 "$script_dir/notification-state.py" options)
    while IFS= read -r line; do
        case "$line" in
            TMUX_AGENT_*=*) agent_environment["${line%%=*}"]="${line#*=}" ;;
        esac
    done < <(tmux show-environment -g)
}

# Call at the entrypoint with its original arguments, then shift the private marker.
# The Python launcher acquires a process-owned record lock and execs this shell.
lock_agent_state() {
    if [ "${1:-}" != --agent-state-lock-held ]; then
        local script_dir
        script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        exec python3 "$script_dir/state-lock.py" "$0" "$@"
    fi
    load_state_snapshot
    prune_pane_states
}

unlock_agent_state() {
    # POSIX record locks belong to this process, not to children inheriting descriptor 9.
    exec 9>&-
}

clear_pane_state() {
    local pane_id="$1" agent
    agent=$(tmux_get_env "TMUX_AGENT_PANE_${pane_id}_AGENT")
    tmux_unset_env "TMUX_AGENT_PANE_${pane_id}_STATE"
    tmux_unset_env "TMUX_AGENT_PANE_${pane_id}_AGENT"
    tmux_unset_env "TMUX_AGENT_PANE_${pane_id}_PID"
    if [ -n "$agent" ] && [ "$(tmux_get_env "TMUX_AGENT_ACTIVE_PANE_${agent}")" = "$pane_id" ]; then
        tmux_unset_env "TMUX_AGENT_ACTIVE_PANE_${agent}"
    fi
}

# Pane IDs survive respawn; recorded states belong to the process that emitted
# them. Prune under the writer lock before applying hooks, focus, or sweeps.
prune_pane_states() {
    local panes line pane_id recorded_pid current_pid dead window_id affected=""
    panes=$(tmux list-panes -a -F '#{pane_id} #{pane_pid} #{pane_dead} #{window_id}')
    while IFS= read -r line; do
        pane_id="${line#TMUX_AGENT_PANE_}"
        pane_id="${pane_id%%_STATE=*}"
        recorded_pid=$(tmux_get_env "TMUX_AGENT_PANE_${pane_id}_PID")
        current_pid=""; dead=""; window_id=""
        read -r current_pid dead window_id < <(printf '%s\n' "$panes" | awk -v p="$pane_id" '$1 == p { print $2, $3, $4; exit }') || true
        if [ -z "$recorded_pid" ] || [ "$current_pid" != "$recorded_pid" ] || [ "$dead" != 0 ]; then
            clear_pane_state "$pane_id"
            if [ -n "$window_id" ]; then
                reset_pane_style "$pane_id" || true
                affected="$affected $window_id"
            fi
        fi
    done < <(tmux show-environment -g | grep '^TMUX_AGENT_PANE_.*_STATE=' || true)
    for window_id in $affected; do
        render_window_state "$window_id"
    done
}

# Only live panes participate. A closed or moved pane cannot keep its old window colored.
window_agent_state() {
    local window_id="$1" pane_id dead state result=idle
    # A reserved review keeps the window busy across the gap between agents.
    if [ "$(tmux show-option -wqv -t "$window_id" @agent-indicator-review-pending)" = 1 ] &&
        is_enabled "$(tmux_get_option_or_default @agent-indicator-running-enabled on)"; then
        result=running
    fi
    while read -r pane_id dead; do
        [ "$dead" = 0 ] || continue
        state=$(tmux_get_env "TMUX_AGENT_PANE_${pane_id}_STATE")
        case "$state" in
            needs-input) printf '%s\n' needs-input; return ;;
            running) result=running ;;
            done) [ "$result" != idle ] || result=done ;;
        esac
    done < <(tmux list-panes -t "$window_id" -F '#{pane_id} #{pane_dead}' 2>/dev/null || true)
    printf '%s\n' "$result"
}

render_window_state() {
    local window_id="$1" state bg fg border active_window style_active saved
    tmux list-windows -a -F '#{window_id}' | grep -qxF "$window_id" || return 0
    state=$(window_agent_state "$window_id")
    if [ "$state" = idle ]; then
        clear_window_title_style "$window_id"
        restore_active_border_style "$window_id"
    else
        bg=$(tmux_get_option_or_default "@agent-indicator-${state}-window-title-bg" "$(default_state_title_bg "$state")")
        fg=$(tmux_get_option_or_default "@agent-indicator-${state}-window-title-fg" "$(default_state_title_fg "$state")")
        active_window=$(tmux display-message -p '#{window_id}')
        style_active=$(tmux_get_option_or_default @agent-indicator-style-active-window off)
        saved=$(tmux_get_env "TMUX_AGENT_WINDOW_${window_id}_ORIG_STATUS_STYLE")
        if [ -z "$bg" ] || [ -z "$fg" ]; then
            clear_window_title_style "$window_id"
        elif [ "$window_id" != "$active_window" ] || is_enabled "$style_active" || [ -n "$saved" ]; then
            apply_window_title_style "$window_id" "$bg" "$fg"
        fi
        border=$(tmux_get_option_or_default "@agent-indicator-${state}-border" "$(default_state_border "$state")")
        if ! is_enabled "$(tmux_get_option_or_default @agent-indicator-border-enabled on)" || [ "$border" = default ]; then
            restore_active_border_style "$window_id"
        elif [ -n "$border" ]; then
            apply_active_border_style "$window_id" "$border"
        fi
    fi
    # Consumers (including menu-bar dots) read this instead of choosing their own priority.
    tmux set-window-option -qt "$window_id" @agent-indicator-state "$state"
}

sync_animation() {
    local pane_id dead
    if ! is_enabled "$(tmux_get_option_or_default @agent-indicator-animation-enabled off)" && [ -z "$(tmux_get_env TMUX_AGENT_ANIMATION_PID)" ]; then
        return
    fi
    while read -r pane_id dead; do
        [ "$dead" = 0 ] || continue
        if [ "$(tmux_get_env "TMUX_AGENT_PANE_${pane_id}_STATE")" = running ]; then
            start_animation
            return
        fi
    done < <(tmux list-panes -a -F '#{pane_id} #{pane_dead}')
    stop_animation
}

tmux_option_is_set() {
    local option="$1"
    if [ "$agent_snapshot_loaded" = true ]; then
        [ "${agent_options[$option]+set}" = set ]
        return
    fi
    local raw
    raw=$(tmux show-option -gq "$option" 2>/dev/null || true)
    [ -n "$raw" ]
}

tmux_get_option_or_default() {
    local option="$1"
    local default_value="$2"
    local value

    if [ "$agent_snapshot_loaded" = true ]; then
        printf '%s\n' "${agent_options[$option]-$default_value}"
        return
    fi
    if tmux_option_is_set "$option"; then
        value=$(tmux show-option -gqv "$option")
        printf '%s\n' "$value"
    else
        printf '%s\n' "$default_value"
    fi
}

tmux_get_env() {
    local key="$1"
    if [ "$agent_snapshot_loaded" = true ]; then
        printf '%s\n' "${agent_environment[$key]-}"
        return
    fi
    tmux show-environment -g "$key" 2>/dev/null | sed 's/^[^=]*=//' || true
}

tmux_set_env() {
    tmux set-environment -g "$1" "$2"
    agent_environment["$1"]="$2"
}

tmux_unset_env() {
    tmux set-environment -gu "$1" 2>/dev/null || true
    unset 'agent_environment[$1]'
}

is_enabled() {
    case "$1" in
        on|true|yes|1) return 0 ;;
        *) return 1 ;;
    esac
}

default_state_bg() {
    case "$1" in
        running) printf 'default\n' ;;
        needs-input) printf 'default\n' ;;
        done) printf 'default\n' ;;
        *) printf 'default\n' ;;
    esac
}

default_state_border() {
    case "$1" in
        running) printf 'default\n' ;;
        needs-input) printf 'yellow\n' ;;
        done) printf 'green\n' ;;
        *) printf 'default\n' ;;
    esac
}

default_state_title_bg() {
    case "$1" in
        needs-input) printf 'yellow\n' ;;
        done) printf 'red\n' ;;
        *) printf '\n' ;;
    esac
}

default_state_title_fg() {
    case "$1" in
        needs-input) printf 'black\n' ;;
        done) printf 'black\n' ;;
        *) printf '\n' ;;
    esac
}

save_window_option_once() {
    local window_id="$1"
    local option="$2"
    local env_key="$3"
    local marker="__UNSET__"
    local existing saved

    existing=$(tmux_get_env "$env_key")
    if [ -n "$existing" ]; then
        return
    fi

    saved=$(tmux show-options -wqv -t "$window_id" "$option" 2>/dev/null || true)
    if [ -z "$saved" ]; then
        tmux_set_env "$env_key" "$marker"
    else
        tmux_set_env "$env_key" "$saved"
    fi
}

restore_window_option() {
    local window_id="$1"
    local option="$2"
    local env_key="$3"
    local marker="__UNSET__"
    local saved

    saved=$(tmux_get_env "$env_key")
    if [ -z "$saved" ]; then
        return
    fi
    if [ "$saved" = "$marker" ]; then
        tmux set-window-option -qt "$window_id" -u "$option" || true
    else
        tmux set-window-option -qt "$window_id" "$option" "$saved"
    fi
    tmux_unset_env "$env_key"
}

apply_window_title_style() {
    local window_id="$1"
    local bg="$2"
    local fg="$3"
    local status_key="TMUX_AGENT_WINDOW_${window_id}_ORIG_STATUS_STYLE"
    local current_key="TMUX_AGENT_WINDOW_${window_id}_ORIG_STATUS_CURRENT_STYLE"

    if [ -z "$bg" ] || [ -z "$fg" ]; then
        return
    fi

    save_window_option_once "$window_id" "window-status-style" "$status_key"
    save_window_option_once "$window_id" "window-status-current-style" "$current_key"
    tmux set-window-option -qt "$window_id" window-status-style "bg=$bg,fg=$fg"
    tmux set-window-option -qt "$window_id" window-status-current-style "bg=$bg,fg=$fg"
}

clear_window_title_style() {
    local window_id="$1"
    [ -z "$window_id" ] && return
    local status_key="TMUX_AGENT_WINDOW_${window_id}_ORIG_STATUS_STYLE"
    local current_key="TMUX_AGENT_WINDOW_${window_id}_ORIG_STATUS_CURRENT_STYLE"

    restore_window_option "$window_id" "window-status-style" "$status_key"
    restore_window_option "$window_id" "window-status-current-style" "$current_key"
}

apply_active_border_style() {
    local window_id="$1"
    local border="$2"
    local orig_key="TMUX_AGENT_WINDOW_${window_id}_ORIG_ACTIVE_BORDER_STYLE"
    save_window_option_once "$window_id" "pane-active-border-style" "$orig_key"
    tmux set-window-option -qt "$window_id" pane-active-border-style "fg=$border,bold"
}

restore_active_border_style() {
    local window_id="$1"
    local orig_key="TMUX_AGENT_WINDOW_${window_id}_ORIG_ACTIVE_BORDER_STYLE"
    restore_window_option "$window_id" "pane-active-border-style" "$orig_key"
}

# Style panes via the pane-scoped window-style option rather than
# `select-pane -P`: select-pane also makes the target pane the active pane,
# which steals focus when the styled pane shares the user's current window;
# capturing and restoring the active pane around it races with user navigation
# and has no reliable "active" pane without a client context (e.g. from agent
# hooks). set-option -p has no focus side effect at all.
reset_pane_style() {
    local pane_id="$1"
    tmux set-option -p -t "$pane_id" window-style "bg=default"
}

apply_pane_style() {
    local pane_id="$1"
    local bg="$2"
    tmux set-option -p -t "$pane_id" window-style "bg=$bg"
}

# Publish a pane state and its aggregate window style under the writer lock.
apply_agent_state() {
    local pane_id="$1" agent="$2" state="$3" pane_pid="$4"
    local window_id active_pane_id state_bg
    pane_exists "$pane_id" || return 1
    [ "$(tmux display-message -p -t "$pane_id" '#{pane_pid}:#{pane_dead}')" = "$pane_pid:0" ] || return 1
    window_id=$(tmux display-message -p -t "$pane_id" '#{window_id}')
    active_pane_id=$(tmux display-message -p '#{pane_id}')
    clear_pane_state "$pane_id"
    if [ "$state" != off ]; then
        tmux_set_env "TMUX_AGENT_PANE_${pane_id}_STATE" "$state"
        tmux_set_env "TMUX_AGENT_PANE_${pane_id}_AGENT" "$agent"
        tmux_set_env "TMUX_AGENT_PANE_${pane_id}_PID" "$pane_pid"
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
}

pane_exists() {
    local pane_id="$1"
    [ -n "$pane_id" ] || return 1
    tmux display-message -p -t "$pane_id" '#{pane_id}' >/dev/null 2>&1
}

start_animation() {
    local anim_enabled
    anim_enabled=$(tmux_get_option_or_default "@agent-indicator-animation-enabled" "off")
    if ! is_enabled "$anim_enabled"; then
        return
    fi

    # Check if animation process is already alive.
    local existing_pid
    existing_pid=$(tmux_get_env "TMUX_AGENT_ANIMATION_PID")
    if [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null; then
        return
    fi

    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    nohup bash "$script_dir/animation.sh" >/dev/null 2>&1 &
    tmux_set_env TMUX_AGENT_ANIMATION_PID "$!"
    disown
}

stop_animation() {
    local pid
    pid=$(tmux_get_env "TMUX_AGENT_ANIMATION_PID")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
    fi
    tmux_unset_env TMUX_AGENT_ANIMATION_PID
    tmux_unset_env TMUX_AGENT_ANIMATION_FRAME
}


source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/notification-lib.sh"
