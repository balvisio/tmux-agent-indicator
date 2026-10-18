#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"

trap cleanup_test_server EXIT

setup_test_server "window-title-reset"
create_other_window
tmux_cmd select-window -t ai:other
tmux_cmd set-option -g @agent-indicator-needs-input-bg colour94

run_state needs-input
before_style="$(get_window_option "$WIN" "window-status-style")"
before_current_style="$(get_window_option "$WIN" "window-status-current-style")"
assert_non_empty "$before_style" "window-status-style should be set in needs-input"
assert_non_empty "$before_current_style" "window-status-current-style should be set in needs-input"

sleep 0.1

while_away_style="$(get_window_option "$WIN" "window-status-style")"
while_away_current_style="$(get_window_option "$WIN" "window-status-current-style")"
assert_non_empty "$while_away_style" "window-status-style should stay set while away from source window"
assert_non_empty "$while_away_current_style" "window-status-current-style should stay set while away from source window"

tmux_cmd select-window -t ai:main
# Wait for an explicit focus handler so the assertion cannot race an asynchronous hook.
tmux_cmd run-shell "$ROOT_DIR/scripts/pane-focus-in.sh \"$PANE\" \"$WIN\""

after_return_style="$(get_window_option "$WIN" "window-status-style")"
after_return_current_style="$(get_window_option "$WIN" "window-status-current-style")"
[ "$after_return_style" = "$before_style" ] || fail "needs-input title should remain after focus: $after_return_style"
[ "$after_return_current_style" = "$before_current_style" ] || fail "needs-input active title should remain after focus: $after_return_current_style"
[ "$(get_env "TMUX_AGENT_PANE_${PANE}_STATE")" = needs-input ] || fail "focus must not acknowledge unanswered input"
[ "$(tmux_cmd show-options -pv -p -t "$PANE" window-style)" = bg=colour94 ] || fail "needs-input pane background should remain after focus"
[ "$(get_window_option "$WIN" pane-active-border-style)" = fg=yellow,bold ] || fail "needs-input border should remain after focus"

# The PostToolUse transition, rather than focus, clears the waiting title.
tmux_cmd run-shell "TMUX_PANE=$PANE \"$ROOT_DIR/scripts/agent-state.sh\" --agent claude --state running --if-state needs-input"
[ "$(get_env "TMUX_AGENT_PANE_${PANE}_STATE")" = running ] || fail "PostToolUse must transition out of needs-input"
assert_empty "$(get_window_option "$WIN" window-status-style)" "waiting title should reset after the agent resumes"
assert_empty "$(get_window_option "$WIN" window-status-current-style)" "waiting active title should reset after the agent resumes"

pass "needs-input visuals survive focus until the agent resumes"
