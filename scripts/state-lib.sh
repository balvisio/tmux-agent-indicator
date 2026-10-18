#!/usr/bin/env bash
# Shared state and styling helpers. Source this file; acquire the lock before mutations.

lock_agent_state() {
    tmux wait-for -L agent-indicator-state
    trap 'tmux wait-for -U agent-indicator-state 2>/dev/null || true' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM HUP
}

unlock_agent_state() {
    tmux wait-for -U agent-indicator-state
    trap - EXIT INT TERM HUP
}

clear_pane_state() {
    local pane_id="$1" agent
    agent=$(tmux_get_env "TMUX_AGENT_PANE_${pane_id}_AGENT")
    tmux_unset_env "TMUX_AGENT_PANE_${pane_id}_STATE"
    tmux_unset_env "TMUX_AGENT_PANE_${pane_id}_AGENT"
    if [ -n "$agent" ] && [ "$(tmux_get_env "TMUX_AGENT_ACTIVE_PANE_${agent}")" = "$pane_id" ]; then
        tmux_unset_env "TMUX_AGENT_ACTIVE_PANE_${agent}"
    fi
}

# Only live panes participate. A closed or moved pane cannot keep its old window colored.
window_agent_state() {
    local window_id="$1" pane_id dead state result=idle
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
    local raw
    raw=$(tmux show-option -gq "$option" 2>/dev/null || true)
    [ -n "$raw" ]
}

tmux_get_option_or_default() {
    local option="$1"
    local default_value="$2"
    local value

    if tmux_option_is_set "$option"; then
        value=$(tmux show-option -gqv "$option")
        printf '%s\n' "$value"
    else
        printf '%s\n' "$default_value"
    fi
}

tmux_get_env() {
    local key="$1"
    tmux show-environment -g "$key" 2>/dev/null | sed 's/^[^=]*=//' || true
}

tmux_set_env() {
    tmux set-environment -g "$1" "$2"
}

tmux_unset_env() {
    tmux set-environment -gu "$1" 2>/dev/null || true
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
# which steals focus when the styled pane shares the user's current window.
# The previous capture/restore dance raced with user navigation and could
# resolve the wrong "active" pane when run without a client context (e.g.
# from agent hooks). set-option -p has no focus side effect at all.
reset_pane_style() {
    local pane_id="$1"
    tmux set-option -p -t "$pane_id" window-style "bg=default"
}

apply_pane_style() {
    local pane_id="$1"
    local bg="$2"
    tmux set-option -p -t "$pane_id" window-style "bg=$bg"
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
    tmux set-environment -gu TMUX_AGENT_ANIMATION_PID 2>/dev/null || true
    tmux set-environment -gu TMUX_AGENT_ANIMATION_FRAME 2>/dev/null || true
}

