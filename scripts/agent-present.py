#!/usr/bin/env python3
"""One process scan per server sweep; apply with fresh state under the writer lock."""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
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
                "pane_tty", "@ssh_reconnect_host", "@ssh_reconnect_pid"])


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
               "@agent_present", "@agent_label", "@ssh_host", "@agent-indicator-state",
               "@agent-indicator-review-pending", "@agent-indicator-shell-running")
    marker = "agent-style-" + uuid.uuid4().hex
    commands = []
    for window in windows:
        for option in options:
            commands.extend([["show-options", "-wqv", "-t", window, option],
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
    for pane, pid, dead, window, tty, marker_host, marker_pid in panes:
        for key, value in (("pid", pid), ("dead", dead), ("window", window),
                           ("marker", marker_host), ("marker_pid", marker_pid)):
            emit("pane", pane, key, value)
    emit("active", "", "", state.tmux("display-message", "-p", "#{window_id}"))
    emit("ready", "", "", "")


def process_snapshot():
    raw = subprocess.check_output(["ps", "-axo", "pid=,ppid=,tty=,command="], text=True, timeout=5)
    processes = {}
    for line in raw.splitlines():
        fields = line.split(None, 3)
        if len(fields) == 4 and fields[0].isdigit() and fields[1].isdigit():
            pid, parent, tty, command = fields
            processes[pid] = (parent, tty[5:] if tty.startswith("/dev/") else tty, command)
    return processes


def pane_commands(processes, root, tty):
    tty = tty[5:] if tty.startswith("/dev/") else tty
    if not tty:
        return
    for pid, (_parent, terminal, command) in processes.items():
        if terminal != tty:
            continue
        for _ in range(100):
            if pid == root:
                yield command
                break
            if pid not in processes or processes[pid][1] != tty:
                break
            parent = processes[pid][0]
            if pid == parent or pid == "1":
                break
            pid = parent


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


def presence(panes, processes, names):
    records = []
    for window in dict.fromkeys(p[3] for p in panes):
        members = [p for p in panes if p[3] == window]
        kinds, name, host = [], "", ""
        for pane, pid, dead, _window, tty, marker, marker_pid in members:
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
                if agent == "claude" and not name:
                    name = names.get(pane, "").replace("\0", "")
            if not host:
                host = next((value for words in commands if (value := ssh_host(words))), "") or marker
        icons = "".join("🤖" if agent == "claude" else "🧠" for agent in kinds)
        label = (icons + " " + (name or (kinds[0] if len(kinds) == 1 else "agents"))) if kinds else ""
        records.extend(["window", window, signature(members), label, host])
    return "\0".join(records) + "\0"


def sweep(if_idle=False):
    server = state.tmux("display-message", "-p", "#{socket_path}|#{pid}")
    socket, pid = server.rsplit("|", 1)
    # Periodic status refreshes exit rather than accumulating background scans.
    # Explicit refreshes (including SSH lifecycle events) wait for a fresh scan.
    # This lock is separate from the FIFO writer lock and released on death.
    fd = os.open(f"{socket}.agent-presence-{pid}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.lockf(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if if_idle else 0))
        except BlockingIOError:
            return
        if not if_idle and state.tmux("display-message", "-p", "#{socket_path}|#{pid}") != server:
            return
        panes = pane_rows()
        data = presence(panes, process_snapshot(), claude_names())
        if state.tmux("display-message", "-p", "#{socket_path}|#{pid}") != server:
            return
        subprocess.run([str(DIRECTORY / "window-state.sh"), "--sweep"], input=data,
                       text=True, check=True)
        for session, owner in rows(["list-sessions"], ["session_id", "@ssh_passthrough_pane"]):
            if owner:
                subprocess.run([str(DIRECTORY / "ssh-passthrough.sh"), "check", session], check=True)


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["snapshot"]:
            snapshot()
        elif sys.argv[1:] in (["sweep"], ["sweep", "--if-idle"]):
            sweep(if_idle="--if-idle" in sys.argv)
        else:
            sys.exit("usage: agent-present.sh sweep [--if-idle]")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"tmux-agent-indicator: presence sweep failed: {error}", file=sys.stderr)
        sys.exit(1)
