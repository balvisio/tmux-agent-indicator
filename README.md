# tmux-agent-indicator

tmux window titles, pane borders and a macOS menu-bar item that follow the state of the Claude Code
and Codex sessions running in your panes: running, needs input, done, idle.

## Requirements

- tmux 3.1+, bash 4+, Python 3, autossh (`brew install autossh`) for `ssh-reconnect`
- Claude Code and/or Codex on the machine
- macOS extras: Hammerspoon (`brew install --cask hammerspoon`) for the menu-bar item

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/balvisio/tmux-agent-indicator/main/install.sh | bash
```

Or from a checkout: `./install.sh`. The installer

- copies the plugin to `~/.tmux/plugins/tmux-agent-indicator`
- writes `~/.tmux/agent-indicator.conf` and adds one `source-file` line to `~/.tmux.conf`; your settings go in `~/.tmux/agent-indicator.local.conf`
- adds the Claude hooks to `~/.claude/settings.json` and the Codex hooks to `~/.codex/hooks.json`
- adds `alias codex='codex --no-daemon'` and `alias ssh-reconnect=…` to the shell startup files
- on macOS with Hammerspoon installed: copies `agent_dots.lua` to `~/.hammerspoon` and adds `require("agent_dots")` to `init.lua`
- reloads the running tmux server

A file of yours that the installer replaces is kept next to it as a dated `.bak` copy.
Options: `--no-tmux-conf`, `--no-alias`, `--no-hammerspoon`, `--no-claude`, `--no-codex`, `--no-opencode`, `--uninstall`.

## Finish

1. Open a new shell (picks up the alias).
2. Codex: run `/hooks` once and trust the tmux-agent-indicator hooks. Repeat after an update that changes a hook.
3. Hammerspoon: grant Accessibility on first launch, enable *Launch at login*, then menu-bar icon → **Reload Config**.

## Verify

```bash
S=~/.tmux/plugins/tmux-agent-indicator/scripts/agent-state.sh
bash "$S" --agent demo --state needs-input   # current window title magenta, Ping
bash "$S" --agent demo --state done          # red, Glass
bash "$S" --agent demo --state off           # cleared
```

Start `claude` or `codex` in a window and switch to another one: the title is blue while idle, yellow
while it works, magenta when it waits for you, red when it finished, cleared when you come back.
Windows whose pane runs `ssh` are listed at the right end of the status bar.

## Remote Linux host

Run the same installer on the host (or inside the dev container when the terminal runs there). The
installer skips the Hammerspoon step there. If agent icons show as `_`, start tmux as `tmux -u`.

Open the host with `ssh-reconnect HOST` instead of `ssh HOST`: the connection comes back by itself
after the laptop sleeps or the network drops, and the window keeps its place in the status bar
meanwhile. Put `RequestTTY force` and `RemoteCommand tmux new-session -A -s main` in the host's
`~/.ssh/config` block to land in the same remote tmux session every time.

- Through an `ssh HOST` window of a local tmux: the local window entry takes the colour of the most
  urgent agent on that host, and the menu bar gains an `SSH - HOST` segment with the Mac sounds.
  `HOST` must be an alias in `~/.ssh/config` that connects without a prompt.
- Through the VS Code Remote-SSH terminal: the menu bar gains a `VS - HOST` segment while VS Code is
  connected. Without Hammerspoon, the remote can only ring the terminal bell; VS Code plays it only with
  `"accessibility.signals.terminalBell": { "sound": "on" }` in the user settings.

## Update

Run the install command again. Then `/hooks` in Codex if a hook changed, and reload Hammerspoon.

## Uninstall

```bash
~/.tmux/plugins/tmux-agent-indicator/install.sh --uninstall
```

## Customize

Put your settings in `~/.tmux/agent-indicator.local.conf`. It is loaded after the defaults and never
touched by the installer. Colours per state:

```tmux
set -g @agent-indicator-running-window-title-bg 'yellow'
set -g @agent-indicator-needs-input-window-title-bg 'magenta'
set -g @agent-indicator-done-window-title-bg 'red'
```

To turn the sounds off for good:

```tmux
set -g @agent-indicator-notification-enabled 'off'
```

To silence them for a while, press **F10** or click the menu-bar item and choose **Do Not Disturb**;
the same again switches back. The colours keep working. Another key goes in `~/.hammerspoon/init.lua`
before the `require` line (`hotkey = false` for none):

```lua
AGENT_DOTS = { hotkey = { mods = {}, key = "F9" } }
```

Every other option is listed in the reference below.

## Reference

### How it works

The plugin tracks three states per pane: `running`, `needs-input`, and `done`.

State transitions are driven by hooks. Claude Code and Codex fire hooks on prompt submit, permission request, and stop. OpenCode uses its plugin system. Any other agent can call `agent-state.sh` directly from a wrapper script or hook.

Each state can change:
- Pane border color
- Pane background (supported but disabled by default to avoid visual noise)
- Window title background and foreground
- Status bar icon (per-agent, e.g. `claude=🤖`, `codex=🧠`)

Focusing any pane in a window acknowledges **all completed agents in that window**.
Waiting and running agents keep their states. Needs-input indicators stay visible until
the agent reports its next state; focusing a permission prompt does not answer it. Window titles
are coloured only for windows other than the one you are looking at, unless
`@agent-indicator-style-active-window` is on.

When several panes share a window, its title and border use the highest-priority live pane state:

| Panes in the window | Window state |
| --- | --- |
| Any agent needs input | `needs-input` |
| Otherwise, any agent is running | `running` |
| Otherwise, at least one agent is done | `done` |
| All agents are idle/off | `idle` (restores the original style) |

This applies to any number of panes, regardless of hook arrival order. For example, done + running
stays running, done + idle shows done, and needs-input + done stays needs-input. Canceling one agent
clears only its pane. Moving or closing a pane recalculates the affected windows. State records also
include the pane process ID. Exit hooks and reconciliation sweeps clear dead or replaced processes,
even if tmux reuses the pane ID. Tmux does not emit a respawn hook; a forced respawn is cleaned up by
the next state writer or `window-state.sh` sweep.

The plugin publishes this result as the window option `@agent-indicator-state`. Menu-bar indicators
should read that option instead of independently ranking pane states:

```bash
tmux list-windows -a -F '#{window_id} #{@agent-indicator-state}'
```

Use `@agent-indicator-style-active-window on` and configure title colors for each state when mirroring
window colors in another UI. The plugin does not install an OS menu-bar integration; the optional
[Hammerspoon companion](integrations/hammerspoon/README.md) reads the actual title styles for its dot
colors, including custom palettes. An integration that supplies an idle color can call `scripts/window-state.sh --idle-style <window_id> 'bg=blue,fg=white'`
(or `__UNSET__` to remove its idle override). That command shares the state-update lock, preserves
active colors, and reconciles the window. Polling it also repairs stale colors without acknowledging
completion. Run `scripts/window-state.sh` to reconcile every window without changing pane states.

State writers use a process-owned file lock scoped to the tmux server. The operating system releases
it when the writer exits, including a forced kill; background children do not inherit the lock.
Writers acquire the lock in queue order. After two seconds of contention, a hook returns successfully
and its detached retry keeps waiting; the update is applied when earlier writers finish. This keeps
agent hooks responsive without dropping or reordering state transitions. Codex resolves its pane
before detaching, and queued events are discarded if that pane was replaced or the server stopped.
Full sweeps lock one window update at a time so hooks can run between updates. The empty lock file
next to the tmux socket is intentional and must not be deleted while that server has active writers.

#### Presence labels

`scripts/agent-present.sh sweep` maintains blue idle styling, `@agent_present`, `@agent_label`, and
`@ssh_host` for integrations such as the Hammerspoon dots. It uses `scripts/pane-processes.sh` to
require both a matching terminal and ancestry from the pane process. This prevents container
processes with the same `pts/N` names from labeling unrelated host shells as agents or SSH sessions.
The plugin indicator uses the same ancestry filter for its process-detection fallback.


### Agent limits

`#{agent_limits}` shows the shortest active account-limit window for Claude and Codex:

```text
C 5h 4% used · X 7d 2% used
```

Percentages are used allowance, matching the provider dashboards. `C` means Claude and `X` means Codex. These limits are account-wide, so every pane using the same provider shares them.

The collector reads local agent data:

- Claude: `cachedUsageUtilization` from `~/.claude.json`, plus fresh `rate_limits` captured from Claude's status-line input
- Codex: the newest `token_count.rate_limits` snapshot under `~/.codex/sessions/`

The installer wraps an existing Claude status-line command and passes its input and output through unchanged. `--uninstall-claude` restores the previous command. Expired data is hidden.

Configuration:

```tmux
set -g @agent-indicator-limits-enabled 'on'
set -g @agent-indicator-limits-providers 'claude,codex'
set -g @agent-indicator-limits-cache-seconds '60'
```

Omit a provider to hide it. Set the cache duration to `0` to read the source files on every status refresh.

### Session Dots

Optional visual session indicator in the status bar. Shows all sessions as symbols with the current one highlighted. Sessions where agents need attention (`needs-input` or `done` state) are highlighted in a different color.

Includes code from [tmux-session-dots](https://github.com/jtmcginty/tmux-session-dots) by [Jack McGinty](https://github.com/jtmcginty), used under the [MIT License](licenses/tmux-session-dots.txt).

Example with 4 sessions, second is current, fourth needs attention: `○●○●`

With emoji symbols:

<img width="2518" alt="session dots full status bar" src="https://github.com/user-attachments/assets/565fe020-85bb-4c1c-96a6-2571c3b40019" />
<img width="163" alt="session dots close up" src="https://github.com/user-attachments/assets/8fbf3776-3f1b-42b5-a0b3-b94ba2bce861" />

```tmux
set -g @agent-indicator-session-dots-active '🌕'
set -g @agent-indicator-session-dots-inactive '🌑'
set -g @agent-indicator-session-dots-attention '🤖'
```

Add `#{agent_session_dots}` to your status bar:

```tmux
set -g status-right '#{agent_session_dots} #{agent_indicator} | %H:%M'
```

Configuration:

```tmux
# Symbols
set -g @agent-indicator-session-dots-active '●'
set -g @agent-indicator-session-dots-inactive '○'
set -g @agent-indicator-session-dots-attention '●'

# Colors (empty = inherit from status bar)
set -g @agent-indicator-session-dots-color ''
set -g @agent-indicator-session-dots-active-color ''
set -g @agent-indicator-session-dots-attention-color 'yellow'

# Which agent states trigger attention highlighting (comma-separated)
set -g @agent-indicator-session-dots-attention-states 'needs-input,done'
```

The current session always shows as the active dot, even if it also has agents needing attention.

### Configuration

Quick start with the most useful options:

```tmux
# Enable/disable visual channels
set -g @agent-indicator-border-enabled 'on'
set -g @agent-indicator-indicator-enabled 'on'

# Per-agent icons
set -g @agent-indicator-icons 'claude=🤖,codex=🧠,opencode=💻,default=🤖'

# Keep pane colors until you focus the pane (instead of clearing immediately)
set -g @agent-indicator-reset-on-focus 'on'

# Also colour the window title of the window you are currently in
set -g @agent-indicator-style-active-window 'on'

# Knight Rider animation while running
set -g @agent-indicator-animation-enabled 'on'
```

<details>
<summary>Full configuration reference</summary>

```tmux
# Global toggles
set -g @agent-indicator-background-enabled 'on'
set -g @agent-indicator-border-enabled 'on'
set -g @agent-indicator-indicator-enabled 'on'

# Account limits
set -g @agent-indicator-limits-enabled 'on'
set -g @agent-indicator-limits-providers 'claude,codex'
set -g @agent-indicator-limits-cache-seconds '60'

# Running state (default keeps pane/border unchanged)
set -g @agent-indicator-running-enabled 'on'
set -g @agent-indicator-running-bg 'default'
set -g @agent-indicator-running-border 'default'
set -g @agent-indicator-running-window-title-bg ''
set -g @agent-indicator-running-window-title-fg ''

# Needs-input state
set -g @agent-indicator-needs-input-enabled 'on'
set -g @agent-indicator-needs-input-bg 'default'
set -g @agent-indicator-needs-input-border 'yellow'
set -g @agent-indicator-needs-input-window-title-bg 'yellow'
set -g @agent-indicator-needs-input-window-title-fg 'black'

# Done state
set -g @agent-indicator-done-enabled 'on'
set -g @agent-indicator-done-bg 'default'
set -g @agent-indicator-done-border 'green'
set -g @agent-indicator-done-window-title-bg 'red'
set -g @agent-indicator-done-window-title-fg 'black'

# Per-agent icons
set -g @agent-indicator-icons 'claude=🤖,codex=🧠,opencode=💻,default=🤖'

# Process fallback detection
set -g @agent-indicator-processes 'claude,codex,aider,cursor,opencode'

# Keep pane colors until pane focus-in after done
set -g @agent-indicator-reset-on-focus 'on'

# Window-title styling for the active window too (default: only other windows)
set -g @agent-indicator-style-active-window 'off'

# Running animation in status indicator
set -g @agent-indicator-animation-enabled 'off'
set -g @agent-indicator-animation-speed '300'

# Notifications (tmux display-message on state transitions)
set -g @agent-indicator-notification-enabled 'on'
set -g @agent-indicator-notification-states 'needs-input,done'
set -g @agent-indicator-notification-format '[#{agent_name}] #{agent_state} (#{session_name}:#{window_name})'
set -g @agent-indicator-notification-duration '5000'
set -g @agent-indicator-notification-message 'on'
set -g @agent-indicator-notification-command ''

# Session dots (visual session indicator in status bar)
set -g @agent-indicator-session-dots-active '●'
set -g @agent-indicator-session-dots-inactive '○'
set -g @agent-indicator-session-dots-attention '●'
set -g @agent-indicator-session-dots-color ''
set -g @agent-indicator-session-dots-active-color ''
set -g @agent-indicator-session-dots-attention-color 'yellow'
set -g @agent-indicator-session-dots-attention-states 'needs-input,done'
```

Pane background coloring is unchanged by default (`*-bg 'default'` for all states).
To enable background colors, set per-state values in your `~/.tmux.conf`, for example:

```tmux
set -g @agent-indicator-needs-input-bg 'colour94'
set -g @agent-indicator-done-bg 'green'
```

Empty option values are treated as "do not apply this property" (for toggles, empty behaves as disabled). Example:

```tmux
set -g @agent-indicator-done-bg ''
```

This skips done-state background changes while still allowing done border/title styles.

Note: tmux border coloring is window-scoped (`pane-active-border-style` / `pane-border-style`).
You can style the active border differently, but tmux cannot set a fully independent border color for one arbitrary non-active pane.

</details>

<details>
<summary>Color presets</summary>

Preset 1 (Balanced):

```tmux
set -g @agent-indicator-running-bg ''
set -g @agent-indicator-running-border ''
set -g @agent-indicator-needs-input-bg 'colour223'
set -g @agent-indicator-needs-input-border 'colour214'
set -g @agent-indicator-done-bg ''
set -g @agent-indicator-done-border 'colour34'
set -g @agent-indicator-done-window-title-bg 'colour34'
set -g @agent-indicator-done-window-title-fg 'black'
```

Preset 2 (High Contrast):

```tmux
set -g @agent-indicator-running-bg 'colour52'
set -g @agent-indicator-running-border 'colour196'
set -g @agent-indicator-needs-input-bg 'colour94'
set -g @agent-indicator-needs-input-border 'colour226'
set -g @agent-indicator-done-bg ''
set -g @agent-indicator-done-border 'colour46'
set -g @agent-indicator-done-window-title-bg 'colour46'
set -g @agent-indicator-done-window-title-fg 'black'
```

Preset 3 (Subtle):

```tmux
set -g @agent-indicator-running-bg ''
set -g @agent-indicator-running-border 'colour244'
set -g @agent-indicator-needs-input-bg ''
set -g @agent-indicator-needs-input-border 'colour220'
set -g @agent-indicator-done-bg ''
set -g @agent-indicator-done-border 'colour70'
set -g @agent-indicator-done-window-title-bg 'colour238'
set -g @agent-indicator-done-window-title-fg 'colour194'
```

</details>

<details>
<summary>Tmux color reference</summary>

Tmux supports:
- 8 basic colors: `black`, `red`, `green`, `yellow`, `blue`, `magenta`, `cyan`, `white`
- Bright variants (`brightred`, `brightblue`, etc.)
- 256-color palette: `colour0` ... `colour255`

![Tmux color chart](docs/assets/tmux-colors.png)

</details>

### Notifications

State transitions trigger a `tmux display-message` notification. Enabled by default for `needs-input` and `done` states.

```tmux
# Disable notifications entirely
set -g @agent-indicator-notification-enabled 'off'

# Only notify on done
set -g @agent-indicator-notification-states 'done'

# Custom format
set -g @agent-indicator-notification-format '#{agent_name} is #{agent_state} in #{session_name}'

# Duration in milliseconds (0 = use tmux default display-time)
set -g @agent-indicator-notification-duration '3000'

# Skip the on-screen message but still run the notification command below
set -g @agent-indicator-notification-message 'off'
```

Available format placeholders: `#{agent_name}`, `#{agent_state}`, `#{session_name}`, `#{window_name}`, `#{window_index}`.

You can also run an external command on each notification. The command receives environment variables `AGENT_NAME`, `AGENT_STATE`, `AGENT_SESSION`, and `AGENT_WINDOW`:

```tmux
# Log notifications to a file
set -g @agent-indicator-notification-command 'echo "$AGENT_NAME $AGENT_STATE" >> /tmp/agent-notify.log'

# macOS system notification
set -g @agent-indicator-notification-command 'osascript -e "display notification \"$AGENT_NAME is $AGENT_STATE\" with title \"tmux agent\""'

# macOS sound only (pair with notification-message 'off')
set -g @agent-indicator-notification-command 'afplay /System/Library/Sounds/Ping.aiff'
```

### Custom Agent Integration

To integrate any agent that does not have built-in hook support, call `agent-state.sh` from your agent's hooks or wrapper script.

```bash
~/.tmux/plugins/tmux-agent-indicator/scripts/agent-state.sh --agent claude --state running
~/.tmux/plugins/tmux-agent-indicator/scripts/agent-state.sh --agent claude --state needs-input
~/.tmux/plugins/tmux-agent-indicator/scripts/agent-state.sh --agent claude --state done
~/.tmux/plugins/tmux-agent-indicator/scripts/agent-state.sh --agent claude --state off
```

`--state off` always resets pane background and border immediately.

Codex requires `codex --no-daemon`. Its hooks follow their process ancestry to the originating
tmux pane, supporting multiple Codex sessions even when `TMUX_PANE` is missing or stale.
Hooks without a pane ancestor are ignored. Other agents use `TMUX_PANE`.

To make this the default when typing `codex`, add this alias to your shell startup file
(`~/.bash_profile` for Bash login shells, `~/.bashrc` for other interactive Bash shells,
or `~/.zshrc` for Zsh), then open a new shell:

```bash
alias codex='codex --no-daemon'
```

Setting `[features] daemon_auto_start = false` alone is insufficient: Codex can still connect to
an existing daemon. Scripts must pass `--no-daemon` explicitly because they do not use interactive
aliases. See the [Codex CLI reference](https://learn.chatgpt.com/docs/cli/reference).

`--if-state <state>` makes the call conditional: nothing happens unless the pane is currently in that
state (`off` = no state recorded). The `PostToolUse` hooks use it to return from `needs-input`
to `running` once a permission prompt or question has been answered, without touching `running` or
`done` panes:

```bash
~/.tmux/plugins/tmux-agent-indicator/scripts/agent-state.sh --agent claude --state running --if-state needs-input
```

### Hook Templates

Default template files:
- `hooks/claude-hooks.json`
- `hooks/codex-hooks.json`

The installer uses these templates as the source for Claude and Codex hook configuration, replacing the default plugin path with the selected install target.

They map:
- `UserPromptSubmit` -> `running`
- `PermissionRequest` -> `needs-input`
- `PostToolUse` -> `running`, only while the pane is `needs-input` (an answered permission prompt or
  question goes back to `running` instead of staying `needs-input` until `Stop`)
- `Stop` -> `done`
- `SessionEnd` -> `off`
- Codex `Interrupt` -> `off` when an active turn is canceled

Prompt submission uses a single `running` update, which replaces the previous pane state under
the same lock. Claude runs matching hooks in parallel, so a separate `off` hook could finish last
and incorrectly clear an active turn. Reinstalling replaces older plugin hook entries with this template.

Codex hooks must be reviewed and trusted with `/hooks` before they can run.

Claude does not fire `Stop` when a turn is interrupted. The tmux plugin binds Ctrl+C to forward
the key normally, then clear `running` or `needs-input` state in that Claude pane. It preserves
custom Ctrl+C bindings; if you have one, append
`run-shell '~/.tmux/plugins/tmux-agent-indicator/scripts/pane-interrupt.sh "#{pane_id}"'`
after its key-forwarding command. This fallback covers Ctrl+C sent through tmux, not cancellation
through Escape, another client, or an external signal. Codex uses its native
[Interrupt hook](https://learn.chatgpt.com/docs/hooks#interrupt).

### OpenCode Plugin

The installer copies `plugins/opencode-tmux-agent-indicator.js` to `~/.config/opencode/plugins/`. The plugin uses OpenCode's event system:
- `session.status` (busy) -> `running`
- `permission.updated` -> `needs-input`
- `session.idle` -> `done`

To install manually without the installer, copy the plugin file:

```bash
mkdir -p ~/.config/opencode/plugins
cp plugins/opencode-tmux-agent-indicator.js ~/.config/opencode/plugins/
```

### License

MIT
