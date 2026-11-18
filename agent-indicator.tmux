#!/usr/bin/env bash
# tmux-agent-indicator bootstrap.

CURRENT_DIR="${BASH_SOURCE[0]%/*}"
[ "$CURRENT_DIR" != "${BASH_SOURCE[0]}" ] || CURRENT_DIR=.
CURRENT_DIR="$(cd "$CURRENT_DIR" && pwd)"

do_interpolation() {
    local string="$1"
    string="${string//\#\{agent_indicator\}/#($CURRENT_DIR/scripts/indicator.sh)}"
    string="${string//\#\{agent_limits\}/#($CURRENT_DIR/scripts/agent-limits.sh)}"
    string="${string//\#\{agent_session_dots\}/#($CURRENT_DIR/scripts/session-dots.sh '#S')}"
    echo "$string"
}

update_tmux_option() {
    local option="$1"
    local option_value
    option_value=$(tmux show-option -gqv "$option")
    if [ -z "$option_value" ]; then
        return
    fi
    tmux set-option -gq "$option" "$(do_interpolation "$option_value")"
}

register_hook_once() {
    local hook_type="$1"
    local command="$2"
    local match="$3"
    local existing_hooks
    existing_hooks=$(tmux show-hooks -g "$hook_type" 2>/dev/null || true)

    # Each owner replaces its own slot, preserving recovery and user hooks.
    local -A occupied=()
    local slot=900
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        local existing_name
        existing_name="${line%% *}"
        if [[ "$line" == *"$match"* ]] ||
           { [ "$hook_type" = client-session-changed ] && [[ "$line" == *"$CURRENT_DIR/scripts/session-changed.sh"* ]]; }; then
            tmux set-hook -gu "$existing_name" 2>/dev/null || true
        else
            occupied["$existing_name"]=1
        fi
    done <<< "$existing_hooks"
    while [ -n "${occupied[${hook_type}[$slot]]-}" ]; do
        slot=$((slot + 1))
    done
    tmux set-hook -g "${hook_type}[$slot]" "$command"
}

register_interrupt_binding() {
    local script="$CURRENT_DIR/scripts/pane-interrupt.sh"
    local existing
    existing=$(tmux list-keys -T root 2>/dev/null | awk '
        { for (i = 2; i < NF; i++) if ($i == "-T") {
            if ($(i + 1) == "root" && $(i + 2) == "C-c") print
            break
        } }' || true)
    # Preserve custom bindings; reload only our own binding.
    if [ -z "$existing" ] || [[ "$existing" == *"$script"* ]]; then
        tmux bind-key -n C-c send-keys C-c '\;' if-shell -F '#{!=:#{@agent-indicator-interrupt},}' \
            "run-shell -b '\"$script\" \"#{pane_id}\" \"#{@agent-indicator-interrupt}\"'"
    fi
}

main() {
    tmux set-environment -g TMUX_AGENT_INDICATOR_DIR "$CURRENT_DIR"
    python3 "$CURRENT_DIR/scripts/agent-present.py" setup
    update_tmux_option "status-right"
    update_tmux_option "status-left"
    update_tmux_option "@minimal-tmux-status-right"
    update_tmux_option "@minimal-tmux-status-left"
    update_tmux_option "@minimal-tmux-status-right-extra"
    update_tmux_option "@minimal-tmux-status-left-extra"
    python3 "$CURRENT_DIR/scripts/navigation.py"
    register_interrupt_binding
    # Keep menu-bar labels derived from tmux's real window name, including manual
    # renames and inactive windows. Native commands do not spawn a shell per rename.
    register_hook_once "window-renamed" \
        'if-shell -F "#{@agent_present}" { set-window-option -qF @agent_label "#{@agent_icons} #{window_name}" }' \
        '@agent_label'
    # Recompute both source and destination windows after panes move, close, or exit.
    local state_script="$CURRENT_DIR/scripts/window-state.sh"
    tmux run-shell -b "\"$state_script\""
    source "$CURRENT_DIR/scripts/session-hooks.sh"
    register_session_hooks
}

main
