#!/usr/bin/env bash
# ssh-reconnect.sh keeps a window an ssh window while it waits to reconnect; stale markers go away.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOCK="ssh-reconnect-$$"
WORK="$(mktemp -d)"
trap 'tmux -L "$SOCK" kill-server 2>/dev/null || true; rm -rf "$WORK"' EXIT
export HOME="$WORK/home" TMUX_TMPDIR="$WORK"
mkdir -p "$HOME/.tmux/plugins" "$WORK/bin"
ln -s "$ROOT_DIR" "$HOME/.tmux/plugins/tmux-agent-indicator"
cp "$ROOT_DIR/config/agent-indicator.conf" "$HOME/.tmux/agent-indicator.conf"
printf 'source-file ~/.tmux/agent-indicator.conf\n' > "$HOME/.tmux.conf"
# Fake autossh: "connecting" until a stop file for its host appears, then a normal exit.
cat > "$WORK/bin/autossh" <<'FAKE'
#!/usr/bin/env bash
host="${@: -1}"
while [ ! -f "${STOP_DIR}/${host}" ]; do sleep 0.2; done
exit 0
FAKE
chmod +x "$WORK/bin/autossh"
export PATH="$WORK/bin:$PATH" STOP_DIR="$WORK/stop"; mkdir -p "$STOP_DIR"
t() { tmux -L "$SOCK" "$@"; }
sweep() { "$ROOT_DIR/scripts/agent-present.sh" sweep; }
wait_for() { # wait_for <seconds> '<shell condition>'  (re-evaluated every 0.1 s)
    local n=$(( $1 * 10 ))
    while ! eval "$2" >/dev/null 2>&1; do n=$((n - 1)); [ "$n" -gt 0 ] || return 1; sleep 0.1; done
}
fail() { echo "FAIL: $*" >&2; exit 1; }

unset TMUX
t -f "$HOME/.tmux.conf" new-session -d -s t -x 120 -y 10 'sleep 300'
# Everything below, the sweep included, must talk to this server and never to the caller's tmux.
export TMUX="$(t display -p '#{socket_path}'),$(t display -p '#{pid}'),0"
t set-environment -g STOP_DIR "$STOP_DIR"; t set-environment -g PATH "$PATH"
t new-window -d -t t -n r "$ROOT_DIR/scripts/ssh-reconnect.sh alpha"
WIN=$(t display -p -t t:r '#{window_id}'); PANE=$(t display -p -t t:r '#{pane_id}')
wait_for 5 '[ -n "$(t display -p -t "$PANE" "#{@ssh_reconnect_pid}")" ]' || fail "launcher did not mark its pane"
sweep
[ "$(t show -wqv -t "$WIN" @ssh_host)" = alpha ] || fail "window should carry the host while reconnecting"
[ "$(t display -p -t "$WIN" '#{E:@ssh_window}')" = 1 ] || fail "@ssh_window should be true while reconnecting"
t display -p -t "$WIN" '#{T:window-status-format}' | grep -q 'alpha' || fail "window label should show the host"
bar=$(t display -p '#{T:status-format[0]}')
right="${bar#*nolist align=right}"
case "$right" in *alpha*) ;; *) fail "reconnecting window must be in the right-hand ssh list" ;; esac
case "${bar%%nolist align=right*}" in *alpha*) fail "reconnecting window must not also be in the left list" ;; esac
echo "PASS: a reconnecting pane keeps its host label and right-hand placement"

# Two launchers in one window own their panes separately; a normal exit clears only its own.
t split-window -d -t "$WIN" "$ROOT_DIR/scripts/ssh-reconnect.sh beta"
PANE2=$(t list-panes -t "$WIN" -F '#{pane_id}' | grep -v "^$PANE\$")
wait_for 5 '[ -n "$(t display -p -t "$PANE2" "#{@ssh_reconnect_pid}")" ]' || fail "second launcher did not mark its pane"
[ "$(t display -p -t "$PANE" '#{@ssh_reconnect_host}')" = alpha ] || fail "first pane marker overwritten"
touch "$STOP_DIR/alpha"
wait_for 5 '[ -z "$(t display -p -t "$PANE" "#{@ssh_reconnect_host}")" ]' || fail "normal exit must clear the pane marker"
[ "$(t display -p -t "$PANE2" '#{@ssh_reconnect_host}')" = beta ] || fail "other pane must keep its marker"
sweep
[ "$(t show -wqv -t "$WIN" @ssh_host)" = beta ] || fail "window host should follow the remaining launcher"
echo "PASS: launchers own their panes; a normal exit clears only its own marker"

# A stale marker in a later pane is cleaned even when an earlier pane still holds a live host.
t split-window -d -t "$WIN" "$ROOT_DIR/scripts/ssh-reconnect.sh gamma"
PANE=$(t list-panes -t "$WIN" -F '#{pane_id}' | grep -v "^$PANE2\$")
wait_for 5 '[ "$(t display -p -t "$PANE" "#{@ssh_reconnect_host}")" = gamma ]' || fail "third launcher did not mark its pane"
pid2=$(t display -p -t "$PANE2" '#{@ssh_reconnect_pid}')
kill -9 "$pid2"
wait_for 5 "! kill -0 $pid2"
sweep
[ -z "$(t display -p -t "$PANE2" '#{@ssh_reconnect_host}')" ] || fail "stale marker in a later pane must be removed"
[ "$(t show -wqv -t "$WIN" @ssh_host)" = gamma ] || fail "window host should be the live launcher's"
echo "PASS: a dead launcher's marker is discarded even behind a live one"

# SIGTERM to the launcher ends autossh too and clears the marker; the window returns to the left.
pid1=$(t display -p -t "$PANE" '#{@ssh_reconnect_pid}')
kill -TERM "$pid1"
wait_for 5 "! kill -0 $pid1" || fail "launcher must exit on SIGTERM"
pgrep -f "autossh.*gamma" >/dev/null && fail "autossh must not outlive its launcher"
wait_for 5 '[ -z "$(t display -p -t "$PANE" "#{@ssh_reconnect_host}")" ]' || fail "SIGTERM must clear the marker"
sweep
[ -z "$(t show -wqv -t "$WIN" @ssh_host)" ] || fail "window must stop being an ssh window"
[ "$(t display -p -t "$WIN" '#{E:@ssh_window}')" = 0 ] || fail "@ssh_window should be false afterwards"
echo "PASS: terminating the launcher terminates autossh and releases the window"

# Tab completion lists the Host entries of ~/.ssh/config and nothing else.
cat > "$WORK/ssh_config" <<'CFG'
# comment
Host colossus
  Host indented
HOST upper
Host colossus *.example.org
Host=eqform
Hostname nothost
HostKeyAlgorithms ssh-ed25519
Host neg !bad good2 # trailing words
Host ?.wild *
CFG
hosts=$("$ROOT_DIR/scripts/ssh-hosts.sh" "$WORK/ssh_config" | tr '\n' ' ')
[ "$hosts" = "colossus indented upper eqform neg good2 " ] || fail "ssh-hosts.sh parsed: $hosts"
comp=$(bash --norc --noprofile -c '
    mkdir -p "$1/.ssh" && cp "$2/ssh_config" "$1/.ssh/config"; HOME="$1"
    _fake_ssh() { COMPREPLY=(known-host); }; complete -F _fake_ssh ssh
    . "$3/scripts/ssh-reconnect-completion.sh"
    COMP_WORDS=(ssh-reconnect col); COMP_CWORD=1; _ssh_reconnect_complete; echo "col:${COMPREPLY[*]}"
    COMP_WORDS=(ssh-reconnect ""); COMP_CWORD=1; _ssh_reconnect_complete; echo "all:${COMPREPLY[*]}"
' _ "$HOME" "$WORK" "$ROOT_DIR")
case "$comp" in *"col:colossus"*) ;; *) fail "completion of col: $comp" ;; esac
case "$comp" in *"all:colossus indented upper eqform neg good2"*) ;; *) fail "completion must list config hosts only: $comp" ;; esac
echo "PASS: ssh-reconnect completes the Host entries of ~/.ssh/config"
