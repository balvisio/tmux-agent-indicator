#!/usr/bin/env python3
"""One process scan per server sweep; apply with fresh state under the writer lock."""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from contextlib import closing
import sqlite3
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime
import scheduled
DIRECTORY = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("notification_state", DIRECTORY / "notification-state.py")
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


def rows(command, fields):
    """Format values may contain pipes, quotes or newlines, including session names."""
    separator = "agent-presence-" + uuid.uuid4().hex
    raw = state.tmux(*command, "-F", separator.join("#{" + field + "}" for field in fields) + separator)
    values = raw.split(separator)
    result = []
    for index in range(0, len(values) - 1, len(fields)):
        row = values[index:index + len(fields)]
        row[0] = row[0].lstrip("\n")
        result.append(row)
    return result


def pane_rows():
    return rows(["list-panes", "-a"], ["pane_id", "pane_pid", "pane_dead", "window_id",
                "pane_tty", "@ssh_reconnect_host", "@ssh_reconnect_pid", "@agent_recovery"])


def signature(panes):
    # Include reconnect ownership too: a new launcher can keep the same shell
    # process while changing destination during an unlocked process scan.
    return hashlib.sha256(json.dumps(list(panes), separators=(",", ":")).encode()).hexdigest()


def emit(*values):
    sys.stdout.write("\0".join(values) + "\0")


def snapshot():
    """Raw local styles distinguish inherited values from explicit empty values."""
    panes = pane_rows()
    windows = dict.fromkeys(pane[3] for pane in panes)
    options = ("window-status-style", "window-status-current-style", "pane-active-border-style",
               "@agent_present", "@agent_label", "@agent_icons", "@agent_name", "window_name", "automatic-rename",
               "@ssh_host", "@agent-indicator-state",
               "@agent-indicator-review-pending", "@agent-indicator-shell-running",
               "@agent-indicator-ack-needed", "@agent-indicator-layout", "@agent-indicator-repaint-needed")
    marker = "agent-style-" + uuid.uuid4().hex
    commands = []
    for window in windows:
        for option in options:
            read = (["display-message", "-p", "-t", window, "#{window_name}"]
                    if option == "window_name" else ["show-options", "-wqv", "-t", window, option])
            commands.extend([read,
                             ["display-message", "-p", marker]])
    # Stream command text: large servers must not exceed tmux's argv-message limit.
    raw = state.tmux_commands(commands)
    values = iter(raw.split(marker + "\n")[:-1])
    for window in windows:
        emit("window", window, "members", " ".join(p[0] for p in panes if p[3] == window))
        emit("window", window, "signature", signature(p for p in panes if p[3] == window))
        for option in options:
            value = next(values)
            emit("window", window, option, value[:-1] if value.endswith("\n") else "")
    for pane, pid, dead, window, tty, marker_host, marker_pid, _conversation in panes:
        for key, value in (("pid", pid), ("dead", dead), ("window", window),
                           ("marker", marker_host), ("marker_pid", marker_pid)):
            emit("pane", pane, key, value)
    emit("active", "", "", state.tmux("display-message", "-p", "#{window_id}"))
    emit("ready", "", "", "")


def process_snapshot():
    raw = subprocess.check_output(["ps", "-axo", "pid=,ppid=,tty=,lstart=,command="], text=True, timeout=5)
    processes = {}
    for line in raw.splitlines():
        fields = line.split(None, 8)
        if len(fields) == 9 and fields[0].isdigit() and fields[1].isdigit():
            pid, parent, tty = fields[:3]
            processes[pid] = (parent, tty[5:] if tty.startswith("/dev/") else tty,
                              fields[8], " ".join(fields[3:8]))
    return processes


def pane_processes(processes, root, tty):
    tty = tty[5:] if tty.startswith("/dev/") else tty
    if not tty:
        return
    for owner, (_parent, terminal, _command, _started) in processes.items():
        pid = owner
        if terminal != tty:
            continue
        for _ in range(100):
            if pid == root:
                yield owner
                break
            if pid not in processes or processes[pid][1] != tty:
                break
            parent = processes[pid][0]
            if pid == parent or pid == "1":
                break
            pid = parent


def pane_commands(processes, root, tty):
    return (processes[pid][2] for pid in pane_processes(processes, root, tty))


def ssh_host(words):
    if not words or words[0].rsplit("/", 1)[-1] != "ssh":
        return ""
    arguments = iter(words[1:])
    for arg in arguments:
        if len(arg) == 2 and arg[0] == "-" and arg[1] in "bcDEeFIiJLlmOopQRSWw":
            next(arguments, None)
        elif not arg.startswith("-"):
            return arg
    return ""


def claude_names():
    files = []
    for path in (Path.home() / ".claude/sessions").glob("*.json"):
        try:
            files.append((path.stat().st_mtime, path))
        except OSError:
            pass
    names = {}
    for _, path in sorted(files, reverse=True):
        try:
            data = json.loads(path.read_text())
            pane = str(data.get("tmux", "")).rsplit(".", 1)[-1]
            if pane not in names:
                names[pane] = str(data.get("name", "")) if data.get("nameSource", "derived") != "derived" else ""
        except (OSError, ValueError, AttributeError):
            pass
    return names


def codex_names(panes, processes):
    """Read explicit names once per Codex home, using live hook-owned identities."""
    homes = {}
    for pane, root, dead, _window, tty, _marker, _marker_pid, raw in panes:
        if dead != "0" or not raw:
            continue
        try:
            record = json.loads(raw)
            owner = str(record["pid"])
            process = processes.get(owner)
            if (record["kind"] != "codex" or not process
                    or not runtime.same_start(process[3], record["started"])
                    or owner not in pane_processes(processes, root, tty)
                    or "codex" not in [word.rsplit("/", 1)[-1] for word in process[2].split()[:2]]):
                continue
            session = str(uuid.UUID(record["session"]))
            home = Path(record.get("env", {}).get("CODEX_HOME")
                        or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
            homes.setdefault(home, {})[pane] = session
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    names = {}
    for home, sessions in homes.items():
        try:
            databases = [path for path in home.glob("state_*.sqlite") if path.stem[6:].isdigit()]
            if not databases:
                continue
            database = max(databases, key=lambda path: int(path.stem[6:]))
            with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True,
                                         timeout=0.05)) as db:
                placeholders = ",".join("?" for _ in sessions)
                saved = dict(db.execute(f"SELECT id, name FROM threads WHERE id IN ({placeholders})",
                                        list(sessions.values())))
            for pane, session in sessions.items():
                name = saved.get(session)
                if isinstance(name, str) and name:
                    names[pane] = name.replace("\0", "")
        except (OSError, ValueError, sqlite3.Error):
            # Missing names must not interrupt presence or state reconciliation.
            continue
    return names


def alive(pid):
    try:
        if not pid.isdigit() or int(pid) <= 0:
            return False
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def presence(panes, processes, names, codex=None):
    records = []
    for window in dict.fromkeys(p[3] for p in panes):
        members = [p for p in panes if p[3] == window]
        kinds, window_names, host = [], {}, ""
        for pane, pid, dead, _window, tty, marker, marker_pid, _conversation in members:
            if marker and not alive(marker_pid):
                records.extend(["stale", pane, pid, marker, marker_pid])
                marker = ""
            if dead != "0":
                continue
            commands = [command.split() for command in pane_commands(processes, pid, tty)]
            agents = sorted({word.rsplit("/", 1)[-1] for words in commands for word in words[:2]
                             if word.rsplit("/", 1)[-1] in ("claude", "codex")})
            for agent in agents:
                if agent not in kinds:
                    kinds.append(agent)
                if not window_names.get(agent):
                    source = names if agent == "claude" else codex or {}
                    window_names[agent] = source.get(pane, "").replace("\0", "")
            if not host:
                host = next((value for words in commands if (value := ssh_host(words))), "") or marker
        icons = "".join("🤖" if agent == "claude" else "🧠" for agent in kinds)
        name = window_names.get("claude") or window_names.get("codex")
        name = name or (kinds[0] if len(kinds) == 1 else "agents")
        # Native automatic naming rejects control characters, leaving the old
        # window name in place. Conversation titles become one line in tmux.
        name = "".join(" " if ord(char) < 32 or 127 <= ord(char) < 160 else char for char in name)
        label = (icons + " " + name) if kinds else ""
        records.extend(["window", window, signature(members), label, host])
    return "\0".join(records) + "\0"


def sweep(if_idle=False):
    server = runtime.identity()
    socket, pid = server['socket'], server['pid']
    # Periodic status refreshes exit rather than accumulating background scans.
    # Explicit refreshes (including SSH lifecycle events) wait for a fresh scan.
    # This lock is separate from the FIFO writer lock and released on death.
    fd = os.open(f"{socket}.agent-presence-{pid}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.lockf(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if if_idle else 0))
        except BlockingIOError:
            return False
        if not runtime.server_alive(server):
            return False
        panes = pane_rows()
        processes = process_snapshot()
        data = presence(panes, processes, claude_names(), codex_names(panes, processes))
        if not runtime.server_alive(server):
            return False
        subprocess.run([str(DIRECTORY / "window-state.sh"), "--sweep"], input=data,
                       text=True, check=True)
        for session, owner in rows(["list-sessions"], ["session_id", "@ssh_passthrough_pane"]):
            if owner:
                subprocess.run([str(DIRECTORY / "ssh-passthrough.sh"), "check", session], check=True)
        return True


if __name__ == "__main__":
    token = None
    success = False
    try:
        if '--scheduled' in sys.argv:
            index = sys.argv.index('--scheduled')
            token = sys.argv[index + 1]
            del sys.argv[index:index + 2]
            if not scheduled.owns('sweep', token):
                sys.exit(0)
        if sys.argv[1:] == ["snapshot"]:
            snapshot()
        elif sys.argv[1:] == ['setup']:
            runtime.remember(runtime.identity())
            runtime.private_directory('arrivals')
            scheduled.install('sweep', [DIRECTORY / 'agent-present.sh', 'sweep', '--if-idle'])
        elif sys.argv[1:] in (["sweep"], ["sweep", "--if-idle"]):
            success = sweep(if_idle="--if-idle" in sys.argv)
        else:
            sys.exit("usage: agent-present.sh sweep [--if-idle]")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"tmux-agent-indicator: presence sweep failed: {error}", file=sys.stderr)
        sys.exit(1)
    finally:
        try:
            scheduled.finish('sweep', token, 5 if success else 1)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # A dead server cannot consume the result; a live gate expires.
