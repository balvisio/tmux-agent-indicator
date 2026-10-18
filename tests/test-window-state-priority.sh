#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"
trap cleanup_test_server EXIT
setup_test_server window-priority
create_other_window
tmux_cmd select-window -t "$OTHER_WIN"
second=$(tmux_cmd split-window -d -P -F '#{pane_id}' -t "$PANE" '/bin/bash --noprofile --norc')
tmux_cmd set -g @agent-indicator-notification-enabled off
tmux_cmd set -g @agent-indicator-style-active-window on
tmux_cmd set -g @agent-indicator-needs-input-window-title-bg magenta
tmux_cmd set -g @agent-indicator-running-window-title-bg yellow
tmux_cmd set -g @agent-indicator-running-window-title-fg black
tmux_cmd set -g @agent-indicator-running-border yellow
tmux_cmd set -g @agent-indicator-needs-input-border magenta
tmux_cmd set -g @agent-indicator-done-border red
tmux_cmd set-window-option -t "$WIN" window-status-style bg=blue,fg=white
tmux_cmd set-window-option -t "$WIN" window-status-current-style bg=blue,fg=white
tmux_cmd set-window-option -t "$WIN" pane-active-border-style fg=white

set_state() {
    tmux_cmd run-shell "TMUX_PANE=$1 \"$ROOT_DIR/scripts/agent-state.sh\" --agent claude --state $2"
}
assert_window() {
    local expected="$1" bg border actual
    case "$expected" in
        needs-input) bg=magenta; border=fg=magenta,bold ;;
        running) bg=yellow; border=fg=yellow,bold ;;
        done) bg=red; border=fg=red,bold ;;
        idle) bg=blue; border=fg=white ;;
    esac
    actual=$(get_window_option "$WIN" @agent-indicator-state)
    [ "$actual" = "$expected" ] || fail "window state: expected $expected, got $actual"
    for option in window-status-style window-status-current-style; do
        actual=$(get_window_option "$WIN" "$option")
        [[ "$actual" == "bg=$bg,"* ]] || fail "$option: expected $bg, got $actual"
    done
    [ "$(get_window_option "$WIN" pane-active-border-style)" = "$border" ] || fail "border must follow $expected"
}

# All ordered state pairs, in both arrival orders. Idle + idle is the only blue pair.
for first_state in off done running needs-input; do
    for second_state in off done running needs-input; do
        case "$first_state/$second_state" in
            *needs-input*) expected=needs-input ;;
            *running*) expected=running ;;
            *done*) expected=done ;;
            *) expected=idle ;;
        esac
        for order in forward reverse; do
            set_state "$PANE" off
            set_state "$second" off
            if [ "$order" = forward ]; then
                set_state "$PANE" "$first_state"
                set_state "$second" "$second_state"
            else
                set_state "$second" "$second_state"
                set_state "$PANE" "$first_state"
            fi
            assert_window "$expected"
        done
    done
done
pass "all state pairs agree with window priority in both arrival orders"

# Focusing A must acknowledge completed B without erasing waiting/running A.
for active_state in needs-input running; do
    tmux_cmd select-window -t "$OTHER_WIN"
    set_state "$PANE" "$active_state"
    set_state "$second" done
    tmux_cmd select-window -t "$WIN"
    tmux_cmd select-pane -t "$PANE"
    tmux_cmd run-shell "\"$ROOT_DIR/scripts/pane-focus-in.sh\" \"$PANE\" \"$WIN\""
    [ "$(get_env "TMUX_AGENT_PANE_${PANE}_STATE")" = "$active_state" ] || fail "focus erased $active_state"
    assert_empty "$(get_env "TMUX_AGENT_PANE_${second}_STATE")" "focus must acknowledge completed sibling"
    assert_window "$active_state"
done
# A stale focus event from a window we left must not acknowledge anything.
tmux_cmd select-window -t "$OTHER_WIN"
set_state "$second" done
tmux_cmd run-shell "\"$ROOT_DIR/scripts/pane-focus-in.sh\" \"$PANE\" \"$WIN\""
[ "$(get_env "TMUX_AGENT_PANE_${second}_STATE")" = done ] || fail "stale focus event acknowledged completion"
pass "focus acknowledges done siblings and preserves waiting/running agents"

# Three agents: clearing a running pane reveals done only after the waiting pane clears.
third=$(tmux_cmd split-window -d -P -F '#{pane_id}' -t "$PANE" '/bin/bash --noprofile --norc')
set_state "$PANE" needs-input
set_state "$second" running
set_state "$third" done
assert_window needs-input
set_state "$second" off
assert_window needs-input
set_state "$PANE" off
assert_window done
set_state "$third" off
assert_window idle

# Simultaneous hooks and a presence sweep must not allow done/blue to hide needs-input.
for _ in 1 2 3; do
    tmux_cmd run-shell "TMUX_PANE=$PANE \"$ROOT_DIR/scripts/agent-state.sh\" --agent claude --state needs-input & TMUX_PANE=$second \"$ROOT_DIR/scripts/agent-state.sh\" --agent claude --state done & \"$ROOT_DIR/scripts/window-state.sh\" --idle-style $WIN bg=blue,fg=white & wait"
    assert_window needs-input
done
# Reconciliation repairs a stale title without changing the stored waiting state.
tmux_cmd set-window-option -t "$WIN" window-status-style bg=blue,fg=white
tmux_cmd run-shell "\"$ROOT_DIR/scripts/window-state.sh\" --idle-style $WIN bg=blue,fg=white"
assert_window needs-input
pass "three agents, concurrent hooks, and idle sweeps preserve priority"

# Moving a waiting pane must update both windows; closing it must remove its contribution.
set_state "$second" off
tmux_cmd join-pane -d -s "$PANE" -t "$OTHER_PANE"
for _ in $(seq 1 80); do
    [ "$(get_window_option "$WIN" @agent-indicator-state)" = idle ] &&
    [ "$(get_window_option "$OTHER_WIN" @agent-indicator-state)" = needs-input ] && break
    sleep 0.05
done
assert_window idle
[ "$(get_window_option "$OTHER_WIN" @agent-indicator-state)" = needs-input ] || fail "moved pane did not color its new window"
tmux_cmd kill-pane -t "$PANE"
for _ in $(seq 1 80); do
    [ "$(get_window_option "$OTHER_WIN" @agent-indicator-state)" = idle ] && break
    sleep 0.05
done
[ "$(get_window_option "$OTHER_WIN" @agent-indicator-state)" = idle ] || fail "closed pane left a stale window state"
pass "moving and closing panes reconciles source and destination windows"
