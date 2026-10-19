#!/usr/bin/env bash
# install.sh keeps one copy of each line it owns in the startup files, replaces an older version of
# a line it ships, and the Bash-only completion line is skipped by sh and zsh.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export HOME="$WORK/home" ZDOTDIR="$WORK/home" TMUX_TMPDIR="$WORK" CLAUDE_CONFIG_DIR="$WORK/claude" CODEX_CONFIG_DIR="$WORK/codex" XDG_CONFIG_HOME="$WORK/xdg"
mkdir -p "$HOME"
TAG="managed by tmux-agent-indicator install.sh"
printf '# mine\n' > "$HOME/.bash_profile"
printf 'export X=1\n' > "$HOME/.profile"
printf 'autoload -Uz compinit; compinit -u\n(( $+functions[compdef] )) && compdef ssh-reconnect=ssh   # %s\n' "$TAG" > "$HOME/.zshrc"
fail() { echo "FAIL: $*" >&2; exit 1; }
install() { SHELL=/bin/bash "$ROOT_DIR/install.sh" --no-tmux-conf --no-hammerspoon --no-claude --no-codex --no-opencode >/dev/null; }

install; install
for f in .bash_profile .profile; do
    [ "$(grep -c "alias ssh-reconnect=" "$HOME/$f")" = 1 ] || fail "$f: alias must appear once"
    [ "$(grep -c "ssh-reconnect-completion.sh" "$HOME/$f")" = 1 ] || fail "$f: completion line must appear once"
    grep -q 'BASH_VERSION' "$HOME/$f" || fail "$f: completion line must be guarded"
done
[ "$(grep -c "compdef" "$HOME/.zshrc")" = 1 ] || fail ".zshrc: exactly one compdef line expected: $(grep -c compdef "$HOME/.zshrc")"
grep -q "compdef _ssh_reconnect ssh-reconnect" "$HOME/.zshrc" || fail ".zshrc: old compdef line was not replaced"
head -1 "$HOME/.bash_profile" | grep -q '^# mine$' || fail "user content must be kept"
echo "PASS: startup lines are added once and older versions are replaced"

# Other shells read the shared files without errors and the guarded line itself exits 0 for them,
# so sourcing a startup file that ends with it still succeeds; Bash loads it only when interactive.
sh -c '. "$1/.profile"' _ "$HOME" || fail ".profile must be safe for sh"
zsh -fc '. "$1/.profile"' _ "$HOME" 2>/dev/null || fail ".profile must be safe for zsh"
guard=$(grep -F "ssh-reconnect-completion.sh" "$HOME/.profile")
out=$(sh -c "$guard" 2>&1; echo "rc=$?"); [ "$out" = "rc=0" ] || fail "guard line under sh: $out"
out=$(zsh -fc "$guard" 2>&1; echo "rc=$?"); [ "$out" = "rc=0" ] || fail "guard line under zsh: $out"
out=$(bash --norc --noprofile -c "$guard" 2>&1; echo "rc=$?"); [ "$out" = "rc=0" ] || fail "guard line under non-interactive bash: $out"
zline=$(grep -F "compdef" "$HOME/.zshrc")
out=$(zsh -fc "$zline" 2>&1; echo "rc=$?"); [ "$out" = "rc=0" ] || fail "zsh line without compinit must still exit 0: $out"
zsh -fc '. "$1/.zshrc" && (( $+functions[_ssh_reconnect] ))' _ "$HOME" || fail ".zshrc completion function must load under zsh"
probe='type _ssh_reconnect_complete >/dev/null 2>&1 && echo loaded || echo not-loaded'
out=$(bash --norc --noprofile -c ". \"\$1/.bash_profile\"; $probe" _ "$HOME")
[ "$out" = not-loaded ] || fail "non-interactive bash must not load the completion: $out"
out=$(bash --norc --noprofile -ic ". \"\$1/.bash_profile\"; $probe" _ "$HOME" 2>/dev/null)
[ "$out" = loaded ] || fail "interactive bash must load the completion: $out"
out=$(bash --norc --noprofile --posix -ic ". \"\$1/.bash_profile\"; $probe" _ "$HOME" 2>/dev/null)
[ "$out" = not-loaded ] || fail "POSIX-mode bash must not load the completion: $out"
# An interactive login /bin/sh reads ~/.profile: no syntax error, nothing loaded (macOS: Bash as sh).
out=$(printf '%s\nexit\n' "$probe" | /bin/sh -il 2>&1 </dev/stdin || true)
case "$out" in *"syntax error"*) fail "interactive sh choked on .profile: $out" ;; esac
case "$out" in *not-loaded*) ;; *) fail "interactive sh must not load the completion: $out" ;; esac
echo "PASS: the completion loads in interactive Bash only; sh, POSIX-mode Bash and zsh skip it"

"$ROOT_DIR/install.sh" --uninstall >/dev/null
for f in .bash_profile .profile .zshrc; do
    grep -qF "$TAG" "$HOME/$f" && fail "$f: tagged lines must be gone after uninstall"
done
echo "PASS: uninstall removes every owned line"
