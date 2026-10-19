#!/usr/bin/env bash
# agent-sound.sh — alert when an agent needs input or finishes.
# Run by tmux-agent-indicator as @agent-indicator-notification-command; the plugin passes the
# transition in the environment (AGENT_STATE: running | needs-input | done, AGENT_NAME: claude | codex),
# and TMUX_PANE (inherited from the agent's hook) identifies the agent's pane.
#
# On a Mac: play a system sound (/System/Library/Sounds: Ping, Glass, Pop, Tink, Hero, Submarine, ...).
# Elsewhere (e.g. a remote Linux box reached from VS Code or iTerm2): ring the terminal bell in the
# agent's pane. tmux forwards it to the attached terminal, which plays its own bell sound
# (VS Code: "accessibility.signals.terminalBell": { "sound": "on" }; iTerm2: Profiles > Terminal).
#
# Do Not Disturb: when the menu-bar dots (~/.hammerspoon/agent_dots.lua) are switched to Do Not
# Disturb they write "dnd <epoch>" to the mode file below; nothing is played then (tmux colours are
# unaffected). "on <epoch>", a missing file, or a remote host without the file = sounds on.
MODE_FILE="$HOME/.local/state/agent-dots/mode"

case "${AGENT_STATE:-}" in
    needs-input) sound="Ping" ;;
    done)        sound="Glass" ;;
    *)           exit 0 ;;
esac

if [ -r "$MODE_FILE" ]; then
    read -r mode _ < "$MODE_FILE"
    [ "$mode" = "dnd" ] && exit 0
fi

if command -v afplay >/dev/null 2>&1; then
    # Detached, so the agent's hook returns immediately instead of waiting for the sound to finish.
    nohup afplay "/System/Library/Sounds/${sound}.aiff" >/dev/null 2>&1 &
elif [ -n "${TMUX_PANE:-}" ]; then
    tty=$(tmux display-message -p -t "$TMUX_PANE" '#{pane_tty}' 2>/dev/null)
    [ -n "$tty" ] && printf '\a' > "$tty" 2>/dev/null
fi
exit 0
