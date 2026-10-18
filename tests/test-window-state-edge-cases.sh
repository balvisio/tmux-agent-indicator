#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"
trap cleanup_test_server EXIT
setup_test_server aggregate-edges
create_other_window
tmux_cmd select-window -t "$OTHER_WIN"
second=$(tmux_cmd split-window -d -P -F '#{pane_id}' -t "$PANE" '/bin/bash --noprofile --norc')
tmux_cmd set -g @agent-indicator-notification-enabled off
tmux_cmd set -g @agent-indicator-style-active-window on
tmux_cmd set -g @agent-indicator-needs-input-bg ''
tmux_cmd set -g @agent-indicator-needs-input-border ''
tmux_cmd set -g @agent-indicator-needs-input-window-title-bg ''
tmux_cmd set -g @agent-indicator-needs-input-window-title-fg ''
tmux_cmd set-option -p -t "$PANE" window-style bg=colour94
tmux_cmd set-window-option -t "$WIN" pane-active-border-style fg=white
tmux_cmd set-window-option -t "$WIN" window-status-style bg=blue,fg=white
run_state needs-input
[ "$(tmux_cmd show-options -pv -t "$PANE" window-style)" = bg=colour94 ] || fail 'empty background option changed pane'
[ "$(get_window_option "$WIN" pane-active-border-style)" = fg=white ] || fail 'empty border option changed border'
[ "$(get_window_option "$WIN" window-status-style)" = bg=blue,fg=white ] || fail 'empty title option changed title'
run_state off
pass 'empty styling options preserve the original properties'
tmux_cmd set -g @agent-indicator-animation-enabled on
run_state running
pid=$(wait_for_non_empty_env TMUX_AGENT_ANIMATION_PID)
tmux_cmd run-shell "TMUX_PANE=$second \"$REPO_ROOT/scripts/agent-state.sh\" --agent claude --state needs-input"
[ "$(get_env TMUX_AGENT_ANIMATION_PID)" = "$pid" ] || fail 'waiting sibling stopped running animation'
tmux_cmd run-shell "TMUX_PANE=$second \"$REPO_ROOT/scripts/agent-state.sh\" --agent claude --state off"
[ "$(get_env TMUX_AGENT_ANIMATION_PID)" = "$pid" ] || fail 'canceled sibling stopped running animation'
run_state off
assert_empty "$(get_env TMUX_AGENT_ANIMATION_PID)" 'last running pane stopped but animation remained'
pass 'animation survives another agent waiting or canceling'
# A retained dead pane must no longer contribute its waiting state.
tmux_cmd set-window-option -t "$WIN" remain-on-exit on
tmux_cmd run-shell "TMUX_PANE=$second \"$REPO_ROOT/scripts/agent-state.sh\" --agent claude --state needs-input"
tmux_cmd send-keys -t "$second" 'exit' Enter
for _ in $(seq 1 100); do
    [ "$(get_window_option "$WIN" @agent-indicator-state)" = idle ] && break
    sleep 0.05
done
[ "$(get_window_option "$WIN" @agent-indicator-state)" = idle ] || fail 'retained dead pane still contributes a state'
pass 'pane exit removes its contribution even with remain-on-exit'
tmux_cmd run-shell "\"$REPO_ROOT/scripts/reset_all.sh\""
[ "$(get_window_option "$WIN" @agent-indicator-state)" = idle ] || fail 'reset did not publish idle'
pass 'reset clears the published aggregate state'
