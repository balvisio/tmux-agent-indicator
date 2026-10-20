# Hammerspoon window dots

`agent_dots.lua` is an optional macOS companion for an existing tmux presence/sound setup. It displays
local agent windows and agent windows on SSH/VS Code hosts, with menus for switching windows.
SSH segments show only the host's tmux; VS Code segments may also show its devcontainers.

Wide titles automatically summarize Terminal, VS Code, and SSH, keeping the clock visible:

```text
T: ●6  |  VS: ●3  |  SSH: ○  00:42  >
```

Each source shows its agent-window count and a summary dot using the highest-priority
window's actual color: needs-input > running > done > idle. An empty source keeps its hollow dot.
Hosts and containers remain separate in the dropdown, with every window still selectable.
When both VS Code and SSH connect to a host, sessions attached through a VS Code terminal appear
under VS Code, alongside its devcontainers. Other host sessions appear under SSH. The attached
tmux client's environment identifies VS Code; session names do not affect grouping. Detached
sessions keep their last observed source. A session attached from both sources appears once under
SSH. If SSH disconnects, the remaining host agents appear under the VS Code group.
VS Code host sessions include their session name in the label, with a leading `vscode-` omitted:
`vscode-bionemo-framework` appears as `VS - linux-box - bionemo-framework`.

Click **>** to expand into individual dots and host/container names, or **<** to collapse.
Details appear in the menu bar when they fit. Otherwise, the bar keeps its summaries and opens a
floating strip directly below the menu bar on the screen where you clicked. The strip stays on one
compact row, sizes to its contents, and lets you click a dot to select its window. If it exceeds the
screen width, use its left/right controls to scroll. It updates while open and overlays application windows.
Click **<** to close it. Changing screen sizes or connected hosts moves expanded details between the
bar and the strip as needed.

**F9** toggles expansion too, using the screen under the pointer for the floating strip. It is inactive
in Do Not Disturb. Customize it with `AGENT_DOTS.expansionHotkey` in `init.lua` before loading the module
(for example `{ mods = {"cmd"}, key = "F9" }`), or set it to `false` to disable it. If the shortcut is
already assigned, the dropdown reports the conflict and the arrow remains available.

The arrow sits after the clock and toggles directly, without opening a menu. Clicking the menu-bar
dots or clock opens the usual agent/mode menu; there are no layout options in it. The clock stays
visible in every layout. Do Not Disturb hides the arrow and strip until On is selected again.
Manual expansion/collapse lasts until Hammerspoon reloads, when automatic sizing resumes.
Set `AGENT_DOTS.slackBadge = true` in `init.lua` before loading the module to hide Slack's Dock badge
in Do Not Disturb and enable it while On. This uses macOS 26's private notification preferences and
requires an unlocked **Notifications → Slack → Badge application icon** switch. Grant Hammerspoon
Full Disk Access if the menu reports denied access. System Settings stays closed. Refreshing the
notification service affects all apps; rapid toggles can delay badge changes by about 10 seconds.
The dots menu reports failures; agent controls keep working.
Automatic sizing measures the full title and collapses above 600 points or 30% of the narrowest
connected screen, whichever is smaller. This is a conservative budget, not a measurement of space
left by other menu-bar apps. Adjust `MAX_TITLE_WIDTH` near the top of the module to change the cap.

The module reads `@agent-indicator-state` for state labels and notifications. Dot and menu colors
come from the window's actual title style, including the active-window style, inherited status style,
and last-window/bell/activity overrides. Local and remote polls use the same format. Changing a color
without changing state redraws the dots on the next poll (2 seconds locally, 3 seconds remotely).

Supported colors include tmux names, `colour0`–`colour255` (`colorN` also works), and `#RRGGBB`.
RGB and extended indexed colors have portable values; named/ANSI colors use stable menu-bar
counterparts because terminal themes can redefine those colors. When the background has no explicit
color, the dot uses the foreground, then neutral gray if neither is available. Inline `#[...]` colors
inside a custom window label are outside this style-based integration.

## SSH window colors in tmux

The same remote poll publishes `@ssh_agent_host` and `@ssh_agent_color` on every local SSH window.
The color comes from the highest-priority agent window in the SSH group: needs-input > running > done >
idle. VS Code sessions and devcontainers are excluded. With no SSH agents, the color is green.
Multiple SSH windows connected to the same host receive the same color without additional remote polls.

Add this format helper to `.tmux.conf`, then prepend `#{E:@ssh_agent_title_style}` to both your
existing `window-status-format` and `window-status-current-format` values:

```tmux
set -g @ssh_agent_title_style '#{?#{&&:#{==:#{pane_current_command},ssh},#{&&:#{==:#{@agent_label},},#{&&:#{==:#{@ssh_host},#{@ssh_agent_host}},#{@ssh_agent_color}}}},#[bg=#{@ssh_agent_color}#,fg=#{?#{==:#{@ssh_agent_color},blue},white,black}#,noreverse],}'
```

Reload `.tmux.conf` and Hammerspoon after updating the module. The helper colors the SSH title's
background, preserving the usual current-window marker and mouse target. It applies only when the
active pane runs SSH and the window has no local agent label. The cached host must match `@ssh_host`,
so switching destinations cannot apply the previous host's color. Disconnected windows and failed
polls clear the override, returning to the normal tmux style. Updates continue in Do Not Disturb.
Only custom display options are written; local agent states and saved window styles are unaffected.

## Existing companion configuration

The script expects these window options from `scripts/agent-present.sh sweep` (wired up by the shipped `config/agent-indicator.conf`):

- `@agent_present=1` for windows containing an agent.
- `@agent_label` for their display labels.
- `@ssh_host` on local SSH windows for remote-host discovery.

For remote notifications and Do Not Disturb, it uses the plugin's `scripts/agent-sound.sh` and
`~/.local/state/agent-dots/mode`. The installer puts both in place.
The tmux binary path near the top of the module defaults to `/opt/homebrew/bin/tmux`; adjust it for
other Homebrew installations. Remote hosts need tmux and the same presence options. Container
inspection additionally uses Docker on the remote host.

## Install from this checkout

Run `./install.sh`, then reload Hammerspoon. The installer copies the module and its Slack badge
helper and adds the following line to `~/.hammerspoon/init.lua`:

```lua
require("agent_dots")
```

`~/.cache/agent_dots.state` records the most recent state and color
for each drawn window.

Run `./tests/test-hammerspoon-dots.py` to check automatic sizing, arrow clicks, color conversion,
local/remote queries, palette-only redraws, menus, notifications, and SSH/container separation
without launching Hammerspoon or SSH.
The test requires Lua and tmux.
