#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"

trap cleanup_test_server EXIT

setup_test_server "if-state"

run_state_if() {
    local state="$1" if_state="$2"
    tmux_cmd run-shell "TMUX_PANE=$PANE \"$REPO_ROOT/scripts/agent-state.sh\" --agent claude --state $state --if-state $if_state"
}

state_key="TMUX_AGENT_PANE_${PANE}_STATE"

# A pane without state counts as "off".
run_state_if running needs-input
assert_empty "$(get_env "$state_key")" "running --if-state needs-input must not act on a pane without state"
run_state_if running off
[ "$(get_env "$state_key")" = "running" ] || fail "running --if-state off should act on a pane without state: $(get_env "$state_key")"

# The PostToolUse hook case: needs-input goes back to running once a tool call completes...
run_state needs-input
[ "$(get_env "$state_key")" = "needs-input" ] || fail "precondition: pane should be needs-input"
run_state_if running needs-input
[ "$(get_env "$state_key")" = "running" ] || fail "running --if-state needs-input should act on a needs-input pane: $(get_env "$state_key")"

# ...and later tool calls leave the running pane alone.
run_state_if done needs-input
[ "$(get_env "$state_key")" = "running" ] || fail "done --if-state needs-input must not act on a running pane: $(get_env "$state_key")"

# A done pane is owned by the deferred focus reset, not by the hook.
run_state "done"
run_state_if running needs-input
[ "$(get_env "$state_key")" = "done" ] || fail "running --if-state needs-input must not act on a done pane: $(get_env "$state_key")"

# An unknown value is rejected like an unknown --state (the trailing true keeps tmux from reporting it).
tmux_cmd run-shell "TMUX_PANE=$PANE \"$REPO_ROOT/scripts/agent-state.sh\" --agent claude --state running --if-state bogus; true"
[ "$(get_env "$state_key")" = "done" ] || fail "an invalid --if-state must change nothing: $(get_env "$state_key")"

pass "--if-state only acts when the pane is in the given state"
