#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"

trap cleanup_test_server EXIT

setup_test_server "focus-reset-window-change"
create_other_window
tmux_cmd select-window -t ai:other

run_state "done"
before_style="$(get_window_option "$WIN" "window-status-style")"
assert_non_empty "$before_style" "window-status-style should be set for done in a non-active window"

# Drop the select-window / select-pane hooks so only session-window-changed can reset.
tmux_cmd set-hook -gu after-select-window
tmux_cmd set-hook -gu after-select-pane

# Window switches that bypass select-window (status-bar mouse clicks run switch-client;
# next-/previous-/last-window have no after-* hooks) only fire session-window-changed.
tmux_cmd last-window
sleep 0.2

after_style="$(get_window_option "$WIN" "window-status-style")"
after_state="$(get_env "TMUX_AGENT_PANE_${PANE}_STATE")"
assert_empty "$after_style" "window-status-style should reset after returning via session-window-changed"
assert_empty "$after_state" "done state env should reset after returning via session-window-changed"

pass "session-window-changed resets done styling when the switch bypasses select-window"
