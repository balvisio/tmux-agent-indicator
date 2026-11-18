#!/usr/bin/env python3
"""Bound sweep process cost and exercise coalescing with real isolated tmux servers."""
import fcntl
from contextlib import closing
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
TMUX = shutil.which("tmux")


def until(check, message):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError(message)


def main():
    env = {key: value for key, value in os.environ.items() if key not in ("TMUX", "TMUX_PANE")}
    servers, workers, locks = [], [], []
    with tempfile.TemporaryDirectory(prefix="agent-sweep-test-") as directory:
        home = Path(directory)
        log, process_file, gate, seen = (home / name for name in ("calls", "processes", "gate", "seen"))
        (home / "tmux").write_text('#!/bin/sh\nprintf "tmux %s\\n" "$1" >> "$TEST_CALLS"\nexec "$TEST_TMUX" "$@"\n')
        (home / "ps").write_text('''#!/usr/bin/env python3
import os
from pathlib import Path
import time
with open(os.environ["TEST_CALLS"], "a") as stream:
    stream.write("ps\\n")
Path(os.environ["TEST_SEEN"]).touch()
while Path(os.environ["TEST_GATE"]).exists():
    time.sleep(0.05)
for line in Path(os.environ["TEST_PROCESSES"]).read_text().splitlines():
    pid, parent, tty, command = line.split(None, 3)
    print(pid, parent, tty, "Mon Nov 16 12:00:00 2026", command)
''')
        for name in ("tmux", "ps"):
            (home / name).chmod(0o755)
        inside = dict(env, HOME=directory, XDG_STATE_HOME=str(home / "state"),
                      XDG_CONFIG_HOME=str(home / "config"), PATH=directory + os.pathsep + env["PATH"],
                      TEST_TMUX=TMUX, TEST_CALLS=str(log), TEST_PROCESSES=str(process_file),
                      TEST_GATE=str(gate), TEST_SEEN=str(seen))

        def server():
            socket = "agent-sweep-test-" + uuid.uuid4().hex[:12]
            servers.append(socket)
            def tmux(*args):
                return subprocess.check_output([TMUX, "-L", socket, *args], env=inside, text=True, timeout=10).strip()
            tmux("-f", "/dev/null", "new-session", "-d", "-s", "test", "/bin/bash --noprofile --norc")
            path, pid = tmux("display-message", "-p", "#{socket_path}|#{pid}").rsplit("|", 1)
            locks.extend([Path(f"{path}.agent-indicator-{pid}.lock"), Path(f"{path}.agent-presence-{pid}.lock")])
            tmux("set", "-g", "@agent-indicator-notification-enabled", "off")
            tmux("set", "-g", "@agent-indicator-style-active-window", "on")
            tmux("set", "-g", "@agent-indicator-running-window-title-bg", "yellow")
            tmux("set", "-g", "@agent-indicator-running-window-title-fg", "black")
            for line in (ROOT / "config/agent-indicator.conf").read_text().splitlines():
                if line.startswith(("set -g automatic-rename-format ", "set -g window-status-format ",
                                    "set -g window-status-current-format ")):
                    tmux(*shlex.split(line))
            environment = dict(inside, TMUX=f"{path},{pid},0")
            subprocess.run(["bash", str(ROOT / "agent-indicator.tmux")], env=environment,
                           check=True, capture_output=True, timeout=15)
            subprocess.run([sys.executable, str(ROOT / "scripts/state-lock.py"), "--wait", "--barrier"],
                           env=environment, check=True, timeout=15)
            return tmux, environment, locks[-2]

        def start(environment, periodic=True):
            command = [sys.executable, str(ROOT / "scripts/agent-present.py"), "sweep"]
            if periodic:
                command.append("--if-idle")
            process = subprocess.Popen(command,
                                       env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            workers.append(process)
            return process

        def finish(process):
            output, errors = process.communicate(timeout=15)
            assert process.returncode == 0, (output, errors)

        def fixture(tmux):
            panes = [line.split("|") for line in tmux("list-panes", "-a", "-F",
                     "#{pane_id}|#{pane_pid}|#{pane_tty}|#{window_id}").splitlines()]
            process_file.write_text("".join(f"{pid} 1 {tty[5:] if tty.startswith('/dev/') else tty} claude\n"
                                            for _pane, pid, tty, _window in panes))
            return panes

        def state(tmux, pane, pid, value):
            for suffix, content in (("STATE", value), ("PID", pid), ("AGENT", "claude")):
                tmux("set-environment", "-g", f"TMUX_AGENT_PANE_{pane}_{suffix}", content)
            # This fixture publishes state directly, including the native
            # focus-admission flag normally written by render_window_state.
            tmux('set', '-wq', '-t', pane, '@agent-indicator-ack-needed', str(int(value == 'done')))

        def named(tmux, window, name, icons):
            def current():
                return (tmux("display", "-p", "-t", window, "#{window_name}") == name
                        and tmux("show-options", "-wqv", "-t", window, "@agent_label") == icons + " " + name)
            try:
                until(current, f"window {window} and menu label did not acquire {name!r}")
            except AssertionError as error:
                actual = tmux("display", "-p", "-t", window,
                              "#{window_name}|#{@agent_label}|#{@agent_name}|#{automatic-rename}")
                raise AssertionError(f"{error}; actual: {actual!r}") from error
            for option in ("window-status-format", "window-status-current-format"):
                title = tmux("display", "-p", "-t", window, "#{E:" + option + "}")
                assert name in title, title
            assert tmux("show-options", "-wqv", "-t", window, "@agent_icons") == icons

        try:
            tmux, environment, writer_lock = server()
            panes = fixture(tmux)
            log.write_text("")
            finish(start(environment))
            small = log.read_text().splitlines()
            for _ in range(7):
                tmux("new-window", "-d", "/bin/bash --noprofile --norc")
            panes = fixture(tmux)
            log.write_text("")
            finish(start(environment))
            large = log.read_text().splitlines()
            assert small.count("ps") == large.count("ps") == 1, (small, large)
            assert len(large) <= len(small) + 1 and len(large) < 25, (small, large)
            print(f"PASS: one process scan and bounded tmux launches for 1 and 8 windows ({len(large)} launches)", flush=True)

            # Codex names use live hook identities, including before a first prompt.
            codex_home = home / "custom codex"
            codex_home.mkdir()
            sessions_by_pane = {}
            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as db, db:
                db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, preview TEXT)")
                for pane, pid, tty, window in panes:
                    session = str(uuid.uuid4())
                    sessions_by_pane[pane] = session
                    db.execute("INSERT INTO threads VALUES (?, ?, ?)", (session, "Codex " + pane, "Generated preview"))
                    record = dict(kind="codex", session=session, pid=int(pid), started="Mon Nov 16 12:00:00 2026",
                                  env={"CODEX_HOME": str(codex_home)})
                    tmux("set", "-pq", "-t", pane, "@agent_recovery", json.dumps(record))
            process_file.write_text("".join(f"{pid} 1 {tty[5:]} codex --no-daemon\n" for pane, pid, tty, window in panes))
            log.write_text("")
            finish(start(environment))
            named_calls = log.read_text().splitlines()
            assert named_calls.count("ps") == 1 and len(named_calls) <= len(large), named_calls
            for pane, pid, tty, window in panes:
                named(tmux, window, "Codex " + pane, "🧠")
                assert not tmux("show-options", "-wqv", "-t", window, "automatic-rename"), "name refresh overrode inherited automatic naming"
            pane, pid, tty, window = panes[0]
            renamed = 'review | "quotes" $HOME #{pane_id} \\ next\nline'
            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as db, db:
                db.execute("UPDATE threads SET name=? WHERE id=?", (renamed, sessions_by_pane[pane]))
            finish(start(environment))
            assert tmux("show-options", "-wqv", "-t", window, "@agent_name") == renamed.replace("\n", " ")
            named(tmux, window, renamed.replace("\n", " ").replace("\\", "\\\\"), "🧠")
            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as db, db:
                db.execute("UPDATE threads SET name=NULL WHERE id=?", (sessions_by_pane[pane],))
            finish(start(environment))
            named(tmux, window, "codex", "🧠")
            for pane, pid, tty, window in panes:
                tmux("set", "-pqu", "-t", pane, "@agent_recovery")
            panes = fixture(tmux)
            print("PASS: Codex window names, bar and menu labels follow renames literally without extra sweep processes", flush=True)

            # Text reaches native naming literally, including dollars and formats.
            # Tmux escapes backslashes in names, even with manual rename-window.
            # Newlines become spaces so automatic naming accepts the title.
            pane, pid, tty, window = panes[0]
            name = 'one | "two" $HOME #{pane_id} \\ three\nnext'
            sessions = home / ".claude/sessions"
            sessions.mkdir(parents=True)
            (sessions / "1.json").write_text(json.dumps(dict(tmux="server." + pane, name=name, nameSource="user")))
            state(tmux, pane, pid, "running")
            finish(start(environment))
            assert tmux("show-options", "-wqv", "-t", window, "@agent_name") == name.replace("\n", " ")
            named(tmux, window, name.replace("\n", " ").replace("\\", "\\\\"), "🤖")
            assert tmux("show-options", "-wqv", "-t", window, "window-status-style") == "bg=yellow,fg=black"
            assert tmux("show-environment", "-g", f"TMUX_AGENT_WINDOW_{window}_ORIG_STATUS_STYLE").endswith("=bg=blue,fg=white")
            state(tmux, pane, pid, "needs-input")
            finish(start(environment))
            assert tmux("show-options", "-wqv", "-t", window, "@agent-indicator-state") == "needs-input"
            state(tmux, pane, pid, "done")
            finish(start(environment))
            assert tmux("show-options", "-wqv", "-t", window, "@agent-indicator-state") == "done"
            tmux("set-environment", "-gu", f"TMUX_AGENT_PANE_{pane}_STATE")
            finish(start(environment))
            assert tmux("show-options", "-wqv", "-t", window, "window-status-style") == "bg=blue,fg=white"
            print("PASS: batched labels preserve literal characters and active styles restore the idle baseline", flush=True)

            tmux("rename-window", "-t", window, "my manual title")
            named(tmux, window, "my manual title", "🤖")
            (sessions / "1.json").write_text(json.dumps(dict(tmux="server." + pane, name="new conversation name", nameSource="user")))
            finish(start(environment))
            named(tmux, window, "my manual title", "🤖")
            assert tmux("show-options", "-wv", "-t", window, "automatic-rename") == "off"
            inactive = panes[1][3]
            tmux("rename-window", "-t", inactive, "inactive manual title")
            named(tmux, inactive, "inactive manual title", "🤖")
            named(tmux, window, "my manual title", "🤖")
            tmux("set", "-w", "-t", inactive, "automatic-rename", "on")
            tmux("set", "-w", "-t", window, "automatic-rename", "on")
            named(tmux, window, "new conversation name", "🤖")
            for literal in ("0", "0-prefixed name", "__UNSET__"):
                (sessions / "1.json").write_text(json.dumps(dict(tmux="server." + pane, name=literal, nameSource="user")))
                finish(start(environment))
                named(tmux, window, literal, "🤖")
            print("PASS: manual renames win, inactive-window hooks target correctly, and zero/sentinel names stay literal", flush=True)

            peer, peer_pid, peer_tty = tmux("split-window", "-d", "-t", window, "-P", "-F",
                                           "#{pane_id}|#{pane_pid}|#{pane_tty}",
                                           "/bin/bash --noprofile --norc").split("|")
            peer_session = str(uuid.uuid4())
            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as db, db:
                db.execute("INSERT INTO threads VALUES (?, ?, ?)", (peer_session, "Codex reviewer", "preview"))
            tmux("set", "-pq", "-t", peer, "@agent_recovery", json.dumps(dict(
                kind="codex", session=peer_session, pid=int(peer_pid), started="Mon Nov 16 12:00:00 2026",
                env={"CODEX_HOME": str(codex_home)})))
            process_file.write_text(process_file.read_text() + f"{peer_pid} 1 {peer_tty[5:]} codex\n")
            finish(start(environment))
            named(tmux, window, "__UNSET__", "🤖🧠")
            (sessions / "1.json").write_text(json.dumps(dict(tmux="server." + pane, name="derived-name", nameSource="derived")))
            finish(start(environment))
            named(tmux, window, "Codex reviewer", "🤖🧠")
            tmux("kill-pane", "-t", peer)
            panes = fixture(tmux)
            finish(start(environment))
            named(tmux, window, "claude", "🤖")
            print("PASS: paired windows prefer Claude's explicit name and fall back to Codex or the agent type", flush=True)

            tmux("rename-window", "-t", inactive, "keep after exit")
            process_file.write_text("".join(f"{p} 1 {t[5:]} /bin/bash\n" for _, p, t, _ in panes))
            finish(start(environment))
            until(lambda: tmux("display", "-p", "-t", window, "#{window_name}") == "bash", "agent exit kept its name")
            assert tmux("display", "-p", "-t", inactive, "#{window_name}") == "keep after exit"
            for option in ("@agent_name", "@agent_icons", "@agent_label"):
                assert not tmux("show-options", "-wqv", "-t", window, option), option
            tmux("set", "-w", "-t", inactive, "automatic-rename", "on")
            fixture(tmux)
            finish(start(environment))
            print("PASS: agent exit restores command names, preserves manual titles and removes agent decoration", flush=True)

            # Two concurrent requests must only do one process scan.
            other_tmux, other_env, _ = server()
            other_env["TEST_GATE"] = str(home / "no-gate")
            gate.touch()
            seen.unlink()
            log.write_text("")
            winner = start(environment)
            until(seen.exists, "sweep never reached process scan")
            finish(start(environment))
            assert winner.poll() is None and log.read_text().splitlines().count("ps") == 1
            explicit = start(environment, periodic=False)
            time.sleep(.2)
            assert explicit.poll() is None, "explicit refresh skipped an in-flight scan"
            gate.unlink()
            finish(winner)
            finish(explicit)
            assert log.read_text().splitlines().count("ps") == 2
            print("PASS: periodic scans coalesce; explicit refreshes wait and rescan", flush=True)

            # Keep the scan's owner alive through a >2-second FIFO lock wait.
            # A newer pane state must be read only after acquiring that lock.
            with writer_lock.open("a+") as lock:
                fcntl.lockf(lock, fcntl.LOCK_EX)
                log.write_text("")
                winner = start(environment)
                queue = Path(str(writer_lock) + ".queue")
                until(lambda: queue.exists() and any(queue.iterdir()), "sweep never queued for apply")
                time.sleep(2.2)
                finish(start(environment))
                assert winner.poll() is None and log.read_text().splitlines().count("ps") == 1
                finish(start(other_env))
                assert winner.poll() is None, "a different server released the sweep lock"
                state(tmux, pane, pid, "needs-input")
            finish(winner)
            assert tmux("show-options", "-wqv", "-t", window, "@agent-indicator-state") == "needs-input"
            print("PASS: contention retains coalescing, independent servers run, and newer state survives", flush=True)

            # A pane replacement between scan and apply must not acquire the old
            # process's label or retain its state.
            tmux("set", "-wu", "-t", window, "@agent_label")
            gate.touch()
            seen.unlink()
            winner = start(environment)
            until(seen.exists, "sweep never reached process scan")
            tmux("respawn-pane", "-k", "-t", pane, "/bin/bash --noprofile --norc")
            gate.unlink()
            finish(winner)
            assert not tmux("show-options", "-wqv", "-t", window, "@agent_label")
            assert tmux("show-options", "-wqv", "-t", window, "@agent-indicator-state") == "idle"
            print("PASS: process replacement invalidates stale presence data and clears old state", flush=True)

            # Focus hooks must not hold the selecting client's command queue.
            subprocess.run(["bash", str(ROOT / "agent-indicator.tmux")], env=environment, check=True, timeout=15)
            until(lambda: not any(Path(str(writer_lock) + ".queue").iterdir()), "initial reconciliation remained queued")
            panes = fixture(tmux)
            first, second = panes[:2]
            with writer_lock.open("a+") as lock:
                fcntl.lockf(lock, fcntl.LOCK_EX)
                state(tmux, first[0], first[1], "done")
                state(tmux, second[0], second[1], "done")
                start_time = time.monotonic()
                tmux("select-window", "-t", first[3], ";", "select-window", "-t", second[3])
                assert time.monotonic() - start_time < 1.5, "navigation waited for the state writer"
            def acknowledged():
                return f"TMUX_AGENT_PANE_{second[0]}_STATE=" not in tmux("show-environment", "-g")
            until(acknowledged, "focused window was not acknowledged")
            assert tmux("show-environment", "-g", f"TMUX_AGENT_PANE_{first[0]}_STATE").endswith("=done")
            assert "run-shell -b" in tmux("show-hooks", "-g", "after-select-window")
            print("PASS: navigation stays responsive under contention and stale focus events do not acknowledge other windows", flush=True)
        finally:
            gate.unlink(missing_ok=True)
            for process in workers:
                if process.poll() is None:
                    process.communicate(timeout=15)
            for socket in servers:
                # Drain asynchronous focus writers before server shutdown can
                # kill a waiter between ticket creation and normal cleanup.
                barrier = shlex.join([sys.executable, str(ROOT / "scripts/state-lock.py"),
                                      "--wait", "--barrier"])
                subprocess.run([TMUX, "-L", socket, "run-shell", barrier], env=inside,
                               capture_output=True, timeout=20)
                subprocess.run([TMUX, "-L", socket, "kill-server"], env=env, capture_output=True)
            # Give detached focus writers a chance to observe the stopped server.
            for path in locks:
                queue = Path(str(path) + ".queue")
                if queue.exists():
                    until(lambda: not any(queue.iterdir()), "test left a queued writer")
                    queue.rmdir()
                path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
