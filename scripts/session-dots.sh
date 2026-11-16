#!/usr/bin/env bash
# Session dots - shows all sessions as symbols, highlighting current and attention-needing sessions.

set -euo pipefail

if ! command -v tmux >/dev/null 2>&1; then
    exit 0
fi
if ! tmux display-message -p '#{session_name}' >/dev/null 2>&1; then
    exit 0
fi

current="${1:-}"

tmux_option_is_set() {
    local option="$1"
    local raw
    raw=$(tmux show-option -gq "$option" 2>/dev/null || true)
    [ -n "$raw" ]
}

tmux_get_option_or_default() {
    local option="$1"
    local default_value="$2"
    local value
    if tmux_option_is_set "$option"; then
        value=$(tmux show-option -gqv "$option")
        printf '%s\n' "$value"
    else
        printf '%s\n' "$default_value"
    fi
}

ACTIVE_SYMBOL=$(tmux_get_option_or_default "@agent-indicator-session-dots-active" "●")
INACTIVE_SYMBOL=$(tmux_get_option_or_default "@agent-indicator-session-dots-inactive" "○")
ATTENTION_SYMBOL=$(tmux_get_option_or_default "@agent-indicator-session-dots-attention" "●")
DOTS_COLOR=$(tmux_get_option_or_default "@agent-indicator-session-dots-color" "")
ACTIVE_COLOR=$(tmux_get_option_or_default "@agent-indicator-session-dots-active-color" "")
ATTENTION_COLOR=$(tmux_get_option_or_default "@agent-indicator-session-dots-attention-color" "yellow")
ATTENTION_STATES=$(tmux_get_option_or_default "@agent-indicator-session-dots-attention-states" "needs-input,done")

# Build lookup of attention states
declare -A attention_state_map
IFS=',' read -ra STATE_ARRAY <<< "$ATTENTION_STATES"
for s in "${STATE_ARRAY[@]}"; do
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    [ -n "$s" ] && attention_state_map["$s"]=1
done

# Build set of sessions needing attention by scanning agent pane state env vars
declare -A attention_sessions
while IFS= read -r line; do
    [ -z "$line" ] && continue
    # Format: TMUX_AGENT_PANE_%xx_STATE=<state>
    pane_id="${line#TMUX_AGENT_PANE_}"
    pane_id="${pane_id%%_STATE=*}"
    state="${line#*_STATE=}"
    [ -n "${attention_state_map[$state]:-}" ] || continue
    session=$(tmux display-message -p -t "$pane_id" '#{session_name}' 2>/dev/null || true)
    [ -n "$session" ] && attention_sessions["$session"]=1
done < <(tmux show-environment -g 2>/dev/null | grep '^TMUX_AGENT_PANE_.*_STATE=' || true)

# Color helpers
color_open() {
    local color="$1"
    [ -n "$color" ] && printf '#[fg=%s]' "$color"
}

color_reset() {
    if [ -n "$DOTS_COLOR" ]; then
        printf '#[fg=%s]' "$DOTS_COLOR"
    else
        printf '#[default]'
    fi
}

# Render dots
result=""
if [ -n "$DOTS_COLOR" ]; then
    result="#[fg=${DOTS_COLOR}]"
fi

while IFS='|' read -r session_id seen session; do
    [ -z "$session" ] && continue
    # Session-scoped markers follow renames and disappear with their session.
    if [ -n "$seen" ] && [ -z "${attention_sessions[$session]:-}" ]; then
        tmux set-option -qu -t "$session_id" @agent-indicator-session-seen 2>/dev/null || true
        seen=''
    fi
    has_attention=""
    if [ "$session" != "$current" ] && [ -n "${attention_sessions[$session]:-}" ]; then
        [ "$seen" != "1" ] && has_attention=1
    fi

    if [ -n "$has_attention" ]; then
        result+="$(color_open "$ATTENTION_COLOR")${ATTENTION_SYMBOL}$(color_reset)"
    elif [ "$session" = "$current" ]; then
        if [ -n "$ACTIVE_COLOR" ]; then
            result+="$(color_open "$ACTIVE_COLOR")${ACTIVE_SYMBOL}$(color_reset)"
        else
            result+="${ACTIVE_SYMBOL}"
        fi
    else
        result+="${INACTIVE_SYMBOL}"
    fi
done < <(tmux list-sessions -F '#{session_id}|#{@agent-indicator-session-seen}|#{session_name}' 2>/dev/null)

if [ -n "$DOTS_COLOR" ]; then
    result+="#[default]"
fi

printf '%s' "$result"
