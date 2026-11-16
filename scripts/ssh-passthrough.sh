#!/usr/bin/env bash
# Toggle remote keyboard control; the presence sweep calls check when SSH may have ended.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Serialize toggles and checks independently of agent state and style updates.
if [ "${1:-}" != --agent-state-lock-held ]; then
    # Manual bindings must finish routing before tmux handles the next key.
    # Automatic bindings already run in the background; neither may detach a
    # delayed mutation from its caller and silently weaken this ordering.
    exec python3 "$script_dir/state-lock.py" --ssh-passthrough --wait "$0" "$@"
fi
shift

action="${1:-}"; session="${2:-}"; pane="${3:-}"
case "$action" in
    on|auto|off|check) ;;
    *) echo "usage: ${0##*/} on|auto <session_id> <pane_id> | off|check <session_id>" >&2; exit 2 ;;
esac
[ -n "$session" ] || exit 2
# One server snapshot instead of spawning a client for each setting/pane check.
# Keep a terminating line for each value: no output means inherited; a blank
# line means explicitly empty. Never evaluate tmux's quoted option output.
declare -A settings=() ssh_panes=()
marker="agent-ssh-$$-$RANDOM:"
queries=(display-message -p -t "$session" '#{session_id}|#{pane_id}|#{@ssh_passthrough_pane}')
for option in prefix prefix2 key-table @ssh_passthrough_saved_prefix @ssh_passthrough_saved_prefix2 @ssh_passthrough_saved_key-table; do
    queries+=(\; display-message -p "${marker}option:$option" \; show-options -qv -t "$session" "$option")
done
queries+=(\; display-message -p "${marker}panes" \; list-panes -s -t "$session" -F '#{pane_id}|#{pane_dead}|#{pane_current_command}|#{@ssh_reconnect_host}|#{@ssh_reconnect_pid}')
snapshot=$(tmux "${queries[@]}" 2>/dev/null) || exit 0
IFS='|' read -r session selected owner <<< "${snapshot%%$'\n'*}"
section=''
while IFS= read -r line; do
    case "$line" in
        "${marker}option:"*) section="${line#"${marker}option:"}" ;;
        "${marker}panes") section=panes ;;
        *)
            if [ "$section" = panes ]; then
                ssh_panes["${line%%|*}"]="${line#*|}"
            elif [ -n "$section" ]; then
                settings["$section"]+="$line"$'\n'
            fi
            ;;
    esac
done <<< "${snapshot#*$'\n'}"
automatic=false
[ "$action" != auto ] || automatic=true

quote_tmux() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//\$/\\\$}"
    value="${value//$'\n'/\\n}"
    value="${value//$'\r'/\\r}"
    value="${value//$'\t'/\\t}"
    printf '"%s" ' "$value"
}

apply_commands() {
    if [ "$automatic" = false ]; then
        tmux "$@" \; if-shell -F '#{client_name}' 'refresh-client -S'
        return
    fi
    # Mouse events keep flowing while this helper runs. Check at the actual
    # mutation, inside tmux, so a click away cannot leave local keys routed to SSH.
    local argument
    {
        printf 'if-shell -F -t '
        quote_tmux "$session"
        quote_tmux "#{==:#{pane_id},$pane}"
        printf '{\n'
        for argument in "$@"; do
            if [ "$argument" = ';' ]; then printf '\n'; else quote_tmux "$argument"; fi
        done
        printf '\nif-shell -F "#{client_name}" "refresh-client -S"\n}\n'
    } | tmux source-file -
}

pane_is_ssh() {
    local dead command host pid
    # A closed or moved pane must not leave its former session in passthrough.
    [ -n "${ssh_panes[$pane]:-}" ] || return 1
    IFS='|' read -r dead command host pid <<< "${ssh_panes[$pane]}"
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
        saved="${settings[@ssh_passthrough_saved_$option]:-}"
        saved="${saved%$'\n'}"
        case "$saved" in
            set:*) commands+=(set-option -t "$session" "$option" "${saved#set:}" \;) ;;
            unset) commands+=(set-option -t "$session" -u "$option" \;) ;;
        esac
        commands+=(set-option -t "$session" -qu "@ssh_passthrough_saved_$option" \;)
    done
    apply_commands "${commands[@]}" set-option -t "$session" -qu @ssh_passthrough_pane
}

# Mouse selection happens before this helper runs. A queued click must not change
# keyboard routing after the user has selected a different window or pane.
if [ "$action" = auto ]; then
    [ -n "$pane" ] || exit 0
    [ "$selected" = "$pane" ] || exit 0
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
                if [ -n "${settings[$option]:-}" ]; then
                    saved="set:${settings[$option]%$'\n'}"
                fi
                commands+=(set-option -t "$session" "@ssh_passthrough_saved_$option" "$saved" \;)
            done
        fi
        apply_commands "${commands[@]}" set-option -t "$session" prefix2 None \; \
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
