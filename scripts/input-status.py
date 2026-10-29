#!/usr/bin/env python3
"""Observe unanswered questions without reading input or sending keystrokes."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


def process(pid):
    if sys.platform == "linux":
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[1]), fields[19]
    # Claude records macOS process start times in UTC.
    output = subprocess.check_output(
        ["ps", "-p", str(pid), "-o", "ppid=", "-o", "lstart="],
        env=dict(os.environ, TZ="UTC", LC_ALL="C"),
        text=True, stderr=subprocess.DEVNULL, timeout=1).strip().split(None, 1)
    return int(output[0]), " ".join(output[1].split())


def pid_domain():
    if sys.platform == "linux":
        machine = Path("/etc/machine-id").read_text().strip()
        return "linux:" + machine + ":" + os.readlink("/proc/self/ns/pid")
    return sys.platform


def claude_status(question, pane_pid):
    session = question["session"]
    if not session:
        return "unknown"
    domain = pid_domain()
    for path in (Path(question["config_dir"]) / "sessions").glob("*.json"):
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or data.get("sessionId") != session:
                continue
            # Host and container can share session files and have overlapping PIDs.
            if data.get("pidDomain") != domain:
                continue
            pid = int(data["pid"])
            parent, started = process(pid)
            if started != " ".join(data["procStart"].split()):
                continue
            for _ in range(40):
                if str(pid) == pane_pid:
                    break
                if parent <= 1:
                    break
                pid = parent
                parent, _ = process(pid)
            if str(pid) != pane_pid:
                continue
            if data.get("status") == "waiting":
                return "waiting"
            # A busy value from before PermissionRequest is not an answer.
            if (data.get("status") in ("busy", "idle")
                    and float(data["statusUpdatedAt"]) >= question["received"] * 1000):
                return "answered"
        except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError):
            continue
    return "unknown"


def codex_screen(text, cursor_y, cursor_visible):
    """Recognize the active dialog or working composer, never scrollback alone."""
    lines = text.splitlines()
    footer = " ".join(line.strip() for line in lines[-5:]).lower()
    if ("enter to confirm" in footer and "esc to cancel" in footer
            or "enter to submit" in footer and "esc to interrupt" in footer):
        return "waiting"
    if not cursor_visible or not 0 <= cursor_y < len(lines):
        return "unknown"
    composer = lines[cursor_y].lstrip()
    if not composer.startswith("› ") or re.match(r"›\s+\d+\.", composer):
        return "unknown"
    # The working status belongs above the normal composer. Question footers
    # also mention interrupt, but appear below their input instead.
    above = " ".join(lines[max(0, cursor_y - 6):cursor_y])
    if re.search(r"\([^()]*\b\d+[smh][^()]*[•·]\s*esc to interrupt\)", above):
        return "answered"
    return "unknown"


def status(question, pane, info, tmux):
    if question["agent"] == "claude":
        try:
            return claude_status(question, info["pid"])
        except OSError:
            return "unknown"
    if question["agent"] == "codex":
        try:
            meta = tmux("display-message", "-p", "-t", pane,
                        "#{pane_current_command}|#{pane_in_mode}|#{cursor_y}|#{cursor_flag}|#{pane_pid}|#{pane_tty}").split("|")
            if meta[1] != "0":
                return "unknown"
            if meta[0] != "codex":
                if meta[0] != "node":
                    return "unknown"
                commands = subprocess.check_output(
                    ["bash", str(Path(__file__).with_name("pane-processes.sh")), meta[4], meta[5]],
                    text=True, stderr=subprocess.DEVNULL, timeout=2)
                if not any(Path(word).name == "codex" for line in commands.splitlines()
                           for word in line.split()[:2]):
                    return "unknown"
            # Keep blank leading rows: cursor_y refers to the untrimmed grid.
            text = subprocess.check_output(["tmux", "capture-pane", "-p", "-t", pane],
                                           text=True, stderr=subprocess.DEVNULL, timeout=2)
            result = codex_screen(text, int(meta[2]), meta[3] == "1")
            # Before the grace expires, the working composer may precede the
            # dialog. At the deadline it also covers answers between samples.
            if (result == "answered" and not question.get("seen_waiting")
                    and time.monotonic() < question["deadline"]):
                return "unknown"
            return result
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return "unknown"
    return "unknown"


class Questions:
    def __init__(self, records, live, delay, probe, now=None):
        self.records = records
        self.live = live
        self.delay = delay
        self.probe = probe
        self.now = time.monotonic() if now is None else now
        self.started = {}
        self.silent = False
        for pane, question in list(records.items()):
            if pane not in live or question["pid"] != live[pane]["pid"]:
                del records[pane]

    def transition(self, pane, agent, state, payload):
        event = payload.get("hook_event_name", "")
        question = self.records.get(pane)
        tool = payload.get("tool_use_id") or payload.get("call_id") or ""
        if state == "needs-input" and event == "PermissionRequest":
            if self.delay == 0:
                self.records.pop(pane, None)
                return state
            if question and tool and question["tool"] == tool and question["session"] == payload.get("session_id"):
                self.silent = True
                return "needs-input" if question["shown"] else "running"
            self.silent = self.delay > 0
            self.records[pane] = dict(
                token=uuid.uuid4().hex, pid=self.live[pane]["pid"], agent=agent,
                session=payload.get("session_id", ""), tool=tool,
                received=payload.get("received_at", time.time()),
                config_dir=payload.get("claude_config_dir", str(Path.home() / ".claude")),
                deadline=self.now + self.delay, next_check=self.now + self.delay + 5,
                shown=False, seen_waiting=False)
            self.started[pane] = self.records[pane]
            return "running"
        if not question:
            return state
        if state == "running" and event in ("PostToolUse", "PostToolUseFailure"):
            observed = self.probe(question, pane, self.live[pane])
            matching = bool(tool and tool == question["tool"]
                            and payload.get("session_id") == question["session"])
            if observed == "waiting":
                question["seen_waiting"] = True
                return None
            if observed != "answered" and not matching:
                return None
        self.records.pop(pane, None)
        return state

    def check(self):
        changes = []
        for pane, question in list(self.records.items()):
            if self.now >= question["next_check"]:
                observed = self.probe(question, pane, self.live[pane])
                if observed == "answered":
                    del self.records[pane]
                    changes.append((pane, question["agent"], "running", False))
                    continue
                if observed == "waiting":
                    question["seen_waiting"] = True
                if not question["shown"] and self.now >= question["deadline"]:
                    question["shown"] = True
                    changes.append((pane, question["agent"], "needs-input", True))
                question["next_check"] = self.now + 5
        return changes

    def observed(self, pane, token, pid, result, seen_waiting):
        question = self.records.get(pane)
        if not question or question["token"] != token or question["pid"] != pid:
            return []
        question["seen_waiting"] |= seen_waiting == "1"
        if result == "answered":
            del self.records[pane]
            return [(pane, question["agent"], "running", False)]
        if self.now >= question["deadline"] and not question["shown"]:
            question["shown"] = True
            question["next_check"] = self.now + 5
            return [(pane, question["agent"], "needs-input", True)]
        return []


def tmux(*args):
    return subprocess.check_output(["tmux", *args], text=True,
                                   stderr=subprocess.DEVNULL, timeout=2).strip()


def watch(pane, question):
    """Read during the grace period without holding the state writer lock."""
    while True:
        identity = tmux("display-message", "-p", "-t", pane,
                        "#{pane_pid}|#{pane_dead}|#{@agent-indicator-input-pending}")
        if identity != f'{question["pid"]}|0|{question["token"]}':
            return
        # Decide whether this is the final probe before reading the screen. If
        # the read crosses the deadline, take another sample before alerting.
        remaining = question["deadline"] - time.monotonic()
        result = status(question, pane, {"pid": question["pid"]}, tmux)
        if result == "waiting":
            question["seen_waiting"] = True
        if result == "answered" or remaining <= 0:
            # The writer validates the token and process again: a hook can cancel
            # or replace the question between observing it and acquiring the lock.
            subprocess.run(["bash", str(Path(__file__).with_name("window-state.sh")),
                            "--input-result", pane, question["token"], question["pid"],
                            result, str(int(question["seen_waiting"]))], check=True)
            return
        time.sleep(min(.25, remaining))


if __name__ == "__main__":
    try:
        if len(sys.argv) != 4 or sys.argv[1] != "watch":
            raise ValueError("usage: input-status.py watch PANE QUESTION_JSON")
        watch(sys.argv[2], json.loads(sys.argv[3]))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        # The regular sweep still checks a question if its short-lived reader fails.
        sys.exit(1)
