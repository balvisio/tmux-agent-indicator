#!/usr/bin/env bash
# tmux-agent-indicator bootstrap.

CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
    local match="${3:-$CURRENT_DIR}"
    local existing_hooks
    existing_hooks=$(tmux show-hooks -g "$hook_type" 2>/dev/null || true)

    # Remove all previously registered plugin hooks to avoid duplicates.
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        local existing_name
        existing_name="${line%% *}"
        tmux set-hook -gu "$existing_name" 2>/dev/null || true
    done < <(printf '%s\n' "$existing_hooks" | grep -F "$match" || true)

    tmux set-hook -ag "$hook_type" "$command"
}

register_focus_hooks() {
    local hook_script="$CURRENT_DIR/scripts/pane-focus-in.sh"
    local hook_command="run-shell \"$hook_script \\\"#{pane_id}\\\" \\\"#{window_id}\\\"\""
    register_hook_once "pane-focus-in" "$hook_command"
    register_hook_once "after-select-window" "$hook_command"
    register_hook_once "after-select-pane" "$hook_command"
    # Window switches that bypass select-window (a status-bar mouse click runs switch-client;
    # next-/previous-/last-window have no after-* hooks) still fire session-window-changed.
    register_hook_once "session-window-changed" "$hook_command"
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
        tmux bind-key -n C-c send-keys C-c '\;' run-shell "\"$script\" \"#{pane_id}\""
    fi
}

main() {
    tmux set-environment -g TMUX_AGENT_INDICATOR_DIR "$CURRENT_DIR"
    update_tmux_option "status-right"
    update_tmux_option "status-left"
    update_tmux_option "@minimal-tmux-status-right"
    update_tmux_option "@minimal-tmux-status-left"
    update_tmux_option "@minimal-tmux-status-right-extra"
    update_tmux_option "@minimal-tmux-status-left-extra"
    register_focus_hooks
    register_interrupt_binding
    local session_script="$CURRENT_DIR/scripts/session-changed.sh"
    register_hook_once "client-session-changed" "run-shell \"$session_script\""
}

main
