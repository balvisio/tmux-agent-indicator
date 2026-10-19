#!/usr/bin/env bash
# Exercise passthrough with isolated SSH panes and the installed status format.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOCK="ssh-passthrough-$$"
WORK="$(mktemp -d)"
trap 'tmux -L "$SOCK" kill-server 2>/dev/null || true; rm -rf "$WORK"' EXIT
export HOME="$WORK/home" TMUX_TMPDIR="$WORK"
unset TMUX BASH_ENV ENV
mkdir -p "$HOME/.tmux/plugins" "$WORK/bin" "$WORK/stop"
ln -s "$ROOT_DIR" "$HOME/.tmux/plugins/tmux-agent-indicator"
cp "$ROOT_DIR/config/agent-indicator.conf" "$HOME/.tmux/agent-indicator.conf"
printf 'source-file ~/.tmux/agent-indicator.conf\n' > "$HOME/.tmux.conf"
cat > "$WORK/bin/autossh" <<'FAKE'
#!/usr/bin/env bash
host="${@: -1}"
while [ ! -f "${STOP_DIR}/${host}" ]; do sleep 0.1; done
FAKE
# Record only this fixture's SSH pid. The parent shell survives its exit.
cat > "$WORK/ssh-fixture.sh" <<'SSH'
#!/usr/bin/env bash
set -m
bash --noprofile --norc -c '
    echo "$$" > "$1"
    exec ssh -F /dev/null -o "ProxyCommand=sleep 300" -o ConnectTimeout=250 -o BatchMode=yes fixture.invalid
' _ "$1"
exec sleep 300
SSH
chmod +x "$WORK/bin/autossh" "$WORK/ssh-fixture.sh"
export PATH="$WORK/bin:$PATH" STOP_DIR="$WORK/stop"
t() { tmux -L "$SOCK" "$@"; }
S="$ROOT_DIR/scripts/ssh-passthrough.sh"
sweep() { "$ROOT_DIR/scripts/agent-present.sh" sweep; }
fail() { echo "FAIL: $*" >&2; exit 1; }
wait_for() {
    local n=60
    while ! eval "$1" >/dev/null 2>&1; do
        n=$((n - 1)); [ "$n" -gt 0 ] || fail "timed out: $1"; sleep 0.1
    done
}
readme_cmd() {
    awk -v t="bind -T $1" 'index($0, t) == 1 { s = index($0, "run-shell \""); rest = substr($0, s + 11); e = index(rest, "\" \\;"); print substr(rest, 1, e - 1); exit }' "$ROOT_DIR/README.md"
}
ON_CMD=$(readme_cmd root); OFF_CMD=$(readme_cmd off)
[ -n "$ON_CMD" ] && [ -n "$OFF_CMD" ] || fail "README bindings not found"

t -f "$HOME/.tmux.conf" new-session -d -s t -x 120 -y 10 "$WORK/ssh-fixture.sh $WORK/ssh.pid"
export TMUX="$(t display -p '#{socket_path}'),$(t display -p '#{pid}'),0"
t set-environment -g STOP_DIR "$STOP_DIR"
t set-environment -g PATH "$PATH"
SES=$(t display -p '#{session_id}'); PANE=$(t display -p '#{pane_id}'); WIN=$(t display -p '#{window_id}')
OTHER_WIN=$(t new-window -d -t "$SES" -P -F '#{window_id}' 'sleep 300')
wait_for '[ "$(t display -p -t "$PANE" "#{pane_current_command}")" = ssh ]'

# A toggle preserves explicit settings and rendered agent colours, even after changing windows.
t set -t "$SES" prefix C-a \; set -t "$SES" prefix2 C-x \; set -t "$SES" key-table custom
t set -t "$SES" status-style 'bg=cyan,fg=white'
t set -w -t "$WIN" window-status-style 'bg=magenta,fg=black'
t set -w -t "$WIN" window-status-current-style 'bg=magenta,fg=black'
t set -w -t "$OTHER_WIN" window-status-current-style 'bg=blue,fg=white'
sweep
baseline=$(t display -p -t "$PANE" '#{T:status-format[0]}')
t run-shell -t "$PANE" "$ON_CMD"
[ "$(t show -t "$SES" -qv @ssh_passthrough_pane)" = "$PANE" ] || fail "owner pane not recorded"
[ "$(t show -t "$SES" -v prefix)" = None ] && [ "$(t show -t "$SES" -v prefix2)" = None ] || fail "both prefixes must pass through"
[ "$(t show -t "$SES" -v key-table)" = off ] || fail "key table must pass through"
bar=$(t display -p -t "$PANE" '#{T:status-format[0]}')
case "$bar" in '#[fg=black,bg=colour208,fill=colour208,bold,set-default]'*) ;; *) fail "missing orange overlay: $bar" ;; esac
case "$bar" in *'bg=magenta,fg=black'*) ;; *) fail "agent colour lost during passthrough" ;; esac
[ "$(t show -t "$SES" -v status-style)" = 'bg=cyan,fg=white' ] || fail "status-style was overwritten"
"$S" on "$SES" "$PANE"  # Repeated activation must not replace the saved settings.
sweep
[ "$(t show -t "$SES" -v key-table)" = off ] || fail "live SSH should keep passthrough"
t select-window -t "$OTHER_WIN"
t run-shell "$OFF_CMD"
[ "$(t show -t "$SES" -v prefix)" = C-a ] && [ "$(t show -t "$SES" -v prefix2)" = C-x ] || fail "custom prefixes not restored"
[ "$(t show -t "$SES" -v key-table)" = custom ] || fail "custom key table not restored"
[ "$(t show -w -t "$WIN" -v window-status-current-style)" = 'bg=magenta,fg=black' ] || fail "original window style changed"
[ "$(t show -w -t "$OTHER_WIN" -v window-status-current-style)" = 'bg=blue,fg=white' ] || fail "other window style changed"
"$S" off "$SES"
t select-window -t "$WIN"
[ "$(t display -p -t "$PANE" '#{T:status-format[0]}')" = "$baseline" ] || fail "normal rendering did not return"
echo "PASS: repeated toggles restore custom keyboard settings and preserve colours across window changes"

# Plain SSH finishes but its pane remains. A sweep restores inherited settings.
for option in prefix prefix2 key-table; do t set -t "$SES" -u "$option"; done
t run-shell -t "$PANE" "$ON_CMD"
kill -TERM "$(cat "$WORK/ssh.pid")"
wait_for '[ "$(t display -p -t "$PANE" "#{pane_current_command}")" = sleep ]'
sweep
for option in prefix prefix2 key-table; do
    [ -z "$(t show -t "$SES" -qv "$option")" ] || fail "$option must inherit again"
done
[ -z "$(t show -t "$SES" -qv @ssh_passthrough_pane)" ] || fail "SSH exit did not clear owner"
"$S" on "$SES" "$PANE"
[ -z "$(t show -t "$SES" -qv @ssh_passthrough_pane)" ] || fail "local panes must not enter passthrough"
echo "PASS: a sweep restores inherited settings after plain SSH exits"

# The launcher stays an SSH pane during retries and requests a sweep on normal exit.
t respawn-pane -k -t "$PANE" "bash -c 'set -m; $ROOT_DIR/scripts/ssh-reconnect.sh alpha; exec sleep 300'"
wait_for '[ -n "$(t show -p -t "$PANE" -qv @ssh_reconnect_pid)" ]'
t run-shell -t "$PANE" "$ON_CMD"
sweep
[ "$(t show -t "$SES" -v key-table)" = off ] || fail "retrying launcher lost passthrough"
touch "$STOP_DIR/alpha"
wait_for '[ -z "$(t show -t "$SES" -qv @ssh_passthrough_pane)" ]'
echo "PASS: reconnect retries keep passthrough and launcher exit restores controls"

# A dead marker cannot keep passthrough active, and closing its pane restores only its session.
OTHER_SES=$(t new-session -d -s unrelated -P -F '#{session_id}' 'sleep 300')
t set -t "$OTHER_SES" prefix C-z
t set -p -t "$PANE" @ssh_reconnect_host fixture \; set -p -t "$PANE" @ssh_reconnect_pid "$$"
"$S" on "$SES" "$PANE"
t set -p -t "$PANE" @ssh_reconnect_pid 999999999
sweep
[ -z "$(t show -t "$SES" -qv @ssh_passthrough_pane)" ] || fail "stale marker kept passthrough"
t set -p -t "$PANE" @ssh_reconnect_host fixture \; set -p -t "$PANE" @ssh_reconnect_pid "$$"
"$S" on "$SES" "$PANE"
t kill-pane -t "$PANE"
sweep
[ -z "$(t show -t "$SES" -qv @ssh_passthrough_pane)" ] || fail "closed owner kept passthrough"
[ "$(t show -t "$OTHER_SES" -v prefix)" = C-z ] || fail "unrelated session changed"
echo "PASS: stale and closed owners release passthrough without changing other sessions"
