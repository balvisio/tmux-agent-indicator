#!/usr/bin/env python3
"""Exercise installer failures without touching live tmux, agent settings, or dependencies."""
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")


def main():
    with tempfile.TemporaryDirectory(prefix="test-install-checks-") as tmp:
        work = Path(tmp)
        home, bins = work / "home", work / "bin"
        home.mkdir()
        bins.mkdir()
        # A closed PATH makes absent tools deterministic, including on developer machines.
        for name in ("python3", "dirname", "mktemp", "rm", "mkdir", "cp", "chmod", "grep", "awk", "tail", "date", "cat", "head"):
            (bins / name).symlink_to(shutil.which(name))

        def executable(name, body):
            (bins / name).write_text("#!/bin/sh\n" + body + "\n")
            (bins / name).chmod(0o755)

        executable("bash", 'printf "%s\\n" "${TEST_BASH_VERSION:-5.2.0}"')
        executable("codex", 'printf "%s\\n" "${TEST_CODEX_HELP:---no-daemon}"')
        executable("uname", 'echo Linux')
        executable("tmux", '''
printf '%s\\n' "$*" >> "$TEST_TMUX_CALLS"
case "$1" in
  -V) echo "${TEST_TMUX_VERSION:-tmux 3.6a}" ;;
  info) exit "${TEST_TMUX_INFO:-1}" ;;
  source-file) echo 'test: config reload failed' >&2; exit 1 ;;
  *) exit 0 ;;
esac''')
        claude, codex = home / ".claude", home / ".codex"
        claude.mkdir()
        codex.mkdir()
        settings, hooks, config = claude / "settings.json", codex / "hooks.json", codex / "config.toml"
        settings.write_text('{"permissions":{"allow":["Read"]}}\n')
        hooks.write_text('{"hooks":{}}\n')
        config.write_text('# user settings\n')
        target = home / ".tmux/plugins/tmux-agent-indicator"
        target.mkdir(parents=True)
        sentinel = target / "README.md"
        sentinel.write_text("previous install")
        env = dict(HOME=str(home), PATH=str(bins), SHELL="/bin/bash", ZDOTDIR=str(home),
                   CODEX_HOME=str(codex), CLAUDE_CONFIG_DIR=str(claude),
                   XDG_CONFIG_HOME=str(home / ".config"), XDG_STATE_HOME=str(home / ".local/state"),
                   TMUX_TMPDIR=str(work), TEST_TMUX_CALLS=str(work / "tmux.log"), TERM="xterm-256color")
        command = [BASH, str(ROOT / "install.sh"), "--no-opencode", "--no-alias", "--no-hammerspoon", "--no-tmux-conf"]

        def run(extra=None, args=None):
            return subprocess.run(command if args is None else args, env=dict(env, **(extra or {})),
                                  capture_output=True, text=True, timeout=30)

        def snapshot():
            return {str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file()}

        def fails_unchanged(fragment, extra=None):
            before = snapshot()
            result = run(extra)
            assert result.returncode != 0 and fragment in result.stderr, result
            assert snapshot() == before, "failed preflight changed the installation or settings"

        for path in (settings, hooks):
            valid = path.read_text()
            for invalid in ('{"hooks":', '[]', '{"hooks":[]}', '{"hooks":{"Stop":[{"hooks":null}]}}'):
                path.write_text(invalid)
                fails_unchanged(str(path))
            path.write_text(valid)
        for invalid in ('[broken', 'features = false', '[hooks]\nstate = 3'):
            config.write_text(invalid)
            fails_unchanged(str(config))
        config.write_text('# user settings\n')
        print("PASS: invalid JSON, TOML, and hook structures leave settings and existing installs untouched")

        fails_unchanged("tmux 3.6+ required", {"TEST_TMUX_VERSION": "tmux 3.5a"})
        fails_unchanged("Bash 4.0+ required", {"TEST_BASH_VERSION": "3.2.57"})
        for name, error in (("tmux", "Cannot run tmux"), ("bash", "Cannot run Bash"), ("python3", "Python 3.8+ is required")):
            (bins / name).rename(bins / (name + ".saved"))
            try:
                fails_unchanged(error)
            finally:
                (bins / (name + ".saved")).rename(bins / name)
        (bins / "python3").rename(bins / "python3.saved")
        executable("python3", 'echo "ERROR: Python 3.8+ required; found 3.7.0" >&2; exit 1')
        fails_unchanged("Python 3.8+ required; found 3.7.0")
        (bins / "python3").unlink()
        (bins / "python3.saved").rename(bins / "python3")
        print("PASS: missing or incompatible core tools stop before installation")

        if os.geteuid() != 0:
            for path in (sentinel, hooks, target):
                mode = path.stat().st_mode & 0o777
                path.chmod(0o500 if path.is_dir() else 0o400)
                try:
                    fails_unchanged(str(path))
                finally:
                    path.chmod(mode)
            print("PASS: unwritable destinations stop before installation")

        settings.write_text('{"disableAllHooks":true,"permissions":{"allow":["Read"]}}')
        result = run({"TEST_CODEX_HELP": "Usage: codex [OPTIONS]"})
        assert result.returncode == 0, result.stderr
        assert "Installation completed with warnings" in result.stderr
        assert "Claude hooks are disabled" in result.stderr and "does not advertise --no-daemon" in result.stderr
        assert json.loads(settings.read_text())["disableAllHooks"] is True
        assert "autossh" not in result.stderr and "Hammerspoon" not in result.stderr
        assert "info" not in Path(env["TEST_TMUX_CALLS"]).read_text(), "--no-tmux-conf must not contact a server"
        # Skipping an agent also skips its broken config and capability check.
        settings.write_text('broken')
        hooks.write_text('broken')
        config.write_text('broken')
        result = run({"TEST_CODEX_HELP": "unsupported"}, command + ["--no-claude", "--no-codex"])
        assert result.returncode == 0 and "WARNING:" not in result.stderr, result
        print("PASS: optional warnings preserve settings and respect skipped integrations")

        # Seed pinned dependencies, so a complete config install never needs the network.
        revisions = runpy.run_path(str(ROOT / "scripts/recovery-dependencies.py"))["REVISIONS"]
        for name, revision in revisions.items():
            directory = target.parent / name
            directory.mkdir()
            (directory / ".tmux-agent-indicator-managed").write_text(revision)
            (directory / (name[len("tmux-"):] + ".tmux")).touch()
        full = [arg for arg in command if arg != "--no-tmux-conf"] + ["--no-claude", "--no-codex"]
        result = run(args=full)
        assert result.returncode == 0 and "ssh-reconnect needs autossh" in result.stderr, result
        assert "could not reload" not in result.stderr, "no running server is normal"
        executable("autossh", "exit 0")
        result = run({"TEST_TMUX_INFO": "0"}, full)
        assert result.returncode == 0 and "could not reload" in result.stderr, result
        assert "test: config reload failed" in result.stderr, "tmux's error must remain visible"
        assert "tmux source-file" in result.stderr
        print("PASS: missing autossh and reload failures warn; no running server does not")


if __name__ == "__main__":
    main()
