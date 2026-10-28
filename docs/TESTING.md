# Testing Guide

This guide defines repeatable checks for `tmux-agent-indicator` in automated and manual modes.

## Prerequisites

- Run from repository root.
- tmux 3.6 or newer is installed.
- Plugin loaded with `run-shell '/path/to/agent-indicator.tmux'` (or TPM).

## Automated Mode (Isolated tmux server)

Preferred: run executable test scripts from `tests/`.

Run all checks:

```bash
./tests/run-all.sh
```

Run individual checks:

```bash
./tests/test-agent-limits.sh
./tests/test-hammerspoon-dots.py
./tests/test-state-lock.py
./tests/test-state-transitions.sh
./tests/test-indicator-output.sh
./tests/test-pane-presence.py
./tests/test-focus-reset-done.sh
./tests/test-window-title-reset.sh
./tests/test-window-state-priority.sh
./tests/test-window-state-edge-cases.sh
./tests/test-focus-reset-window-change.sh
./tests/test-style-active-window.sh
./tests/test-if-state.sh
./tests/test-resolve-by-process.sh
./tests/test-resolve-ancestry.sh
./tests/test-cancellation-hooks.sh
./tests/test-notification-command.sh
./tests/test-running-animation.sh
./tests/test-ssh-reconnect.sh
./tests/test-vpn-guard.py
./tests/test-install-startup-lines.sh
./tests/test-ssh-passthrough.sh
./tests/test-ssh-mouse.py
./tests/test-agent-recovery.py
```

The recovery test restores two Kibitz pairs with new pane IDs, including a reviewer started before
its host. It checks that each reviewer keeps its role and links to its own host conversation.

The recovery test downloads the pinned Resurrect and Continuum dependencies into a temporary HOME.
It uses fake agent CLIs on an isolated tmux socket, kills that server, and verifies automatic restart
recovery of separate conversation IDs, splits, working directories and SSH launchers. It also checks
installer idempotence, failed saves, exited agents and uninstall ownership. Two additional disposable
servers remain running throughout the test. Slow restoration cannot overwrite the saved layout, and
reloads respect the startup decision and restore settings. It requires process-list access; run outside
a process-restricting sandbox. It never kills the user's tmux server.
An initial config load delayed by twelve seconds checks restoration after a slow login. Installation
into a running server is checked separately to ensure it never starts an unsolicited restoration.
It also checks lookup across hostname changes, error logs, the rendered failure warning, quiet
background saves, nonzero manual failures, and warning removal after an unchanged successful save.
Real terminal clients check that restoration preserves their sessions with an empty or conflicting
saved session selection.

The VPN guard tests use temporary configuration and mocked GlobalProtect state; they never restart
the real VPN. A real autossh process with a fake SSH executable checks the initial wait, later retries,
normal exit and termination during a wait. State-machine tests cover interface readiness, the timeout,
manual disconnects, login waits, restart failures, cooldown and locking across destinations.
Internal-network checks cover tunnel-free access, discovery restarts, office-to-home transitions,
wake, service restarts, log rotation, stale results and the repeated DST hour. A loopback TCP
listener verifies the destination fallback without sending application data; mocked failures check
timeouts, per-destination caching, proxy handling, network changes during probes and lock isolation.
Fixtures use synthetic provider events and network settings; no corporate configuration or captured
service logs are included.

The ancestry test launches hooks inside two separate panes while an unrelated window stays selected.
It checks all four states with both a missing `TMUX_PANE` and a stale value naming the unrelated pane.
The originating windows must receive the updates and the unrelated window must remain unchanged.
It also checks that detached Codex hooks are ignored despite a valid, stale `TMUX_PANE`.

The passthrough test checks keyboard-setting restoration, unchanged agent colours across window
changes, plain SSH exit, reconnect retries and exit-triggered sweeps, and stale or closed owner panes.
The mouse test sends terminal mouse events through two isolated, nested tmux servers. It checks
local and remote status-bar clicks, split-pane clicks, manual toggles and stale selections.
For a visual check, enable the README's F12 bindings in an isolated test session: the bar should turn
orange while agent titles keep their colours, then return to its original style when passthrough ends.

For live Codex checks, launch two fresh `codex --no-daemon` sessions in separate panes.
Check `running`, `needs-input`, and `done` while a third window is selected: only the
originating pane/window should change. Focus a pending permission prompt and confirm its
`needs-input` color remains until the agent reports its next state.

Cancel an active turn with Ctrl+C in both agents and confirm its state and colors clear.
Codex requires the `Interrupt` hook to be trusted in `/hooks`; Claude uses the plugin's
Ctrl+C binding. Exiting either agent must also clear state through `SessionEnd`.

Check live provider limits without the tmux cache:

```bash
python3 scripts/agent-limits.py --no-cache
```

The output contains only unexpired provider windows. Example: `C 5h 4% used · X 7d 2% used`.

Manual tmux-socket commands (below) are still useful for debugging.

Use a dedicated socket so tests do not affect your normal tmux session:

```bash
SOCK=agent-test-$$
tmux -L "$SOCK" -f /dev/null new-session -d -s ai -n main
tmux -L "$SOCK" set -g status-right '#{agent_indicator} | %H:%M'
tmux -L "$SOCK" run-shell "$PWD/agent-indicator.tmux"
PANE="$(tmux -L "$SOCK" display-message -p -t ai:main.0 '#{pane_id}')"
WIN="$(tmux -L "$SOCK" display-message -p -t ai:main.0 '#{window_id}')"
```

Trigger each state on the same pane:

```bash
tmux -L "$SOCK" run-shell "TMUX_PANE=$PANE \"$PWD/scripts/agent-state.sh\" --agent claude --state running"
tmux -L "$SOCK" run-shell "TMUX_PANE=$PANE \"$PWD/scripts/agent-state.sh\" --agent claude --state needs-input"
tmux -L "$SOCK" run-shell "TMUX_PANE=$PANE \"$PWD/scripts/agent-state.sh\" --agent claude --state done"
tmux -L "$SOCK" run-shell "TMUX_PANE=$PANE \"$PWD/scripts/agent-state.sh\" --agent claude --state off"
```

Core assertions:

```bash
tmux -L "$SOCK" show-window-options -vt "$WIN" | grep -E 'pane-active-border-style|window-status'
tmux -L "$SOCK" run-shell "TMUX_PANE=$PANE \"$PWD/scripts/indicator.sh\" > /tmp/agent-indicator.out"
cat /tmp/agent-indicator.out
```

Focus-reset check:

```bash
tmux -L "$SOCK" run-shell "TMUX_PANE=$PANE \"$PWD/scripts/agent-state.sh\" --agent claude --state done"
tmux -L "$SOCK" run-shell "$PWD/scripts/pane-focus-in.sh \"$PANE\" \"$WIN\""
tmux -L "$SOCK" show-window-options -vt "$WIN" | grep 'window-status'
```

Done window-switch title reset check:

```bash
tmux -L "$SOCK" new-window -d -t ai -n other
OTHER_PANE="$(tmux -L "$SOCK" display-message -p -t ai:other.0 '#{pane_id}')"
OTHER_WIN="$(tmux -L "$SOCK" display-message -p -t ai:other.0 '#{window_id}')"
tmux -L "$SOCK" select-window -t "$OTHER_WIN"
tmux -L "$SOCK" run-shell "TMUX_PANE=$PANE \"$PWD/scripts/agent-state.sh\" --agent claude --state done"
tmux -L "$SOCK" run-shell "$PWD/scripts/pane-focus-in.sh \"$OTHER_PANE\" \"$OTHER_WIN\""
tmux -L "$SOCK" show-window-options -vt "$WIN" | grep 'window-status'
```

Done window-switch title reset assertion (fails if style clears before returning to source window, or does not clear after returning):

```bash
set -euo pipefail
tmux -L "$SOCK" select-window -t "$OTHER_WIN"
tmux -L "$SOCK" run-shell "TMUX_PANE=$PANE \"$PWD/scripts/agent-state.sh\" --agent claude --state done"
before_style="$(tmux -L "$SOCK" show-window-option -v -t "$WIN" window-status-style 2>/dev/null || true)"
before_current_style="$(tmux -L "$SOCK" show-window-option -v -t "$WIN" window-status-current-style 2>/dev/null || true)"
[ -n "$before_style" ] || { echo "FAIL: window-status-style was not applied before switch"; exit 1; }
[ -n "$before_current_style" ] || { echo "FAIL: window-status-current-style was not applied before switch"; exit 1; }

tmux -L "$SOCK" run-shell "$PWD/scripts/pane-focus-in.sh \"$OTHER_PANE\" \"$OTHER_WIN\""
while_away_style="$(tmux -L "$SOCK" show-window-option -v -t "$WIN" window-status-style 2>/dev/null || true)"
while_away_current_style="$(tmux -L "$SOCK" show-window-option -v -t "$WIN" window-status-current-style 2>/dev/null || true)"
[ -n "$while_away_style" ] || { echo "FAIL: window-status-style cleared before returning to source window"; exit 1; }
[ -n "$while_away_current_style" ] || { echo "FAIL: window-status-current-style cleared before returning to source window"; exit 1; }

tmux -L "$SOCK" select-window -t "$WIN"
tmux -L "$SOCK" run-shell "$PWD/scripts/pane-focus-in.sh \"$PANE\" \"$WIN\""
after_return_style="$(tmux -L "$SOCK" show-window-option -v -t "$WIN" window-status-style 2>/dev/null || true)"
after_return_current_style="$(tmux -L "$SOCK" show-window-option -v -t "$WIN" window-status-current-style 2>/dev/null || true)"

[ -z "$after_return_style" ] || { echo "FAIL: stale window-status-style remains after returning: $after_return_style"; exit 1; }
[ -z "$after_return_current_style" ] || { echo "FAIL: stale window-status-current-style remains after returning: $after_return_current_style"; exit 1; }
echo "PASS: window title styles reset only when returning to source window"
```

Running animation assertion (fails if animation does not tick or cleanup):

```bash
set -euo pipefail
tmux -L "$SOCK" set -g @agent-indicator-animation-enabled 'on'
tmux -L "$SOCK" set -g @agent-indicator-animation-speed '80'
tmux -L "$SOCK" run-shell "TMUX_PANE=$PANE \"$PWD/scripts/agent-state.sh\" --agent claude --state running"

anim_pid=""
for _ in $(seq 1 20); do
  anim_pid="$(tmux -L "$SOCK" show-environment -g TMUX_AGENT_ANIMATION_PID 2>/dev/null | sed 's/^[^=]*=//' || true)"
  [ -n "$anim_pid" ] && break
  sleep 0.05
done
[ -n "$anim_pid" ] || { echo "FAIL: animation PID was not created"; exit 1; }

first_frame="$(tmux -L "$SOCK" show-environment -g TMUX_AGENT_ANIMATION_FRAME 2>/dev/null | sed 's/^[^=]*=//' || true)"
[ -n "$first_frame" ] || { echo "FAIL: first animation frame is empty"; exit 1; }

tmux -L "$SOCK" run-shell "TMUX_PANE=$PANE \"$PWD/scripts/indicator.sh\" > /tmp/agent-indicator.out"
indicator_output="$(cat /tmp/agent-indicator.out)"
printf '%s' "$indicator_output" | grep -q '━' || { echo "FAIL: indicator bar is missing while running"; exit 1; }

changed=0
for _ in $(seq 1 20); do
  sleep 0.1
  next_frame="$(tmux -L "$SOCK" show-environment -g TMUX_AGENT_ANIMATION_FRAME 2>/dev/null | sed 's/^[^=]*=//' || true)"
  if [ -n "$next_frame" ] && [ "$next_frame" != "$first_frame" ]; then
    changed=1
    break
  fi
done
[ "$changed" -eq 1 ] || { echo "FAIL: animation frame did not change"; exit 1; }

tmux -L "$SOCK" run-shell "TMUX_PANE=$PANE \"$PWD/scripts/agent-state.sh\" --agent claude --state done"
sleep 0.2

anim_pid_after="$(tmux -L "$SOCK" show-environment -g TMUX_AGENT_ANIMATION_PID 2>/dev/null | sed 's/^[^=]*=//' || true)"
frame_after="$(tmux -L "$SOCK" show-environment -g TMUX_AGENT_ANIMATION_FRAME 2>/dev/null | sed 's/^[^=]*=//' || true)"
[ -z "$anim_pid_after" ] || { echo "FAIL: animation PID still present after done"; exit 1; }
[ -z "$frame_after" ] || { echo "FAIL: animation frame still present after done"; exit 1; }
echo "PASS: running animation ticks and cleans up"
```

Cleanup:

```bash
tmux -L "$SOCK" kill-server
rm -f /tmp/agent-indicator.out
```

## Manual Mode (UX validation)

1. Open tmux with two windows (`test`, `tmux-agent-indicator`) and at least two panes in the target window.
2. In one pane, run state transitions:
   `scripts/agent-state.sh --agent claude --state running|needs-input|done|off`.
3. Confirm behavior:
   - `running/needs-input/done` apply only configured non-empty properties.
   - `off` resets only the originating pane; window colors reflect the remaining agents.
   - With `@agent-indicator-animation-enabled on`, running state animates the status indicator.
   - `needs-input` title, pane, and border styling survives focus until the agent reports its next state.
   - Focusing the window acknowledges all its `done` panes, preserving `running` and `needs-input`.
   - With `@agent-indicator-reset-on-focus on`, all done pane backgrounds clear when the window is focused.
4. Validate empty-value semantics (`set -g @agent-indicator-done-bg ''` should skip background changes).
5. Validate status icon appears when agent state/process is active.

Optional screenshot capture:

```bash
screencapture -x /tmp/tmux-agent-indicator-check.png
```

## Shared-window state and menu-bar integration

`test-window-state-priority.sh` covers all 16 pairs of idle/done/running/needs-input in both hook
arrival orders, three-agent windows, concurrent hooks and idle sweeps, completion acknowledgment,
original-style restoration, stale focus callbacks, and moving/closing panes. The expected priority
is `needs-input > running > done > idle`; the published `@agent-indicator-state`, normal title,
current title, and border must agree. No waiting/running pane may be cleared by another pane's
completion or cancellation.

For a menu-bar consumer, compare its displayed state with:

```bash
tmux list-windows -a -F '#{window_index}: #{@agent-indicator-state} | #{window-status-style} | #{window-status-current-style}'
```

With active-window styling enabled and matching configured colors, any discrepancy should clear
within the consumer's polling interval. Consumers must read the published window state rather than
applying their own pane priority. Remote plugin installations need the same aggregation code;
consumers of older remote installations can mirror their actual title color instead.

`test-window-state-edge-cases.sh` also covers empty style options, shared animation lifetime,
retained exited panes (`remain-on-exit`), and resetting the published state.

## State lock recovery

`test-state-lock.py` holds the real writer lock on an isolated tmux server. A blocked hook returns
after two seconds with its update still queued. Killing the owner with SIGKILL must apply that update
automatically, even while the owner's background child remains alive. Queued conditional updates
must run in arrival order. The test also covers explicit unlock before process exit and concurrent
writers. The ancestry tests verify that Codex continues targeting its own pane.

## Pane replacement

`test-window-state-edge-cases.sh` checks retained dead panes, `respawn-pane -k`, and
`respawn-window -k`, followed by reconciliation. Old state and its process identity must disappear. A waiting sibling must
stay waiting, and a new hook in the replacement pane must be accepted under its new process ID.

## Menu-bar colors

`test-hammerspoon-dots.py` uses Lua with a mock Hammerspoon API and an isolated tmux server. It checks
named/indexed/RGB colors, actual current/inactive/inherited styles, remote probe output, palette-only
redraws, and menu colors. SSH-only segments must keep excluding container agents, and changing a
palette must not play state-change sounds. Install Lua to run this optional integration test; CI
installs it and runs the test as part of the suite.

## Container terminal collisions

`test-pane-presence.py` supplies a process snapshot where host and container processes share tty
names. A plain host shell must lose stale agent labels, blue styling, SSH-host labels, and fallback
icons. Real descendants (including wrapped agents and agents in sibling panes) must remain visible.
This checks both the optional presence sweep and the plugin indicator.

### Grouped completion notifications

Run `python3 tests/test-group-notifications.py`. Set `KIBITZ_TEST_REPO` to a matching Kibitz checkout
when it is not beside this repository; that enables the real hook integration on an isolated socket.
The tests cover delayed delivery, queued and identical exchanges, `/mute`, `/tee`, interim and duplicate
stops, focus acknowledgement, pane replacement, delivery failure, and interruption with queued work.
The hook integration checks yellow across the handoff, magenta priority, and color reset on skips,
interruptions and reviewer closure, including when notification sounds are disabled.
`tests/test-hammerspoon-dots.py` checks remote completion event delivery and duplicate window listings.

For a manual check, update both plugins and trust the indicator hooks in Codex. Submit a prompt to a
Claude/Kibitz pair: the window and dot must stay yellow through the handoff. Claude finishing must
stay silent while Codex reviews, and Codex finishing must produce one completion sound. Repeat with
`/tee`, `/mute`, a queued second exchange, and Ctrl+C during review. Interruption must clear yellow
without ringing; a queued review returned to the input box must be tracked when resubmitted, and
discarding it must not block later completion alerts. Repeat on an updated SSH or VS Code host with
Hammerspoon running.
