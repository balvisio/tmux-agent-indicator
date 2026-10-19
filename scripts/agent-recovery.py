#!/usr/bin/env python3
"""Persist pane-specific resume commands alongside tmux-resurrect layouts."""
import base64
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

SCRIPT = Path(__file__).resolve()
PLUGINS = SCRIPT.parent.parent.parent
RESURRECT = PLUGINS / "tmux-resurrect"
CONTINUUM = PLUGINS / "tmux-continuum"
OPTION = "@agent_recovery"
ERRORS = (OSError, ValueError, KeyError, subprocess.SubprocessError)
STATUS = ("#{?@agent_recovery_warning,#[fg=black#,bg=yellow] "
          "#{@agent_recovery_warning} #[default],}")


def tmux(*args, **kwargs):
    return subprocess.check_output(["tmux", *args], text=True, stderr=subprocess.DEVNULL,
                                   timeout=10, **kwargs).rstrip("\n")


def option(name):
    return tmux("show-option", "-gqv", name)


def enabled():
    return (bool(os.environ.get("TMUX")) and option("@agent-indicator-recovery") == "on"
            and (RESURRECT / "resurrect.tmux").is_file()
            and (CONTINUUM / "continuum.tmux").is_file())


def identity():
    return tmux("display-message", "-p", "#{socket_path}|#{pid}|#{start_time}")


def state_dir(address=None):
    address = address if address is not None else identity().split("|")[0]
    key = hashlib.sha256(address.encode()).hexdigest()[:20]
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    path = root / "tmux-agent-indicator/recovery" / key
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def read_health(root):
    try:
        health = json.loads((root / "health.json").read_text())
        if isinstance(health, dict):
            return health
    except (OSError, ValueError):
        pass
    try:
        saved = (root / "last").stat().st_mtime
    except FileNotFoundError:
        saved = 0
    except OSError:
        saved = None
    return {"last_success": saved, "error": ""}


def write_health(root, health):
    temporary = root / "health.tmp"
    temporary.write_text(json.dumps(health) + "\n")
    temporary.replace(root / "health.json")


def show_health(health):
    warning = ""
    if health.get("error"):
        saved = health.get("last_success", 0)
        if saved:
            date_format = "%H:%M" if time.localtime(saved)[:3] == time.localtime()[:3] else "%m-%d %H:%M"
            last = time.strftime(date_format, time.localtime(saved))
        else:
            last = "unknown" if saved is None else "never"
        warning = f"⚠ Recovery: {health['error']} · last saved {last}"
    tmux("set-option", "-gq", "@agent_recovery_warning", warning)


def saved_successfully(root):
    health = {"last_success": time.time(), "error": ""}
    write_health(root, health)
    show_health(health)


def report_failure(action, error, root):
    # Error reporting must not turn a background failure into output in the active pane.
    operation = "restore" if action == "startup" else action
    if root is None and os.environ.get("TMUX"):
        try:
            # Locate the log even if the tmux command used to resolve the socket timed out.
            root = state_dir(os.environ["TMUX"].rsplit(",", 2)[0])
        except OSError:
            pass
    health = read_health(root) if root else {"last_success": None}
    health["error"] = operation + " failed"
    try:
        if root:
            write_health(root, health)
        directory = root or Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "tmux-agent-indicator/recovery"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        log = directory / "errors.log"
        if log.exists() and log.stat().st_size > 1024 * 1024:
            log.replace(directory / "errors.log.1")
        detail = getattr(error, "stderr", None) or ""
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        with log.open("a") as stream:
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S %z')} {operation}: {error}\n{detail}")
            if detail and not detail.endswith("\n"):
                stream.write("\n")
    except OSError:
        pass
    try:
        show_health(health)
    except ERRORS:
        pass


def processes():
    text = subprocess.check_output(["ps", "-axo", "pid=,ppid=,lstart=,command="], text=True)
    result = {}
    for line in text.splitlines():
        parts = line.split(None, 7)
        if len(parts) == 8:
            result[int(parts[0])] = (int(parts[1]), " ".join(parts[2:7]), parts[7])
    return result


def ancestry(pid, procs):
    seen = set()
    while pid in procs and pid not in seen:
        seen.add(pid)
        yield pid
        pid = procs[pid][0]


def panes():
    fields = ["session_name", "window_index", "pane_index", "pane_id", "pane_pid", "pane_dead", OPTION]
    output = tmux("list-panes", "-a", "-F", "\t".join("#{" + key + "}" for key in fields))
    return [line.split("\t", 6) for line in output.splitlines()]


def request_save():
    tmux("run-shell", "-b", shlex.join([str(SCRIPT), "save", "--background"]))


def record(agent, args):
    if not enabled():
        return
    if agent == "ssh":
        payload = {"kind": "ssh", "args": args, "cwd": os.getcwd()}
        owner = os.getppid()
        procs = processes()
    else:
        event = json.load(sys.stdin)
        if event.get("agent_id"):
            return  # In-process subagents must not replace the terminal conversation.
        if event.get("hook_event_name") == "SessionEnd":
            request_save()
            return
        session = event.get("session_id")
        cwd = event.get("cwd")
        if not isinstance(session, str) or not session or not isinstance(cwd, str):
            return
        payload = {"kind": agent, "session": session, "cwd": cwd}
        procs = processes()
        owner = None
        for pid in ancestry(os.getppid(), procs):
            try:
                words = shlex.split(procs[pid][2])
            except ValueError:
                continue
            # Native agents and Node/Python launchers both expose the executable in these positions.
            names = [Path(word).name for word in words[:2]]
            if agent in names or agent + ".js" in names:
                owner = pid
                break
        if owner is None:
            return
    parents = set(ancestry(owner, procs))
    pane = next((row[3] for row in panes() if int(row[4]) in parents and row[5] == "0"), None)
    if pane is None:
        return
    payload.update(pid=owner, started=procs[owner][1])
    payload["env"] = {key: os.environ[key] for key in
                      ("CODEX_HOME", "CODEX_CONFIG_DIR", "CLAUDE_CONFIG_DIR") if key in os.environ}
    tmux("set-option", "-pq", "-t", pane, OPTION, json.dumps(payload, separators=(",", ":")))
    request_save()


def resume_data(row, procs):
    try:
        data = json.loads(row[6])
        owner = data["pid"]
        if row[5] != "0" or procs[owner][1] != data["started"]:
            return None
        if int(row[4]) not in set(ancestry(owner, procs)):
            return None
        return {key: value for key, value in data.items() if key not in ("pid", "started")}
    except (ValueError, KeyError, TypeError):
        return None


def encode(data):
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()


def run_resurrect(action, stage, keep_existing=False):
    # Resurrect reads its directory from tmux. Our writer lock covers this temporary override.
    server = identity()
    settings = {"@resurrect-dir": stage}
    if keep_existing:
        settings["@resurrect-never-overwrite"] = "on"
    previous = {key: option(key) for key in settings}
    for key, value in settings.items():
        tmux("set-option", "-gq", key, value)
    try:
        return subprocess.run(["bash", str(RESURRECT / ("scripts/" + action + ".sh")), "quiet"],
                              check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
    finally:
        try:
            if identity() == server:
                for key, value in previous.items():
                    if value:
                        tmux("set-option", "-gq", key, value)
                    else:
                        tmux("set-option", "-gqu", key)
        except subprocess.SubprocessError:
            pass


def save(root):
    server = identity()
    before = panes()
    procs = processes()
    records = {(row[0], row[1], row[2]): resume_data(row, procs) for row in before}
    if not before:
        return
    with tempfile.TemporaryDirectory(prefix="agent-recovery-") as stage:
        run_resurrect("save", stage)
        raw = Path(stage, "last").read_text()
    if identity() != server:
        return
    after = panes()
    # Retry on the next event/tick if panes were added, moved or replaced while being saved.
    if before != after:
        return
    output = []
    saved_keys = set()
    grouped = set()
    saved_windows = set()
    for line in raw.splitlines():
        fields = line.split("\t")
        if fields[0] == "grouped_session":
            grouped.add(fields[1])
        elif fields[0] == "window":
            saved_windows.add((fields[1], fields[2]))
        if fields[0] == "pane":
            if len(fields) != 11:
                raise ValueError("invalid resurrect pane record")
            key = (fields[1], fields[2], fields[5])
            saved_keys.add(key)
            data = records.get(key)
            # Only our explicit resume commands are replayed; shell commands are never rerun.
            fields[10] = ":" + (shlex.join([str(SCRIPT), "resume", encode(data)]) if data else "")
            line = "\t".join(fields)
        output.append(line)
    expected = {key for key in records if key[0] not in grouped}
    if not saved_keys or saved_keys != expected or saved_windows != {key[:2] for key in expected}:
        raise ValueError("snapshot does not match the live panes")
    content = "\n".join(output) + "\n"
    latest = root / "last"
    if latest.exists() and latest.read_text() == content:
        return True
    name = "snapshot-" + str(time.time_ns()) + ".txt"
    target = root / name
    with target.open("x") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary = root / "last.tmp"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(name)
    temporary.replace(latest)
    for old in sorted(root.glob("snapshot-*.txt"))[:-20]:
        old.unlink()
    return True


def restore(root, manual=False):
    latest = root / "last"
    already_restored = option("@agent_recovery_restored") == identity()
    if not latest.exists() or (already_restored and not manual):
        return
    server = identity()
    with tempfile.TemporaryDirectory(prefix="agent-restore-") as stage:
        Path(stage, "last").write_text(latest.read_text())
        # The temporary path also avoids upstream's unquoted state-directory handling.
        run_resurrect("restore", stage, keep_existing=already_restored)
    if identity() == server:
        tmux("set-option", "-gq", "@agent_recovery_restored", server)


def resume(token):
    data = json.loads(base64.urlsafe_b64decode(token))
    kind = data["kind"]
    os.chdir(data["cwd"])
    for key, value in data.get("env", {}).items():
        if key in ("CODEX_HOME", "CODEX_CONFIG_DIR", "CLAUDE_CONFIG_DIR"):
            os.environ[key] = value
    if kind == "claude":
        command = ["claude", "--resume", data["session"]]
    elif kind == "codex":
        command = ["codex", "--no-daemon", "resume", data["session"]]
    elif kind == "ssh":
        command = [str(SCRIPT.parent / "ssh-reconnect.sh"), *data["args"]]
    else:
        raise ValueError("unknown recovery command")
    if enabled() and os.environ.get("TMUX_PANE"):
        # Preserve identity while the resumed CLI loads, before its SessionStart hook can run.
        data.update(pid=os.getpid(), started=processes()[os.getpid()][1])
        tmux("set-option", "-pq", "-t", os.environ["TMUX_PANE"], OPTION,
             json.dumps(data, separators=(",", ":")))
    os.execvp(command[0], command)


def setup():
    if not enabled():
        disable()
        return
    if not option("@agent_recovery_startup"):
        # Decide once, including when no snapshot exists or automatic restore is disabled.
        # Loading into an established server must not replay a saved layout.
        now = time.time()
        first_load = not option("@agent_recovery_loaded")
        started = int(identity().rsplit("|", 1)[1])
        max_delay = int(option("@continuum-restore-max-delay") or 10)
        should_restore = (first_load and now - started < max_delay
                          and option("@continuum-restore") == "on"
                          and not (Path.home() / "tmux_no_auto_restore").exists())
        tmux("set-option", "-gq", "@agent_recovery_loaded", str(now))
        tmux("set-option", "-gq", "@agent_recovery_startup", "pending" if should_restore else "ready")
    subprocess.run(["bash", str(RESURRECT / "resurrect.tmux")], check=True, capture_output=True)
    for action in ("save", "restore"):
        wrapper = str(SCRIPT.parent / ("recovery-" + action + ".sh"))
        tmux("set-option", "-gq", "@resurrect-" + action + "-script-path", wrapper)
        # Keep Resurrect's configured bindings, routing manual saves through the same atomic writer.
        keys = option("@resurrect-" + action) or ("C-s" if action == "save" else "C-r")
        for key in keys.split():
            tmux("bind-key", key, "run-shell", shlex.quote(wrapper) + (" manual" if action == "restore" else ""))
    command = shlex.join([str(SCRIPT), "save", "--background"])
    for event in ("window-layout-changed", "session-created", "session-closed", "session-renamed", "window-renamed"):
        for line in tmux("show-hooks", "-g", event).splitlines():
            if str(SCRIPT) in line:
                tmux("set-hook", "-gu", line.split(" ", 1)[0])
        tmux("set-hook", "-ag", event, "run-shell -b " + shlex.quote(command))
    # Snapshots are isolated by socket, so use Continuum's timer without its other-server guard.
    if not option("@continuum-save-last-timestamp"):
        tmux("set-option", "-gq", "@continuum-save-last-timestamp", str(int(time.time())))
    check = "#(" + str(CONTINUUM / "scripts/continuum_save.sh") + ")"
    status = option("status-right")
    tmux("set-option", "-gq", "status-right", check + STATUS + status.replace(check, "").replace(STATUS, ""))
    root = state_dir()
    with (root / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass  # The current writer will publish its health when it finishes.
        else:
            show_health(read_health(root))
    if option("@agent_recovery_startup") == "pending":
        tmux("run-shell", "-b", shlex.join([str(SCRIPT), "startup"]))


def disable(target=None):
    target = Path(target).resolve() if target else SCRIPT.parent.parent
    # An installer with a temporary HOME must never change an inherited live tmux server.
    try:
        directory = tmux("show-environment", "-g", "TMUX_AGENT_INDICATOR_DIR").partition("=")[2]
        if not directory or Path(directory).resolve() != target:
            return
    except subprocess.SubprocessError:
        return
    tmux("set-option", "-gq", "@agent-indicator-recovery", "off")
    tmux("set-option", "-gq", "@agent_recovery_startup", "ready")
    status = option("status-right")
    check = "#(" + str(target.parent / "tmux-continuum/scripts/continuum_save.sh") + ")"
    tmux("set-option", "-gq", "status-right", status.replace(check, "").replace(STATUS, ""))
    tmux("set-option", "-gqu", "@agent_recovery_warning")
    for event in ("window-layout-changed", "session-created", "session-closed", "session-renamed", "window-renamed"):
        for line in tmux("show-hooks", "-g", event).splitlines():
            if str(target / "scripts/agent-recovery.py") in line:
                tmux("set-hook", "-gu", line.split(" ", 1)[0])
    for line in tmux("list-keys", "-a").splitlines():
        if any(str(target / ("scripts/recovery-" + action + ".sh")) in line for action in ("save", "restore")):
            words = shlex.split(line)
            table = words.index("-T")
            tmux("unbind-key", "-T", words[table + 1], words[table + 2])


def main():
    original_umask = os.umask(0o077)
    action = sys.argv[1]
    background = action == "startup" or action == "save" and bool({"--background", "quiet"} & set(sys.argv[2:]))
    root = None
    # Keep the writer lock through error reporting so a failure cannot overwrite newer success.
    with ExitStack() as stack:
        try:
            if action == "resume":
                os.umask(original_umask)
                resume(sys.argv[2])
            elif action == "setup":
                setup()
            elif action == "disable":
                disable(sys.argv[2] if len(sys.argv) > 2 else None)
            elif action in ("hook", "ssh"):
                record(sys.argv[2] if action == "hook" else "ssh", sys.argv[2:])
            elif action in ("save", "restore", "startup") and enabled():
                root = state_dir()
                if action == "save":
                    time.sleep(0.5)
                elif action == "startup":
                    # Config loading may precede the initial session. Run after that command completes.
                    time.sleep(1)
                lock = stack.enter_context((root / "lock").open("a"))
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | (0 if action == "startup" else fcntl.LOCK_NB))
                except BlockingIOError:
                    return
                if action == "startup":
                    if option("@agent_recovery_startup") == "pending":
                        if enabled() and option("@continuum-restore") == "on" and not (Path.home() / "tmux_no_auto_restore").exists():
                            restore(root)
                        # On failure leave saves blocked so the previous snapshot is preserved.
                        tmux("set-option", "-gq", "@agent_recovery_startup", "ready")
                elif action == "save":
                    if option("@agent_recovery_startup") == "ready" and save(root):
                        saved_successfully(root)
                else:
                    restore(root, manual="manual" in sys.argv[2:])
                    tmux("set-option", "-gq", "@agent_recovery_startup", "ready")
        except ERRORS as error:
            if action in ("save", "restore", "startup"):
                report_failure(action, error, root)
            if not background:
                print(f"tmux-agent-indicator recovery: {error}", file=sys.stderr)
                return 1


if __name__ == "__main__":
    sys.exit(main())
