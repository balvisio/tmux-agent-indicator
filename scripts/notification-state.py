#!/usr/bin/env python3
"""Window completion accounting. All mutations run under state-lock.py.

Only IDs and message digests are stored in tmux, never conversation text.
A review reservation survives delivery and is released by its matching turn.
"""
from collections import Counter
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("input_status", Path(__file__).with_name("input-status.py"))
input_status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(input_status)

OPTION = "@agent-indicator-notifications"
TERMINAL = {"completed", "skipped", "cancelled", "paused"}


def exchange_id(session, prompt):
    return hashlib.sha256((session + "\0" + prompt).encode()).hexdigest()


def message_hash(text):
    return hashlib.sha256(text.strip().encode()).hexdigest()


def hook_data(payload):
    result = {key: payload.get(key, "") for key in
              ("session_id", "prompt_id", "turn_id", "hook_event_name", "tool_use_id", "call_id")}
    result["received_at"] = input_status.time.time()
    result["claude_config_dir"] = os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
    prompt = payload.get("prompt") or ""
    result["task_notification"] = prompt.lstrip().startswith("<task-notification>")
    intervals = {(0, len(prompt))} if prompt else set()
    # Codex may process several queued messages in one turn. Match each complete
    # forwarded exchange, including messages containing a quoted kibitz header.
    starts = [m.start() for m in re.finditer(r"\[kibitz from:claude\]", prompt)]
    ends = starts[1:] + [len(prompt)]
    for start in starts:
        for end in ends:
            if end > start:
                intervals.add((start, end))
    result["message_hashes"] = [message_hash(prompt[start:end]) for start, end in sorted(intervals)]
    return result


class Notifications:
    def __init__(self, data, live, questions=None):
        self.data = data
        self.live = live
        self.questions = questions
        data.setdefault("epoch", uuid.uuid4().hex)
        self.panes = data.setdefault("panes", {})
        self.reviews = data.setdefault("reviews", {})
        self.windows = data.setdefault("windows", {})
        for pane, info in live.items():
            self.windows.setdefault(info["window"], {"sequence": 0, "pending": {}})
            old = self.panes.get(pane)
            if old and old["pid"] != info["pid"]:
                self.remove(pane)
            self.panes.setdefault(pane, {"pid": info["pid"], "state": info.get("state", "idle"),
                                         "turn": "", "agent": "agent", "last_done": ""})
            # Completions do not travel into a different window when panes move.
            for wid, win in self.windows.items():
                if wid != info["window"]:
                    win["pending"].pop(pane, None)
        for pane in list(self.panes):
            if pane not in live:
                self.remove(pane)
        live_windows = {info["window"] for info in live.values()}
        for wid in list(self.windows):
            if wid not in live_windows:
                del self.windows[wid]
        for key, review in list(self.reviews.items()):
            host, reviewer = review["host"], review["reviewer"]
            if (host not in live or reviewer not in live
                    or live[host]["pid"] != review["host_pid"]
                    or live[reviewer]["pid"] != review["reviewer_pid"]
                    or live[reviewer].get("host") != host):
                self.discard_review(review)
                del self.reviews[key]

    def remove(self, pane):
        self.panes.pop(pane, None)
        for window in self.windows.values():
            window["pending"].pop(pane, None)

    def discard_review(self, review):
        # Losing an agent is not evidence that its review succeeded.
        for window in self.windows.values():
            for pane in (review["host"], review["reviewer"]):
                window["pending"].pop(pane, None)

    def reviewer(self, host):
        return next((pane for pane, info in self.live.items()
                     if info.get("host") == host
                     and info["window"] == self.live[host]["window"]), None)

    def reserve(self, host, session, prompt, reviewer=None):
        if not session or not prompt or host not in self.live:
            return None
        reviewer = reviewer or self.reviewer(host)
        if reviewer not in self.live or self.live[reviewer].get("host") != host:
            return None
        key = exchange_id(session, prompt)
        if key not in self.reviews:
            self.reviews[key] = {"host": host, "host_pid": self.live[host]["pid"],
                                 "reviewer": reviewer, "reviewer_pid": self.live[reviewer]["pid"],
                                 "session": session, "prompt": prompt, "phase": "expected"}
        return self.reviews[key]

    def begin(self, pane, turn):
        current = self.panes[pane]
        if turn and current["turn"] == turn:
            return
        current.update(state="running", turn=turn, last_done="")
        self.windows[self.live[pane]["window"]]["pending"].pop(pane, None)

    def state(self, pane, agent, state, payload):
        if pane not in self.live:
            return
        current = self.panes[pane]
        current["agent"] = agent
        session = payload.get("session_id", "")
        prompt = payload.get("prompt_id", "")
        turn_id = payload.get("turn_id") or prompt
        turn = session + ":" + turn_id if session and turn_id else ""
        event = payload.get("hook_event_name", "")
        if state == "done" and turn and current.get("cancelled_turn") == turn:
            return False
        if state == "running" and event == "UserPromptSubmit":
            if turn and current["last_done"] == turn:
                return False
            self.begin(pane, turn)
            # Claude's background-task wakeup has a new prompt ID, but its
            # review belongs to the original human prompt reserved at submission.
            if not self.live[pane].get("host") and not (agent == "claude" and payload.get("task_notification")):
                self.reserve(pane, session, prompt)
            matches = Counter(payload.get("message_hashes", []))
            for review in self.reviews.values():
                if review["reviewer"] == pane and review.get("turn") == turn:
                    matches[review.get("hash")] -= 1
            for review in self.reviews.values():
                digest = review.get("hash")
                if (turn and review["reviewer"] == pane and review["phase"] in ("queued", "error", "paused")
                        and matches[digest] > 0):
                    review.update(phase="running", turn=turn)
                    matches[digest] -= 1
        if state == "done" and turn:
            for review in self.reviews.values():
                if review["reviewer"] == pane and review.get("turn") == turn:
                    review["phase"] = "completed"
        # A delayed completion from a prior turn cannot mark a newer turn ready.
        if turn and current["turn"] and turn != current["turn"] and event != "SessionEnd":
            return False
        if self.questions:
            state = self.questions.transition(pane, agent, state, payload)
            if state is None:
                return False
        if state == "done":
            if not current.get("observed") or current["state"] != "done" or (turn and current["last_done"] != turn):
                self.windows[self.live[pane]["window"]]["pending"][pane] = agent
            current["last_done"] = turn
        elif state == "off":
            current["cancelled_turn"] = turn or current["turn"]
            self.windows[self.live[pane]["window"]]["pending"].pop(pane, None)
            for review in self.reviews.values():
                if review["phase"] in TERMINAL:
                    continue
                # Codex restores queued follow-ups to the composer on interrupt.
                # They become work again only when submitted; an abandoned draft
                # must not hold future notifications indefinitely.
                if (event == "Interrupt" and review["reviewer"] == pane
                        and review["phase"] == "queued"):
                    review["phase"] = "paused"
                    self.discard_review(review)
                    continue
                cancelled_turn = turn or current["turn"]
                host_cancelled = (review["host"] == pane and review["phase"] in ("expected", "error")
                                  and (event == "SessionEnd" or
                                       review["session"] + ":" + review["prompt"] == cancelled_turn))
                reviewer_cancelled = (review["reviewer"] == pane and
                                      (event == "SessionEnd" or review.get("turn") == cancelled_turn))
                if host_cancelled or reviewer_cancelled:
                    review["phase"] = "cancelled"
                    self.discard_review(review)
        elif state == "running" and current["state"] != "running":
            self.windows[self.live[pane]["window"]]["pending"].pop(pane, None)
        current["state"] = state
        current["observed"] = True
        return True

    def handoff(self, message):
        host = message.get("host", "")
        if host not in self.live:
            return
        if str(message.get("host_pid", "")) != self.live[host]["pid"]:
            return
        session, prompt = message.get("session_id", ""), message.get("prompt_id", "")
        review = self.reserve(host, session, prompt, message.get("reviewer"))
        if not review:
            return
        action = message.get("action")
        if action == "expect":
            self.begin(host, session + ":" + prompt)
        elif action == "queue" and review["phase"] in ("expected", "error"):
            review.update(phase="queued", hash=message["hash"])
        elif action == "skip" and review["phase"] == "expected":
            review["phase"] = "skipped"
        elif action == "fail" and review["phase"] in ("expected", "queued"):
            review["phase"] = "error"

    def acknowledge(self, window):
        if window in self.windows:
            self.windows[window]["pending"].clear()

    def completions(self, enabled=True):
        events = []
        for wid, window in self.windows.items():
            panes = {p for p, info in self.live.items() if info["window"] == wid}
            busy = any(self.panes[p]["state"] in ("running", "needs-input") for p in panes)
            outstanding = any(r["phase"] not in TERMINAL and
                              (r["host"] in panes or r["reviewer"] in panes)
                              for r in self.reviews.values())
            window["review_pending"] = outstanding
            window["ready"] = not busy and not outstanding
            if window["pending"] and window["ready"]:
                pane, agent = next(reversed(window["pending"].items()))
                window["pending"].clear()
                if enabled:
                    window["sequence"] += 1
                    events.append((wid, pane, agent))
        return events


def tmux(*args):
    return subprocess.check_output(["tmux", *args], text=True, timeout=5).strip()


def tmux_commands(commands):
    # The client command message has a size limit. Stream the snapshot through
    # stdin so retained reviews and pending questions cannot exceed that limit.
    # Escape newlines as well: tmux removes backslash-newline continuations even
    # inside shell-style single quotes. Dollar signs must stay literal too.
    escapes = {'\\': '\\\\', '"': '\\"', '$': '\\$', '\n': '\\n', '\r': '\\r', '\t': '\\t'}
    def quote(value):
        return '"' + ''.join(escapes.get(char, char) for char in value) + '"'
    source = "\n".join(" ".join(quote(arg) for arg in command) for command in commands) + "\n"
    subprocess.run(["tmux", "source-file", "-"], input=source, text=True,
                   stdout=subprocess.PIPE, check=True, timeout=5)


def option_snapshot():
    """Read raw option values in one batch, preserving quotes and empty values."""
    names = [line.split(" ", 1)[0] for line in tmux("show-options", "-g").splitlines()
             if line.startswith("@agent-indicator-")]
    if not names:
        return {}
    marker = "agent-options-" + uuid.uuid4().hex
    commands = []
    for name in names:
        if commands:
            commands.append(";")
        commands.extend(["show-options", "-gqv", name, ";", "display-message", "-p", marker])
    raw = subprocess.check_output(["tmux", *commands], text=True, timeout=5)
    return dict(zip(names, (value[:-1] for value in raw.split(marker + "\n")[:-1])))


def main():
    if sys.argv[1] == "options":
        for name, value in option_snapshot().items():
            sys.stdout.write(name + "\0" + value + "\0")
        sys.stdout.write("__SNAPSHOT_OK__\0\0")
        return
    if sys.argv[1] == "hook-data":
        print(json.dumps(hook_data(json.load(sys.stdin)), separators=(",", ":")))
        return
    # The Bash parent owns the record lock while this child reads/writes tmux.
    os.fstat(9)
    env = dict(line.split("=", 1) for line in tmux("show-environment", "-g").splitlines()
               if line.startswith("TMUX_AGENT_PANE_") and "=" in line)
    live = {}
    for line in tmux("list-panes", "-a", "-F", "#{pane_id}|#{pane_pid}|#{pane_dead}|#{window_id}|#{@kibitz_host_pane}").splitlines():
        pane, pid, dead, window, host = line.split("|", 4)
        if dead == "0":
            live[pane] = dict(pid=pid, window=window, host=host,
                              state=env.get(f"TMUX_AGENT_PANE_{pane}_STATE", "idle"))
    options = option_snapshot()
    raw = options.get(OPTION, "")
    data = json.loads(raw) if raw else {}
    def option(name, default):
        return options.get(name, default)

    try:
        delay = float(option("@agent-indicator-input-delay", "3"))
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("invalid delay")
    except ValueError:
        delay = 3
    questions = input_status.Questions(data.setdefault("questions", {}), live, delay,
                                      lambda q, p, info: input_status.status(q, p, info, tmux))
    tracker = Notifications(data, live, questions)
    action = sys.argv[1]
    apply_state = True
    input_changes = []
    if action == "state":
        apply_state = tracker.state(*sys.argv[2:5], json.loads(os.environ.get("TMUX_AGENT_HOOK_DATA", "{}")))
    elif action == "handoff":
        tracker.handoff(json.loads(os.environ["TMUX_AGENT_NOTIFICATION_MESSAGE"]))
    elif action == "ack":
        tracker.acknowledge(sys.argv[2])
    elif action == "input":
        input_changes = questions.observed(*sys.argv[2:9])
    if option("@agent-indicator-needs-input-enabled", "on").lower() in ("on", "true", "yes", "1"):
        input_changes.extend(questions.check())
    else:
        affected = {pane: agent for pane, agent, _state, _notify in input_changes}
        affected.update((pane, q["agent"]) for pane, q in questions.records.items())
        input_changes = [(pane, agent, "off", False) for pane, agent in affected.items()]
        questions.records.clear()
    for pane, _agent, state, _notify in input_changes:
        tracker.panes[pane]["state"] = state

    enabled = option("@agent-indicator-notification-enabled", "on")
    states = option("@agent-indicator-notification-states", "needs-input,done")
    previous_pending = {wid: window.get("review_pending", False)
                        for wid, window in tracker.windows.items()}
    events = tracker.completions(enabled.lower() in ("on", "true", "yes", "1")
                                 and "done" in states.split(","))
    # Retain recent terminal records to make repeated/out-of-order hooks idempotent.
    # Active reservations are never aged out into successful completion.
    terminal = [key for key, review in tracker.reviews.items() if review["phase"] in TERMINAL]
    for key in terminal[:-128]:
        del tracker.reviews[key]
    commands = [["set-option", "-gq", OPTION, json.dumps(data, separators=(",", ":"))]]
    for pane in live:
        commands.append(["set-option", "-pq", "-t", pane, "@agent-indicator-input-pending",
                         questions.records.get(pane, {}).get("token", "")])
    for wid, window in tracker.windows.items():
        commands.append(["set-option", "-wq", "-t", wid, "@agent-indicator-completion",
                         f'{data["epoch"]}:{window["sequence"]}:{int(window["ready"])}'])
        commands.append(["set-option", "-wq", "-t", wid, "@agent-indicator-review-pending",
                         str(int(window["review_pending"]))])
        failed = any(r["phase"] == "error" and r["host"] in live
                     and live[r["host"]]["window"] == wid for r in tracker.reviews.values())
        commands.append(["set-option", "-wq", "-t", wid, "@agent-indicator-notification-error",
                         "Kibitz delivery failed" if failed else ""])
    tmux_commands(commands)
    for pane, question in questions.started.items():
        if questions.records.get(pane) is question:
            try:
                subprocess.Popen([sys.executable, str(Path(__file__).with_name("input-status.py")),
                                  "watch", pane, json.dumps(question, separators=(",", ":"))],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
            except OSError:
                pass  # The sweep checks the question if its reader cannot start.
    for pane, agent, state, notify in input_changes:
        print("INPUT", pane, agent, state, int(notify), live[pane]["pid"])
    if action == "state" and apply_state is not False:
        print("STATE", tracker.panes[sys.argv[2]]["state"])
        if questions.silent:
            print("QUIET input")
    # Handoffs and owner cleanup can change the color without another agent hook.
    for wid, window in tracker.windows.items():
        if window["review_pending"] != previous_pending[wid]:
            print("RENDER", wid)
    if apply_state is False:
        print("SKIP stale")
    for _wid, pane, agent in events:
        print(pane, re.sub(r"[^a-zA-Z0-9_-]", "", agent) or "agent")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"tmux-agent-indicator: notification tracking failed: {error}", file=sys.stderr)
        sys.exit(1)
