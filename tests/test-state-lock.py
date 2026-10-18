#!/usr/bin/env python3
"""Exercise the real state entrypoints against process death and contention."""

import os
from pathlib import Path
import select
import signal
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]


def main():
    socket = "agent-lock-test-" + uuid.uuid4().hex[:12]
    environment = {key: value for key, value in os.environ.items() if key != "TMUX"}
    children = []
    workers = []
    lock_file = None

    def tmux(*arguments):
        return subprocess.check_output(
            ["tmux", "-L", socket, *arguments], env=environment,
            text=True, stderr=subprocess.STDOUT, timeout=5,
        ).strip()

    try:
        tmux("-f", "/dev/null", "new-session", "-d", "-s", "test", "/bin/bash --noprofile --norc")
        pane = tmux("display-message", "-p", "#{pane_id}")
        path, pid = tmux("display-message", "-p", "#{socket_path}|#{pid}").rsplit("|", 1)
        lock_file = Path(f"{path}.agent-indicator-{pid}.lock")
        inside = dict(environment, TMUX=f"{path},{pid},0", TMUX_PANE=pane, AGENT_TEST_ROOT=str(ROOT))
        tmux("set", "-g", "@agent-indicator-notification-enabled", "off")
        tmux("set", "-g", "@agent-indicator-style-active-window", "on")

        def update(state="needs-input", *extra):
            return subprocess.run(
                [str(ROOT / "scripts/agent-state.sh"), "--agent", "claude", "--state", state, *extra],
                env=inside, text=True, capture_output=True, timeout=5,
            )

        with tempfile.TemporaryDirectory(prefix="agent-lock-fixture-") as directory:
            fixture = Path(directory) / "writer.sh"
            fixture.write_text('''#!/usr/bin/env bash
set -euo pipefail
source "$AGENT_TEST_ROOT/scripts/state-lib.sh"
lock_agent_state "$@"
shift
case "$1" in
    hold|unlock)
        sleep 30 >/dev/null 2>&1 &
        child=$!
        [ "$1" != unlock ] || unlock_agent_state
        printf 'ready %s\n' "$child"
        read -r finish
        ;;
    record)
        printf 'begin %s\n' "$$" >> "$2"
        sleep 0.05
        printf 'end %s\n' "$$" >> "$2"
        ;;
esac
''')

            def launch(mode, *arguments):
                process = subprocess.Popen(
                    ["bash", str(fixture), mode, *arguments], env=inside,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                workers.append(process)
                return process

            def held(mode="hold"):
                process = launch(mode)
                assert select.select([process.stdout], [], [], 5)[0], "writer never acquired lock"
                line = process.stdout.readline().strip()
                assert line.startswith("ready "), (line, process.stderr.read())
                children.append(int(line.split()[1]))
                return process

            owner = held()
            started = time.monotonic()
            blocked = update()
            elapsed = time.monotonic() - started
            assert blocked.returncode == 0, (blocked.returncode, blocked.stderr)
            assert 1.5 <= elapsed < 4, elapsed
            assert not tmux("show-environment", "-g").count(f"TMUX_AGENT_PANE_{pane}_STATE="), "queued hook mutated state before acquiring the lock"
            print("PASS: contention returns promptly with the update still pending", flush=True)

            owner.kill()
            owner.wait(timeout=3)
            os.kill(children[-1], 0)  # The background child still has descriptor 9 open.
            for _ in range(100):
                if tmux("show-options", "-wqv", "@agent-indicator-state") == "needs-input":
                    break
                time.sleep(0.05)
            assert tmux("show-options", "-wqv", "@agent-indicator-state") == "needs-input", "queued event was lost"
            assert not list(Path(str(lock_file) + ".queue").iterdir())
            print("PASS: SIGKILL releases the lock even while a background child survives", flush=True)

            # Conditional transitions must follow earlier queued events, not race them.
            assert update("off").returncode == 0
            owner = held()
            assert update("needs-input").returncode == 0
            assert update("running", "--if-state", "needs-input").returncode == 0
            owner.stdin.write("finish\n")
            owner.stdin.flush()
            owner.wait(timeout=3)
            for _ in range(100):
                if tmux("show-options", "-wqv", "@agent-indicator-state") == "running":
                    break
                time.sleep(0.05)
            assert tmux("show-options", "-wqv", "@agent-indicator-state") == "running", "queued conditional events ran out of order"
            print("PASS: detached retries preserve transition order and conditional updates", flush=True)

            # A detached Codex hook cannot rediscover ancestry; it must retain the
            # pane resolved before returning to its runner.
            assert update("off").returncode == 0
            owner = held()
            completed = Path(directory) / "codex-returned"
            command = "env -u TMUX_PANE " + shlex.join([
                str(ROOT / "scripts/agent-state.sh"), "--agent", "codex", "--state", "done",
            ]) + " && touch " + shlex.quote(str(completed))
            tmux("send-keys", "-t", pane, "-l", command)
            tmux("send-keys", "-t", pane, "Enter")
            for _ in range(100):
                if completed.exists():
                    break
                time.sleep(0.05)
            assert completed.exists(), "Codex hook did not return while contended"
            owner.stdin.write("finish\n")
            owner.stdin.flush()
            owner.wait(timeout=3)
            for _ in range(100):
                if tmux("show-options", "-wqv", "@agent-indicator-state") == "done":
                    break
                time.sleep(0.05)
            assert tmux("show-options", "-wqv", "@agent-indicator-state") == "done", "deferred Codex hook lost its pane"
            print("PASS: deferred Codex updates retain their original pane", flush=True)

            # Explicit release must work before the writer and its children exit.
            owner = held("unlock")
            released = update("running")
            assert released.returncode == 0 and owner.poll() is None, released.stderr
            owner.stdin.write("finish\n")
            owner.stdin.flush()
            owner.wait(timeout=3)
            print("PASS: explicit unlock allows the next writer before notification work ends", flush=True)

            # Separate writers must never interleave a critical section.
            log = Path(directory) / "events"
            contenders = [launch("record", str(log)) for _ in range(6)]
            for contender in contenders:
                _, errors = contender.communicate(timeout=5)
                assert contender.returncode == 0, errors
            lines = log.read_text().splitlines()
            assert len(lines) == 12
            for index in range(0, len(lines), 2):
                assert lines[index].startswith("begin ")
                assert lines[index + 1] == lines[index].replace("begin ", "end ", 1), lines
            assert update("off").returncode == 0
            print("PASS: concurrent writers serialize and normal exits release the lock", flush=True)
    finally:
        for process in workers:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
        for child in children:
            try:
                os.kill(child, signal.SIGTERM)
            except ProcessLookupError:
                pass
        subprocess.run(["tmux", "-L", socket, "kill-server"], env=environment, capture_output=True)
        # Every test writer has exited and the test server is gone; no waiters remain.
        if lock_file is not None:
            queue = Path(str(lock_file) + ".queue")
            if queue.exists():
                for ticket in queue.iterdir():
                    try:
                        os.kill(int(ticket.read_text()), signal.SIGKILL)
                    except (ValueError, ProcessLookupError, FileNotFoundError):
                        pass
                shutil.rmtree(queue)
            lock_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
