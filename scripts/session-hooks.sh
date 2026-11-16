#!/usr/bin/env bash
# Sourced at bootstrap. Session switches themselves execute only native tmux commands.
register_session_hooks() (
    # Keep strict options inside this bootstrap call, not in the sourcing shell.
    set -euo pipefail
    local command client session
    local -a commands=()
    # run-shell -C expands the hook's client before tmux evaluates the comparison.
    # client_name can resolve to another terminal already in the destination.
    # This client event supplies hook_client but no hook_session; session_id is
    # the destination session in the hook's target context.
    # Escaped hashes defer the option lookup to that second expansion. Only
    # session IDs enter command text; names can contain literal tmux formats.
    # A same-session window switch must not re-acknowledge
    # the last session; each attached terminal has its own remembered session.
    command=$(cat <<'TMUX'
if-shell -F '##{!=:##{@agent-indicator-client-session-#{hook_client}},#{session_id}}' {
    if-shell -F '##{S:##{?##{==:##{session_id},##{@agent-indicator-client-session-#{hook_client}}},1,}}' {
        run-shell -C "set-option -q -t '##{@agent-indicator-client-session-#{hook_client}}' @agent-indicator-session-seen 1"
    }
    set-option -gq @agent-indicator-client-session-#{q:hook_client} #{q:session_id}
    refresh-client -S -t #{q:hook_client}
}
TMUX
    )
    # The session loop emits 1 only if the cached ID still exists. Closing a
    # session must not acknowledge a missing target or use client_last_session,
    # which has the same destination-client ambiguity as client_name.
    # Quote the program as a string, so it is parsed only after -C expansion.
    command="${command//\\/\\\\}"
    command="${command//\"/\\\"}"
    command="${command//\$/\\\$}"
    register_hook_once client-session-changed "run-shell -C \"$command\"" '@agent-indicator-client-session-'
    register_hook_once client-detached \
        'run-shell -C "set-option -gqu @agent-indicator-client-session-#{q:hook_client}"' '@agent-indicator-client-session-'
    # Seed existing clients on reload; first attachment is handled by the hook.
    while IFS='|' read -r client session; do
        [ -n "$client" ] || continue
        commands+=(set-option -gq "@agent-indicator-client-session-$client" "$session" \;)
    done < <(tmux list-clients -F '#{client_name}|#{session_id}')
    [ "${#commands[@]}" -eq 0 ] || tmux "${commands[@]}"
)
