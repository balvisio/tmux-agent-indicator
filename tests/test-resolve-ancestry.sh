#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"

trap cleanup_test_server EXIT
setup_test_server "resolve-ancestry"
tmux_cmd set -g @agent-indicator-notification-enabled off
tmux_cmd set -g @agent-indicator-animation-enabled off
tmux_cmd set -g @agent-indicator-running-window-title-bg yellow
tmux_cmd set -g @agent-indicator-running-window-title-fg black

# Keep an unrelated window selected while two other panes emit Codex hook events.
source_a=$(tmux_cmd new-window -d -P -F '#{pane_id}' -t ai -n source-a '/bin/bash --noprofile --norc')
source_b=$(tmux_cmd new-window -d -P -F '#{pane_id}' -t ai -n source-b '/bin/bash --noprofile --norc')
completion=$(mktemp /tmp/agent-ancestry-result.XXXXXX)
register_tmp_file "$completion"

run_pane_hook() {
    local source="$1" mode="$2" state="$3" command result attempt
    local env_args='-u TMUX_PANE'
    if [ "$mode" = stale ]; then
        env_args="TMUX_PANE=$PANE"
    fi
    : > "$completion"
    printf -v command 'env %s %q --agent codex --state %q; printf "%%s\\n" "$?" > %q' \
        "$env_args" "$REPO_ROOT/scripts/agent-state.sh" "$state" "$completion"
    # run-shell would descend from the tmux server, bypassing the pane's ancestry.
    tmux_cmd send-keys -t "$source" -l "$command"
    tmux_cmd send-keys -t "$source" Enter
    for attempt in $(seq 1 100); do
        [ -s "$completion" ] && break
        sleep 0.05
    done
    result=$(cat "$completion")
    [ "$result" = 0 ] || fail "hook in $source did not complete successfully: $result"
}

for mode in stale missing; do
    for state in running needs-input done off; do
        for source in "$source_a" "$source_b"; do
            run_pane_hook "$source" "$mode" "$state"
            actual=$(get_env "TMUX_AGENT_PANE_${source}_STATE")
            if [ "$state" = off ]; then
                assert_empty "$actual" "off must clear the source pane"
                expected_style=''
            else
                [ "$actual" = "$state" ] || fail "$mode TMUX_PANE: $source expected $state, got '$actual'"
                expected_style='bg=yellow,fg=black'
                [ "$state" != done ] || expected_style='bg=red,fg=black'
            fi
            source_window=$(tmux_cmd display-message -p -t "$source" '#{window_id}')
            actual_style=$(get_window_option "$source_window" window-status-style)
            [ "$actual_style" = "$expected_style" ] || fail "incorrect source window style: '$actual_style'"
            if [ "$state" = needs-input ]; then
                tmux_cmd select-window -t "$source_window"
                tmux_cmd run-shell "\"$REPO_ROOT/scripts/pane-focus-in.sh\" \"$source\" \"$source_window\""
                [ "$(get_env "TMUX_AGENT_PANE_${source}_STATE")" = needs-input ] || fail "focus must preserve Codex needs-input state"
                [ "$(get_window_option "$source_window" window-status-current-style)" = "$expected_style" ] || fail "focus must preserve Codex needs-input color"
                tmux_cmd select-window -t "$WIN"
            fi
            assert_empty "$(get_env "TMUX_AGENT_PANE_${PANE}_STATE")" "unrelated pane must never receive state"
            assert_empty "$(get_window_option "$WIN" window-status-style)" "unrelated window must never receive styling"
        done
    done
done

# A daemon-style hook descends from the server, not a pane. Even a valid TMUX_PANE is untrusted.
tmux_cmd run-shell "TMUX_PANE=$PANE \"$REPO_ROOT/scripts/agent-state.sh\" --agent codex --state needs-input"
assert_empty "$(get_env "TMUX_AGENT_PANE_${PANE}_STATE")" "daemon must not trust a stale TMUX_PANE"
assert_empty "$(get_window_option "$WIN" window-status-style)" "daemon must not style an unrelated window"

pass "ancestry routes all four states with missing or stale TMUX_PANE and ignores detached Codex hooks"
