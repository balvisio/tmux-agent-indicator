#!/usr/bin/env bash
# Toggle remote keyboard control; the presence sweep calls check when SSH may have ended.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Serialize toggles and sweep checks with the existing server-scoped state writer lock.
if [ "${1:-}" != --agent-state-lock-held ]; then
    exec python3 "$script_dir/state-lock.py" "$0" "$@"
fi
shift

action="${1:-}"; session="${2:-}"; pane="${3:-}"
case "$action" in
    on|auto|off|check) ;;
    *) echo "usage: ${0##*/} on|auto <session_id> <pane_id> | off|check <session_id>" >&2; exit 2 ;;
esac
[ -n "$session" ] || exit 2
tmux has-session -t "$session" 2>/dev/null || exit 0

owner=$(tmux show-options -t "$session" -qv @ssh_passthrough_pane)

pane_is_ssh() {
    local dead command host pid
    # A closed or moved pane must not leave its former session in passthrough.
    tmux list-panes -s -t "$session" -F '#{pane_id}' | grep -xF "$pane" >/dev/null || return 1
    IFS='|' read -r dead command host pid < <(tmux display-message -p -t "$pane" \
        '#{pane_dead}|#{pane_current_command}|#{@ssh_reconnect_host}|#{@ssh_reconnect_pid}')
    [ "$dead" = 0 ] || return 1
    [ "$command" != ssh ] || return 0
    [ -n "$host" ] || return 1
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$pid" 2>/dev/null
}

restore() {
    local option saved
    local -a commands=()
    for option in prefix prefix2 key-table; do
        saved=$(tmux show-options -t "$session" -qv "@ssh_passthrough_saved_$option")
        case "$saved" in
            set:*) commands+=(set-option -t "$session" "$option" "${saved#set:}" \;) ;;
            unset) commands+=(set-option -t "$session" -u "$option" \;) ;;
        esac
        commands+=(set-option -t "$session" -qu "@ssh_passthrough_saved_$option" \;)
    done
    tmux "${commands[@]}" set-option -t "$session" -qu @ssh_passthrough_pane
}

# Mouse selection happens before this helper runs. A queued click must not change
# keyboard routing after the user has selected a different window or pane.
if [ "$action" = auto ]; then
    [ -n "$pane" ] || exit 0
    [ "$(tmux display-message -p -t "$session" '#{pane_id}')" = "$pane" ] || exit 0
    if pane_is_ssh; then action=on; else action=off; fi
fi

case "$action" in
    on)
        [ -n "$pane" ] && pane_is_ssh || exit 0
        commands=(set-option -t "$session" prefix None \;)
        # Repeated activation may change the owner, but must retain the original keyboard settings.
        if [ -z "$owner" ]; then
            for option in prefix prefix2 key-table; do
                saved=unset
                if [ -n "$(tmux show-options -t "$session" -q "$option")" ]; then
                    saved="set:$(tmux show-options -t "$session" -qv "$option")"
                fi
                commands+=(set-option -t "$session" "@ssh_passthrough_saved_$option" "$saved" \;)
            done
        fi
        tmux "${commands[@]}" set-option -t "$session" prefix2 None \; \
            set-option -t "$session" key-table off \; \
            set-option -t "$session" @ssh_passthrough_pane "$pane"
        ;;
    off)
        [ -n "$owner" ] || exit 0
        restore
        ;;
    check)
        [ -n "$owner" ] || exit 0
        pane="$owner"
        if pane_is_ssh; then exit 0; fi
        restore
        tmux display-message -t "$session" 'local tmux: ON (ssh ended)' 2>/dev/null || true
        ;;
esac
tmux refresh-client -S 2>/dev/null || true
