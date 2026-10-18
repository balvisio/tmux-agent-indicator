#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

tests=(
    "$ROOT_DIR/tests/test-agent-limits.sh"
    "$ROOT_DIR/tests/test-hammerspoon-dots.py"
    "$ROOT_DIR/tests/test-state-lock.py"
    "$ROOT_DIR/tests/test-state-transitions.sh"
    "$ROOT_DIR/tests/test-indicator-output.sh"
    "$ROOT_DIR/tests/test-pane-presence.py"
    "$ROOT_DIR/tests/test-pane-style-preserves-focus.sh"
    "$ROOT_DIR/tests/test-focus-reset-done.sh"
    "$ROOT_DIR/tests/test-window-title-reset.sh"
    "$ROOT_DIR/tests/test-window-state-priority.sh"
    "$ROOT_DIR/tests/test-window-state-edge-cases.sh"
    "$ROOT_DIR/tests/test-focus-reset-window-change.sh"
    "$ROOT_DIR/tests/test-style-active-window.sh"
    "$ROOT_DIR/tests/test-if-state.sh"
    "$ROOT_DIR/tests/test-resolve-by-process.sh"
    "$ROOT_DIR/tests/test-resolve-ancestry.sh"
    "$ROOT_DIR/tests/test-cancellation-hooks.sh"
    "$ROOT_DIR/tests/test-notification-command.sh"
    "$ROOT_DIR/tests/test-running-animation.sh"
)

for test_script in "${tests[@]}"; do
    echo "==> $(basename "$test_script")"
    "$test_script"
done

echo "PASS: all automated tests"
