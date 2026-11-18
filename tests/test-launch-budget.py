#!/usr/bin/env python3
"""Count PATH executable entries and wall time on real, isolated hook calls.

The shims count the program requested, not their instrumentation interpreter.
Absolute shebang /usr/bin/env and the agent's own launcher are outside this budget.
"""
import collections
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import runtime

ROOT = Path(os.environ.get("AGENT_TEST_PLUGIN", Path(__file__).resolve().parents[1]))
TMUX = shutil.which("tmux")


def main():
    with tempfile.TemporaryDirectory(prefix="agent-launch-budget-") as directory:
        home = Path(directory)
        bins = home / "bin"
        bins.mkdir()
        log = home / "launches"
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("TMUX", "CLAUDE", "CODEX", "KIBITZ", "XDG_"))}
        env.update(HOME=directory, XDG_STATE_HOME=str(home / "state"), XDG_CONFIG_HOME=str(home / "config"),
                   PATH=str(bins) + os.pathsep + env["PATH"], AGENT_LAUNCH_LOG=str(log),
                   TMUX_AGENT_RUNTIME_ROOT=str(home / "runtime"))
        for name in ("bash", "python3", "tmux", "ps", "awk", "sed", "tr", "dirname", "grep", "cat",
                     "date", "sleep", "mkdir", "rm", "touch", "nohup"):
            program = shutil.which(name)
            if program:
                shim = bins / name
                shim.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(name)
                                + ' >> "$AGENT_LAUNCH_LOG"\nexec ' + shlex.quote(program) + ' "$@"\n')
                shim.chmod(0o755)
        socket = str(home / "socket")

        def t(*args):
            return subprocess.check_output([TMUX, "-S", socket, *args], env=env, text=True, timeout=20).strip()

        def hook(arguments, event):
            result, output, payload = (home / name for name in ("result", "output", "payload"))
            result.unlink(missing_ok=True)
            payload.write_text(json.dumps(event))
            log.write_text("")
            command = (shlex.join([str(ROOT / "scripts/agent-state.sh"), "--hook", "--agent", "claude", *arguments])
                       + " < " + shlex.quote(str(payload)) + " > " + shlex.quote(str(output))
                       + " 2>&1; printf '%s' $? > " + shlex.quote(str(result)))
            start = time.monotonic()
            t("send-keys", "-t", "%0", "-l", command)
            t("send-keys", "-t", "%0", "Enter")
            while not result.exists() and time.monotonic() - start < 30:
                time.sleep(.025)
            assert result.exists(), "hook did not finish"
            assert result.read_text() == "0", output.read_text()
            return collections.Counter(log.read_text().splitlines()), time.monotonic() - start

        server_pid = None
        try:
            t("-f", "/dev/null", "new-session", "-d", "-s", "test", "/bin/bash --noprofile --norc")
            server_pid = int(t("display", "-p", "#{pid}"))
            t("set", "-g", "@agent-indicator-notification-enabled", "off")
            t("set", "-g", "@agent-indicator-recovery", "off")
            t("set", "-g", "@agent-indicator-style-active-window", "on")
            setup, setup_time = hook(["--state", "running"], dict(session_id="budget", hook_event_name="UserPromptSubmit", prompt="work"))
            counts, elapsed = hook(["--state", "running", "--if-state", "needs-input"],
                                   dict(session_id="budget", hook_event_name="PostToolUse", tool_use_id="tool"))
            assert t("show-environment", "-g", "TMUX_AGENT_PANE_%0_STATE").endswith("=running")
            print(json.dumps(dict(prompt=dict(launches=sum(setup.values()), programs=dict(setup), seconds=round(setup_time, 3)),
                                  noop=dict(launches=sum(counts.values()), programs=dict(counts), seconds=round(elapsed, 3))), sort_keys=True), flush=True)
            if not os.environ.get("AGENT_TEST_BASELINE"):
                assert sum(counts.values()) <= 4, counts
                assert counts["tmux"] == 1 and counts["ps"] == 0, counts
                print("PASS: conditional no-op uses at most four executable entries, one tmux query and no process scan", flush=True)
                other = t('new-window', '-d', '-P', '-F', '#{pane_id}', '-n', 'other', '/bin/bash --noprofile --norc')
                marker = home / 'runtime/arrivals/other'
                marker.write_text(runtime.marker_text(os.getpid(), server_pid, other))
                queue = Path(f'{socket}.agent-indicator-{server_pid}.lock.queue')
                writer = queue / 'other.run'
                writer.write_text(runtime.marker_text(os.getpid(), server_pid, other))
                try:
                    counts, _ = hook(['--state', 'running', '--if-state', 'needs-input'],
                                     dict(session_id='budget', hook_event_name='PostToolUse', tool_use_id='next'))
                    assert sum(counts.values()) <= 4, counts
                finally:
                    marker.unlink(missing_ok=True)
                    writer.unlink(missing_ok=True)
                print('PASS: another pane entering or applying a hook does not defeat the no-op budget', flush=True)
                hook(["--state", "off"], {})
                t('run', str(ROOT / 'agent-indicator.tmux'))
                t('run', shlex.join(['python3', str(ROOT/'scripts/state-lock.py'), '--wait', '--barrier']))
                time.sleep(.2)
                log.write_text('')
                for _ in range(6): t('next-window')
                time.sleep(.3)
                counts = collections.Counter(log.read_text().splitlines())
                assert not counts, counts
                print('PASS: six ordinary window switches launch no plugin programs', flush=True)
        finally:
            subprocess.run([TMUX, "-S", socket, "kill-server"], env=env, capture_output=True)
            deadline = time.monotonic() + 5
            while server_pid and runtime.process(server_pid) and time.monotonic() < deadline:
                time.sleep(.05)


if __name__ == "__main__":
    main()
