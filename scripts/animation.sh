#!/usr/bin/env bash
# Knight Rider animation loop for the running state.
# Launched as a background process by agent-state.sh.
# Bounces a frame index 0->6->0 and writes it to a tmux env var,
# then forces a status bar refresh each tick.

set -euo pipefail

cleanup() {
    # An exiting loop must not erase the PID/frame of a replacement loop.
    [ "$(tmux show-environment -g TMUX_AGENT_ANIMATION_PID 2>/dev/null || true)" = "TMUX_AGENT_ANIMATION_PID=$$" ] || return 0
    tmux set-environment -gu TMUX_AGENT_ANIMATION_FRAME 2>/dev/null || true
    tmux set-environment -gu TMUX_AGENT_ANIMATION_PID 2>/dev/null || true
}
trap cleanup EXIT

# Bail out if tmux server is gone.
if ! command -v tmux >/dev/null 2>&1; then
    exit 0
fi
if ! tmux list-sessions >/dev/null 2>&1; then
    exit 0
fi

tmux_get_option_or_default() {
    local option="$1"
    local default_value="$2"
    local raw
    raw=$(tmux show-option -gq "$option" 2>/dev/null || true)
    if [ -n "$raw" ]; then
        tmux show-option -gqv "$option"
    else
        printf '%s\n' "$default_value"
    fi
}

# Store our PID so agent-state.sh can kill us.
tmux set-environment -g TMUX_AGENT_ANIMATION_PID "$$"

speed_ms=$(tmux_get_option_or_default "@agent-indicator-animation-speed" "300")
# Convert ms to fractional seconds for sleep.
# Pad to 3 digits so printf produces correct decimal: 50 -> 050 -> 0.050
padded=$(printf '%03d' "$speed_ms" 2>/dev/null || printf '%s' "$speed_ms")
sleep_arg="0.${padded}"
if [ "$speed_ms" -ge 1000 ] 2>/dev/null; then
    sleep_arg="1"
fi

# Bounce sequence: 0 1 2 3 4 5 6 5 4 3 2 1 (12 frames total)
frames=(0 1 2 3 4 5 6 5 4 3 2 1)
frame_count=${#frames[@]}
idx=0

any_pane_running() {
    tmux show-environment -g 2>/dev/null | grep -q '_STATE=running'
}

while true; do
    # Self-terminate if tmux server is gone.
    if ! tmux list-sessions >/dev/null 2>&1; then
        break
    fi

    # Self-terminate if no pane is in running state.
    if ! any_pane_running; then
        break
    fi

    frame="${frames[$idx]}"
    tmux set-environment -g TMUX_AGENT_ANIMATION_FRAME "$frame" 2>/dev/null || break
    tmux refresh-client -S 2>/dev/null || true

    idx=$(( (idx + 1) % frame_count ))
    sleep "$sleep_arg" 2>/dev/null || sleep 1
done
