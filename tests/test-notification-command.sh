#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"

trap cleanup_test_server EXIT

setup_test_server "notification-command"

log_file="/tmp/tmux-agent-indicator-${SOCK}-notify.log"
register_tmp_file "$log_file"

tmux_cmd set -g @agent-indicator-notification-message off
tmux_cmd set -g @agent-indicator-notification-command "echo \"\$AGENT_NAME \$AGENT_STATE\" >> \"$log_file\""

log_lines() {
    if [ -f "$log_file" ]; then
        wc -l < "$log_file" | tr -d ' '
    else
        echo 0
    fi
}

wait_for_log_lines() {
    local expected="$1"
    for _ in $(seq 1 20); do
        if [ "$(log_lines)" = "$expected" ]; then
            return 0
        fi
        sleep 0.05
    done
    return 1
}

# The command runs in the background even though the on-screen message is off.
run_state needs-input
wait_for_log_lines 1 || fail "notification command should run with the message disabled"
entry="$(cat "$log_file")"
[ "$entry" = "claude needs-input" ] || fail "notification command should receive agent and state: $entry"

# running is not a notification state by default.
run_state running
sleep 0.2
[ "$(log_lines)" = "1" ] || fail "running should not notify by default"

# Disabling notifications suppresses the command as well.
tmux_cmd set -g @agent-indicator-notification-enabled off
run_state "done"
sleep 0.2
[ "$(log_lines)" = "1" ] || fail "command should not run when notifications are disabled"

pass "notification command runs without the on-screen message and respects the enabled toggle"
