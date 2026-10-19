#!/usr/bin/env bash
# ssh-reconnect.sh — ssh that comes back by itself after a dropped link (laptop sleep, VPN blip).
#
#   ssh-reconnect.sh HOST [ssh options...]
#
# Runs autossh around ssh with keepalives every 15 s (link declared dead after two misses, so about
# 30 s), a 10 s connect timeout and retries at most 10 s apart. Ends when ssh exits normally (you
# typed exit or detached) or on Ctrl-C while it waits. Authentication must work without a prompt.
#
# Inside tmux the pane carries @ssh_reconnect_host / @ssh_reconnect_pid while this runs, so the
# status bar keeps treating the window as an ssh window between attempts (the sweep in
# agent-present.sh drops the marker if this process is gone).
set -uo pipefail

usage() { echo "usage: ${0##*/} HOST [ssh options...]" >&2; exit 2; }
[ $# -ge 1 ] || usage
command -v autossh >/dev/null 2>&1 || { echo "ssh-reconnect: autossh is required (brew install autossh)" >&2; exit 127; }

# The destination is the first argument that is not an option (options taking a value are skipped).
host=""; skip=0
for arg in "$@"; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$arg" in
        -[bcDEeFIiJLlmOopQRSWw]) skip=1 ;;
        -*) ;;
        *) host="$arg"; break ;;
    esac
done
[ -n "$host" ] || usage

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
in_tmux=0
[ -n "${TMUX:-}" ] && [ -n "${TMUX_PANE:-}" ] && in_tmux=1

child=""
cleanup() {
    if [ -n "$child" ] && kill -0 "$child" 2>/dev/null; then
        kill -TERM "$child" 2>/dev/null
        wait "$child" 2>/dev/null
    fi
    if [ "$in_tmux" = 1 ]; then
        tmux set-option -pqu -t "$TMUX_PANE" @ssh_reconnect_host \; set-option -pqu -t "$TMUX_PANE" @ssh_reconnect_pid 2>/dev/null
        tmux run-shell -b "$script_dir/agent-present.sh sweep" 2>/dev/null
    fi
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 129' HUP
trap '' INT   # Ctrl-C belongs to ssh (forwarded to the remote side, or ends a waiting autossh)

if [ "$in_tmux" = 1 ]; then
    tmux set-option -pq -t "$TMUX_PANE" @ssh_reconnect_host "$host" \; \
         set-option -pq -t "$TMUX_PANE" @ssh_reconnect_pid "$$" 2>/dev/null
    tmux run-shell -b "$script_dir/agent-present.sh sweep" 2>/dev/null
    "$script_dir/agent-recovery.py" ssh "$@" || true
fi

# Backgrounded only so this shell can react to signals; <&0 keeps the terminal as its input.
AUTOSSH_GATETIME=0 AUTOSSH_POLL=10 AUTOSSH_FIRST_POLL=10 \
    autossh -M 0 -o ServerAliveInterval=15 -o ServerAliveCountMax=2 -o ConnectTimeout=10 "$@" <&0 &
child=$!
wait "$child"
rc=$?
child=""
exit "$rc"
