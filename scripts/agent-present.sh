#!/usr/bin/env bash
# agent-present.sh — mark tmux windows that contain an AI agent (claude / codex):
#   * paint the window blue while the agent is idle
#   * label it with the agent's icon, plus the Claude session name once it has been /rename'd
#
# Companion to tmux-agent-indicator. That plugin paints a window yellow/magenta/red on hook
# transitions and afterwards RESTORES the window's previous window-status style. This script makes
# that "previous" style blue for windows holding an agent, so idle agent windows read blue while
# the plugin's colours still win whenever a state is active.
#
#   agent-present.sh sweep     re-evaluate every window (one `ps -t` per pane; cheap)
#
# Window options maintained per agent window (read by window-status-format in .tmux.conf):
#   @agent_present 1
#   @agent_label   e.g. "🤖 claude", "🤖 my-session", "🧠 codex", "🤖🧠 my-session", "🤖🧠 agents"
# and per window with an `ssh …` running in a pane (label for the right-hand ssh list in the status bar):
#   @ssh_host      the ssh destination, e.g. "colossus", "user@10.1.2.3"
#
# Cooperation rules (the plugin keeps its bookkeeping in tmux global environment variables):
#   - the plugin owns all color writes and the per-window @agent-indicator-state
#   - this sweep supplies the idle baseline; the plugin preserves any higher-priority state
set -uo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/opt/homebrew/bin:$PATH"

STYLE="bg=blue,fg=white"
UNSET_MARKER="__UNSET__"    # the plugin's marker for "option was not set at window level"
CLAUDE_ICON="🤖"
CODEX_ICON="🧠"
CLAUDE_SESSIONS_DIR="$HOME/.claude/sessions"   # <pid>.json per live Claude session, with name + tmux pane

usage() { echo "usage: $0 sweep" >&2; exit 2; }

# Agents in a pane: require both its process ancestry and its tty. Terminal names
# alone are not unique across container devpts mounts. Match the first or second word of a
# command line (covers `.../bin/claude ...` and `node .../bin/codex ...`). One per line, unique.
pane_agents() {
    "$script_dir/pane-processes.sh" "$1" "$2" | awk '
        { for (i = 1; i <= 2 && i <= NF; i++) { n = split($i, p, "/"); if (p[n] == "claude" || p[n] == "codex") { print p[n]; break } } }' | sort -u
}

# Claude session name for a pane: prints "<name>\t<nameSource>" from the live session file whose
# "tmux" field ends in ".<pane_id>" (newest first, in case a stale file lingers). Empty if none.
claude_session_name() {
    python3 -I - "$CLAUDE_SESSIONS_DIR" "$1" <<'PYTHON'
import json
from pathlib import Path
import sys

files = []
for path in Path(sys.argv[1]).glob("*.json"):
    try:
        files.append((path.stat().st_mtime, path))
    except OSError:
        pass
for _, path in sorted(files, reverse=True):
    try:
        data = json.loads(path.read_text())
        if str(data.get("tmux", "")).endswith("." + sys.argv[2]):
            print(str(data.get("name", "")), str(data.get("nameSource", "")), sep="\t")
            break
    except (OSError, ValueError):
        continue
PYTHON
}

# Label for a window: one icon per agent kind present, then a text:
#   the Claude session name if the user set one (/rename -> nameSource "user"),
#   else "claude" / "codex" for a single kind, else "agents".
#   e.g. "🤖 claude", "🤖 tmux", "🧠 codex", "🤖🧠 tmux", "🤖🧠 agents". Empty when no agent.
window_label() {
    local window_id="$1" pane_id pane_pid tty agent name source
    local icons="" kinds="" claude_name="" text
    local -a kind_array
    while read -r pane_id pane_pid tty; do
        while read -r agent; do
            case "$agent" in
                claude)
                    case " $kinds " in *" claude "*) ;; *) kinds="$kinds claude"; icons="$icons$CLAUDE_ICON" ;; esac
                    if [ -z "$claude_name" ]; then
                        name=""; source=""
                        IFS=$'\t' read -r name source < <(claude_session_name "$pane_id")
                        [ -n "$name" ] && [ "${source:-derived}" != "derived" ] && claude_name="$name"
                    fi ;;
                codex)
                    case " $kinds " in *" codex "*) ;; *) kinds="$kinds codex"; icons="$icons$CODEX_ICON" ;; esac ;;
            esac
        done < <(pane_agents "$pane_pid" "$tty")
    done < <(tmux list-panes -t "$window_id" -F '#{pane_id} #{pane_pid} #{pane_tty}')
    [ -n "$icons" ] || return 0
    if [ -n "$claude_name" ]; then
        text="$claude_name"
    else
        read -r -a kind_array <<< "$kinds"
        if [ "${#kind_array[@]}" -eq 1 ]; then text="${kind_array[0]}"; else text="agents"; fi
    fi
    printf '%s %s' "$icons" "$text"
}

# ssh destination of an `ssh …` in a pane's process tree and on its tty: the first argument that is not an option
# (options taking a separate value are skipped). Empty if no ssh runs there.
pane_ssh_host() {
    "$script_dir/pane-processes.sh" "$1" "$2" | awk '
        { n = split($1, p, "/"); if (p[n] != "ssh") next
          for (i = 2; i <= NF; i++) {
              if ($i ~ /^-[bcDEeFIiJLlmOopQRSWw]$/) { i++; continue }
              if ($i ~ /^-/) continue
              print $i; exit
          } }' | head -n 1
}

# First ssh destination among a window's panes; empty if none.
window_ssh_host() {
    local pane_id pane_pid tty host
    while read -r pane_id pane_pid tty; do
        host=$(pane_ssh_host "$pane_pid" "$tty")
        if [ -n "$host" ]; then
            printf '%s' "$host"
            return 0
        fi
    done < <(tmux list-panes -t "$1" -F '#{pane_id} #{pane_pid} #{pane_tty}')
    return 0
}

# Delegate idle styling and reconciliation to the plugin's serialized window renderer.
paint() {
    local plugin_dir="${TMUX_AGENT_INDICATOR_DIR:-$HOME/.tmux/plugins/tmux-agent-indicator}"
    "$plugin_dir/scripts/window-state.sh" --idle-style "$1" "$2"
}

sweep() {
    local window_id flag ssh_flag label host current changed=0
    while IFS='|' read -r window_id flag ssh_flag; do
        label=$(window_label "$window_id")
        if [ -n "$label" ]; then
            paint "$window_id" "$STYLE"
            current=$(tmux show-options -wqv -t "$window_id" @agent_label)
            if [ "$flag" != "1" ] || [ "$current" != "$label" ]; then
                tmux set-window-option -qt "$window_id" @agent_present 1
                tmux set-window-option -qt "$window_id" @agent_label "$label"
                changed=1
            fi
        elif [ "$flag" = "1" ]; then
            paint "$window_id" "$UNSET_MARKER"
            tmux set-window-option -qt "$window_id" -u @agent_present
            tmux set-window-option -qt "$window_id" -u @agent_label
            changed=1
        fi
        host=$(window_ssh_host "$window_id")
        if [ -n "$host" ]; then
            if [ "$ssh_flag" != "$host" ]; then
                tmux set-window-option -qt "$window_id" @ssh_host "$host"
                changed=1
            fi
        elif [ -n "$ssh_flag" ]; then
            tmux set-window-option -qt "$window_id" -u @ssh_host
            changed=1
        fi
    done < <(tmux list-windows -a -F '#{window_id}|#{@agent_present}|#{@ssh_host}')
    [ "$changed" = "1" ] && tmux refresh-client -S >/dev/null 2>&1
    return 0
}

case "${1:-}" in
    sweep) sweep ;;
    *) usage ;;
esac
