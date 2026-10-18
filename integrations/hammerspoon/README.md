# Hammerspoon window dots

`agent_dots.lua` is an optional macOS companion for an existing tmux presence/sound setup. It displays
local agent windows and agent windows on SSH/VS Code hosts, with menus for switching windows.
SSH segments show only the host's tmux; VS Code segments may also show its devcontainers.

The module reads `@agent-indicator-state` for state labels and notifications. Dot and menu colors
come from the window's actual title style, including the active-window style, inherited status style,
and last-window/bell/activity overrides. Local and remote polls use the same format. Changing a color
without changing state redraws the dots on the next poll (2 seconds locally, 3 seconds remotely).

Supported colors include tmux names, `colour0`–`colour255` (`colorN` also works), and `#RRGGBB`.
RGB and extended indexed colors have portable values; named/ANSI colors use stable menu-bar
counterparts because terminal themes can redefine those colors. When the background has no explicit
color, the dot uses the foreground, then neutral gray if neither is available. Inline `#[...]` colors
inside a custom window label are outside this style-based integration.

## Existing companion configuration

The script expects these window options from a presence sweep, such as `~/.tmux/agent-present.sh`:

- `@agent_present=1` for windows containing an agent.
- `@agent_label` for their display labels.
- `@ssh_host` on local SSH windows for remote-host discovery.

For remote notifications and Do Not Disturb, it uses `~/.tmux/agent-sound.sh` and
`~/.local/state/agent-dots/mode`. These presence/sound scripts are separate from this repository.
The tmux binary path near the top of the module defaults to `/opt/homebrew/bin/tmux`; adjust it for
other Homebrew installations. Remote hosts need tmux and the same presence options. Container
inspection additionally uses Docker on the remote host.

## Install from this checkout

Copy `agent_dots.lua` to `~/.hammerspoon/agent_dots.lua`, retain a reference in `~/.hammerspoon/init.lua`,
and reload Hammerspoon:

```lua
agentDots = require("agent_dots")
```

The tmux plugin installer does not change Hammerspoon configuration. Keep a backup before replacing
an independently customized copy. `~/.cache/agent_dots.state` records the most recent state and color
for each drawn window.

Run `./tests/test-hammerspoon-dots.py` to check color conversion, local/remote queries, palette-only
redraws, menus, notifications, and SSH/container separation without launching Hammerspoon or SSH.
The test requires Lua and tmux.
