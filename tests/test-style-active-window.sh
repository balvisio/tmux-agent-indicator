#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"

trap cleanup_test_server EXIT

setup_test_server "style-active-window"

# Default: the window the user is looking at keeps its title style.
run_state needs-input
default_style="$(get_window_option "$WIN" "window-status-style")"
assert_empty "$default_style" "active window title should stay unstyled by default"
run_state off

tmux_cmd set -g @agent-indicator-style-active-window on

run_state needs-input
needs_input_style="$(get_window_option "$WIN" "window-status-style")"
[ "$needs_input_style" = "bg=yellow,fg=black" ] || fail "active window title should be styled for needs-input: $needs_input_style"

run_state "done"
done_style="$(get_window_option "$WIN" "window-status-style")"
[ "$done_style" = "bg=red,fg=black" ] || fail "active window title should be styled for done: $done_style"

# Focusing the completed window clears its done styling.
tmux_cmd run-shell "$ROOT_DIR/scripts/pane-focus-in.sh \"$PANE\" \"$WIN\""
after_focus_style="$(get_window_option "$WIN" "window-status-style")"
after_focus_state="$(get_env "TMUX_AGENT_PANE_${PANE}_STATE")"
assert_empty "$after_focus_style" "active window title should reset after done focus-in"
assert_empty "$after_focus_state" "done state env should reset after done focus-in"

pass "style-active-window colours the active window title and still resets on focus"
