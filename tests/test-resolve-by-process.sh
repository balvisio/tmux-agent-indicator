#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"

trap cleanup_test_server EXIT

setup_test_server "resolve-by-process"

state_key="TMUX_AGENT_PANE_${PANE}_STATE"

# A Codex hook launched outside a pane's process tree cannot be placed.
# Nothing should be tagged or styled, including the active pane.
tmux_cmd run-shell "env -u TMUX_PANE \"$REPO_ROOT/scripts/agent-state.sh\" --agent codex --state needs-input"
assert_empty "$(get_env "$state_key")" "no pane should be tagged when codex has no pane ancestor"
assert_empty "$(get_window_option "$WIN" "window-status-style")" "no window should be styled when the pane is unknown"

# With TMUX_PANE present (as Claude provides), the same call still resolves normally.
run_state needs-input
[ "$(get_env "$state_key")" = "needs-input" ] || fail "an explicit TMUX_PANE must still resolve: $(get_env "$state_key")"
run_state off

pass "an unresolvable codex hook leaves every window untouched; TMUX_PANE still works"
