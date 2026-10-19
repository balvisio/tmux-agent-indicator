#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"

trap cleanup_test_server EXIT
setup_test_server "cancellation-hooks"
tmux_cmd list-keys -T root | grep -q 'pane-interrupt.sh' || fail "Ctrl+C binding must include the cleanup command"
tmux_cmd set -g @agent-indicator-notification-enabled off
tmux_cmd set -g @agent-indicator-running-window-title-bg yellow
tmux_cmd set -g @agent-indicator-running-window-title-fg black
source_pane=$(tmux_cmd new-window -d -P -F '#{pane_id}' -t ai -n source '/bin/bash --noprofile --norc')
source_window=$(tmux_cmd display-message -p -t "$source_pane" '#{window_id}')
completion=$(mktemp /tmp/agent-cancellation-result.XXXXXX)
register_tmp_file "$completion"

run_in_pane() {
    local command="$1" wrapped result
    : > "$completion"
    # Codex hooks can lack TMUX_PANE; all hooks still originate in the source pane.
    printf -v wrapped 'env -u TMUX_PANE bash -c %q; printf "%%s\\n" "$?" > %q' "$command" "$completion"
    tmux_cmd send-keys -t "$source_pane" -l "$wrapped"
    tmux_cmd send-keys -t "$source_pane" Enter
    for _ in $(seq 1 100); do
        [ -s "$completion" ] && break
        sleep 0.05
    done
    result=$(cat "$completion")
    [ "$result" = 0 ] || fail "hook did not complete successfully: $result"
}

# Keep another agent waiting so cleanup must not touch its state or window.
tmux_cmd set -g @agent-indicator-style-active-window on
run_state needs-input
other_style=$(get_window_option "$WIN" window-status-style)

for agent in claude codex; do
    # Claude executes matching hooks in parallel, so a valid completion order is
    # the reverse of their configuration order. No late reset may erase running.
    prompt_command=$(python3 - "$ROOT_DIR/hooks/$agent-hooks.json" <<'PY'
import json
import sys
with open(sys.argv[1]) as stream:
    entries = json.load(stream)["hooks"]["UserPromptSubmit"]
commands = [hook["command"] for entry in entries for hook in entry["hooks"]]
print("\n".join(reversed(commands)))
PY
    )
    [ "$agent" != claude ] || prompt_command="export TMUX_PANE=$source_pane; $prompt_command"
    for state in off running needs-input done; do
        run_in_pane "TMUX_PANE=$source_pane \"$ROOT_DIR/scripts/agent-state.sh\" --agent $agent --state $state"
        run_in_pane "$prompt_command"
        [ "$(get_env "TMUX_AGENT_PANE_${source_pane}_STATE")" = running ] || fail "$agent prompt hooks must leave the pane running from $state, regardless of execution order"
        [ "$(get_window_option "$source_window" window-status-style)" = bg=yellow,fg=black ] || fail "$agent prompt must show running styling"
        [ "$(get_env "TMUX_AGENT_PANE_${PANE}_STATE")" = needs-input ] || fail "prompt must preserve another agent's waiting state"
    done
    events=SessionEnd
    [ "$agent" != codex ] || events="Interrupt SessionEnd"
    for event in $events; do
        command=$(python3 - "$ROOT_DIR/hooks/$agent-hooks.json" "$event" <<'PY'
import json
import sys
with open(sys.argv[1]) as stream:
    hooks = json.load(stream)["hooks"]
print(hooks[sys.argv[2]][0]["hooks"][0]["command"])
PY
        )
        # Claude supplies its pane explicitly; Codex uses process ancestry.
        [ "$agent" != claude ] || command="export TMUX_PANE=$source_pane; $command"
        for state in running needs-input; do
            run_in_pane "TMUX_PANE=$source_pane \"$ROOT_DIR/scripts/agent-state.sh\" --agent $agent --state $state"
            [ "$(get_env "TMUX_AGENT_PANE_${source_pane}_STATE")" = "$state" ] || fail "missing initial $state state"
            assert_non_empty "$(get_window_option "$source_window" window-status-style)" "initial state must be styled"
            run_in_pane "$command"
            assert_empty "$(get_env "TMUX_AGENT_PANE_${source_pane}_STATE")" "$agent $event must clear $state"
            assert_empty "$(get_env "TMUX_AGENT_PANE_${source_pane}_AGENT")" "$agent $event must clear agent marker"
            assert_empty "$(get_window_option "$source_window" window-status-style)" "$agent $event must clear title styling"
            assert_empty "$(get_window_option "$source_window" window-status-current-style)" "$agent $event must clear active title styling"
            [ "$(tmux_cmd show-options -pv -p -t "$source_pane" window-style)" = bg=default ] || fail "pane background must reset"
            [ "$(get_env "TMUX_AGENT_PANE_${PANE}_STATE")" = needs-input ] || fail "other agent state must remain"
            [ "$(get_window_option "$WIN" window-status-style)" = "$other_style" ] || fail "other window style must remain"
        done
    done
done

# The tmux fallback handles Claude only and must preserve custom Ctrl+C bindings.
for state in running needs-input; do
    run_in_pane "TMUX_PANE=$source_pane \"$ROOT_DIR/scripts/agent-state.sh\" --agent claude --state $state"
    tmux_cmd run-shell "\"$ROOT_DIR/scripts/pane-interrupt.sh\" \"$source_pane\""
    assert_empty "$(get_env "TMUX_AGENT_PANE_${source_pane}_STATE")" "Claude Ctrl+C fallback must clear $state"
done
run_in_pane "\"$ROOT_DIR/scripts/agent-state.sh\" --agent codex --state running"
tmux_cmd run-shell "\"$ROOT_DIR/scripts/pane-interrupt.sh\" \"$source_pane\""
[ "$(get_env "TMUX_AGENT_PANE_${source_pane}_STATE")" = running ] || fail "Codex must use its native interruption hook"
tmux_cmd bind-key -n C-c display-message custom-interrupt
tmux_cmd run-shell "$ROOT_DIR/agent-indicator.tmux"
tmux_cmd list-keys -T root | grep -q custom-interrupt || fail "custom Ctrl+C binding must survive reload"

pass "prompt hooks preserve running state; cancellation hooks clear only their source pane"
