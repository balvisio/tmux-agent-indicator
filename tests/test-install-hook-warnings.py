#!/usr/bin/env python3
"""Run the installer against disabled Codex hooks in an isolated home."""
import json
import os
from pathlib import Path
import pty
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def main():
    with tempfile.TemporaryDirectory(prefix="test-hook-warnings-") as tmp:
        work = Path(tmp)
        home = work / "home"
        codex = home / "custom codex"
        codex.mkdir(parents=True)
        hooks = codex / "hooks.json"
        config = codex / "config.toml"
        bins = work / "bin"
        bins.mkdir()
        (bins / "codex").write_text('#!/bin/sh\nprintf "%s\\n" "--no-daemon"\n')
        (bins / "codex").chmod(0o755)
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
        env["PATH"] = str(bins) + os.pathsep + env["PATH"]
        env.update(HOME=str(home), CODEX_HOME=str(codex), CODEX_CONFIG_DIR=str(codex),
                   CLAUDE_CONFIG_DIR=str(home / ".claude"), ZDOTDIR=str(home),
                   XDG_CONFIG_HOME=str(home / ".config"), XDG_STATE_HOME=str(home / ".local/state"),
                   TMUX_TMPDIR=str(work), TERM="xterm-256color", SHELL="/bin/bash")
        command = ["bash", str(ROOT / "install.sh"), "--no-claude", "--no-opencode",
                   "--no-tmux-conf", "--no-alias", "--no-hammerspoon"]

        def install(*args):
            result = subprocess.run(command + list(args), env=env, capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr
            return result.stderr

        def setting(event, enabled="false", group=0, index=0):
            key = json.dumps(f"{hooks.resolve()}:{event}:{group}:{index}")
            return f'[hooks.state.{key}]\nenabled = {enabled}\ntrusted_hash = "unchanged"\n'

        assert "WARNING:" not in install(), "fresh install must not warn about disabled hooks"
        config.write_text(setting("post_tool_use"))
        before = config.read_bytes()
        for _ in range(2):
            output = install()
            assert "Disabled tmux-agent-indicator Codex hooks: PostToolUse." in output, output
            assert "Enable them in /hooks" in output, output
            assert "\033[" not in output, "redirected output must not contain color escapes"
            assert config.read_bytes() == before, "disabled setting and trust hash must be preserved"
        print("PASS: install and update warn about disabled PostToolUse without enabling or trusting it")

        # An unrelated hook precedes ours; disabled records for it or removed hooks must not warn.
        data = json.loads(hooks.read_text())
        data["hooks"]["PostToolUse"].insert(0, {"hooks": [{"type": "command", "command": "true"}]})
        hooks.write_text(json.dumps(data))
        config.write_text(setting("post_tool_use", group=0) + setting("post_tool_use", group=9)
                          + setting("post_tool_use", "true", group=1))
        assert "WARNING:" not in install(), "only disabled installed indicator hooks should warn"
        config.write_text(setting("post_tool_use", group=1) + setting("stop") + setting("session_start"))
        output = install()
        for event in ("PostToolUse", "Stop", "SessionStart"):
            assert event in output, output
        assert "WARNING:" not in install("--no-codex"), "skipping Codex should skip its warning"
        print("PASS: warnings follow installed hook positions and exclude unrelated or skipped hooks")

        for key in ("hooks", "codex_hooks"):
            config.write_text(f"[features]\n{key} = false\n")
            before = config.read_bytes()
            output = install()
            assert "Codex hooks are disabled" in output and f"{key} = true" in output, output
            assert config.read_bytes() == before
        config.write_text("[features]\ncodex_hooks = false\nhooks = true\n")
        assert "WARNING:" not in install(), "current feature key takes precedence over legacy alias"
        config.write_text("[broken\n")
        before = config.read_bytes(), hooks.read_bytes()
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode != 0 and "Cannot read valid Codex configuration" in result.stderr, result.stderr
        assert before == (config.read_bytes(), hooks.read_bytes()), "invalid config must stop before writing hooks"
        print("PASS: global disable settings warn, while invalid TOML stops installation")

        config.write_text(setting("post_tool_use", group=1))
        for extra, colored in (({}, True), ({"NO_COLOR": "1"}, False), ({"TERM": "dumb"}, False)):
            master, slave = pty.openpty()
            try:
                result = subprocess.run(command, env=dict(env, **extra), stdout=subprocess.DEVNULL,
                                        stderr=slave, timeout=30)
                assert result.returncode == 0
                output = os.read(master, 8192).decode()
                assert "WARNING:" in output, output
                assert ("\033[33m" in output and "\033[0m" in output) == colored, repr(output)
            finally:
                os.close(slave)
                os.close(master)
        print("PASS: terminal warnings are yellow and respect NO_COLOR and TERM=dumb")


if __name__ == "__main__":
    main()
