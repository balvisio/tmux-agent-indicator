#!/usr/bin/env python3
"""Question grace periods, native status, and rendered state on isolated tmux."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("input_status", ROOT / "scripts/input-status.py")
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


class QuestionsTest(unittest.TestCase):
    def setUp(self):
        self.live = {"%1": {"pid": "42", "window": "@1"}}
        self.observed = "unknown"
        self.q = status.Questions({}, self.live, 3, lambda *args: self.observed, now=10)
        self.payload = dict(hook_event_name="PermissionRequest", session_id="one", tool_use_id="a")

    def question(self, **kw):
        return self.q.transition("%1", "claude", "needs-input", dict(self.payload, **kw))

    def report(self, result="unknown"):
        question = self.q.records["%1"]
        return self.q.observed("%1", question["token"], "42", result, "1")

    def test_unknown_alerts_after_three_seconds_once(self):
        self.assertEqual(self.question(), "running")
        self.assertEqual(self.q.check(), [])
        self.q.now = 12.9
        self.assertEqual(self.q.check(), [])
        self.q.now = 13
        self.assertEqual(self.report(), [("%1", "claude", "needs-input", True)])
        self.q.now = 15
        self.assertEqual(self.q.check(), [])

    def test_fast_and_late_answers(self):
        self.question()
        self.q.check()
        self.observed = "answered"
        self.q.now = 11
        self.assertEqual(self.report("answered"), [("%1", "claude", "running", False)])
        self.assertFalse(self.q.records)
        self.question(tool_use_id="b")
        self.observed = "waiting"
        self.q.now = 15
        self.assertTrue(self.report("waiting")[0][3])
        self.observed = "answered"
        self.q.now = 20
        self.assertFalse(self.q.check()[0][3])

    def test_parallel_tool_cannot_cancel_a_question(self):
        self.question()
        post = dict(self.payload, hook_event_name="PostToolUse", tool_use_id="other")
        self.assertIsNone(self.q.transition("%1", "claude", "running", post))
        self.assertTrue(self.q.records)
        self.observed = "waiting"
        post["tool_use_id"] = "a"
        self.assertIsNone(self.q.transition("%1", "claude", "running", post))
        self.observed = "unknown"
        self.assertEqual(self.q.transition("%1", "claude", "running", post), "running")
        self.assertFalse(self.q.records)

    def test_duplicate_does_not_extend_deadline_or_ring_again(self):
        self.question()
        token = self.q.records["%1"]["token"]
        self.q.now = 12
        self.question()
        self.assertEqual(self.q.records["%1"]["deadline"], 13)
        self.assertEqual(self.q.records["%1"]["token"], token)
        self.assertTrue(self.q.silent)
        self.q.now = 13
        self.report("waiting")
        self.assertEqual(self.question(), "needs-input")
        self.assertTrue(self.q.silent)

    def test_new_question_replaces_old_timer(self):
        self.question()
        self.q.now = 12
        self.question(tool_use_id="b")
        self.q.now = 13
        self.assertFalse(self.q.check())
        self.q.now = 15
        self.assertTrue(self.report("waiting")[0][3])

    def test_stale_observer_cannot_clear_a_new_question(self):
        self.question()
        old = dict(self.q.records["%1"])
        self.question(tool_use_id="b")
        self.assertEqual(self.q.observed("%1", old["token"], "42", "answered", "1"), [])
        self.assertEqual(self.q.records["%1"]["tool"], "b")
        self.assertEqual(self.q.observed("%1", self.q.records["%1"]["token"], "99", "answered", "1"), [])

    def test_sweep_recovers_if_reader_dies(self):
        self.question()
        self.q.now = 18
        self.assertEqual(self.q.check(), [("%1", "claude", "needs-input", True)])

    def test_reader_observes_brief_dialog_without_state_writes(self):
        question = dict(pid="42", token="one", deadline=3, seen_waiting=False)
        def observe(q, *_args):
            return "answered" if q["seen_waiting"] else "waiting"
        with patch.object(status, "tmux", return_value="42|0|one"), \
                patch.object(status, "status", side_effect=observe), \
                patch.object(status.time, "monotonic", side_effect=[0, .25]), \
                patch.object(status.time, "sleep") as sleep, \
                patch.object(status.subprocess, "run") as run:
            status.watch("%1", question)
        sleep.assert_called_once_with(.25)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-6:],
                         ["--input-result", "%1", "one", "42", "answered", "1"])
        with patch.object(status, "tmux", return_value="43|0|other"), \
                patch.object(status.subprocess, "run") as run:
            status.watch("%1", question)
        run.assert_not_called()

    def test_interrupt_done_new_prompt_and_pane_reuse_cancel(self):
        for state, event in [("off", "Interrupt"), ("done", "Stop"), ("running", "UserPromptSubmit")]:
            self.question()
            self.q.transition("%1", "claude", state, dict(hook_event_name=event))
            self.assertFalse(self.q.records)
        self.question()
        self.live["%1"]["pid"] = "43"
        status.Questions(self.q.records, self.live, 3, self.q.probe)
        self.assertFalse(self.q.records)
        self.question()
        self.live.clear()
        status.Questions(self.q.records, self.live, 3, self.q.probe)
        self.assertFalse(self.q.records)

    def test_zero_disables_delay(self):
        self.q.delay = 0
        self.assertEqual(self.question(), "needs-input")
        self.assertFalse(self.q.records)

    def test_codex_dialogs_and_working_composer(self):
        approval = "Would you like to run the following command?\n› 1. Yes, proceed (y)\n\n  Press enter to confirm or esc to cancel\n"
        self.assertEqual(status.codex_screen(approval, 1, False), "waiting")
        question = "Question 1/1\n› Type your answer (optional)\n\nenter to submit answer | esc to interrupt\n"
        self.assertEqual(status.codex_screen(question, 1, True), "waiting")
        normal = "\nWorking (34s • esc to interrupt)\n\n› Ask Codex to do anything\n\nmodel · directory\n"
        self.assertEqual(status.codex_screen(normal, 3, True), "answered")
        self.assertEqual(status.codex_screen(normal, 3, False), "unknown")
        self.assertEqual(status.codex_screen(normal, 99, True), "unknown")
        self.assertEqual(status.codex_screen("› 1. Yes, proceed\n", 0, True), "unknown")
        self.assertEqual(status.codex_screen("› Type your answer\nunknown dialog\n", 0, True), "unknown")

    def test_claude_status_requires_current_session_and_process(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "sessions"
            folder.mkdir()
            path = folder / "record.json"
            q = dict(session="one", config_dir=directory, received=time.time())
            _, started = status.process(os.getpid())
            data = dict(sessionId="one", pid=os.getpid(), procStart=started, pidDomain=status.pid_domain(),
                        status="busy", statusUpdatedAt=time.time() * 1000)
            path.write_text(json.dumps(data))
            self.assertEqual(status.claude_status(q, str(os.getpid())), "answered")
            self.assertEqual(status.claude_status(q, "999999999"), "unknown")
            for field, value in [("sessionId", "other"), ("procStart", "wrong"),
                                 ("pidDomain", "another-namespace"), ("statusUpdatedAt", 0)]:
                path.write_text(json.dumps(dict(data, **{field: value})))
                self.assertEqual(status.claude_status(q, str(os.getpid())), "unknown")
            path.write_text("{")
            self.assertEqual(status.claude_status(q, str(os.getpid())), "unknown")
            path.unlink()
            self.assertEqual(status.claude_status(q, str(os.getpid())), "unknown")


FAKE_AGENT = r'''
import json, os, pathlib, subprocess, sys, time
root = pathlib.Path(sys.argv[1])
session = sys.argv[2]
path = root / '.claude/sessions' / (str(os.getpid()) + '.json')
path.parent.mkdir(parents=True, exist_ok=True)
if sys.platform == 'linux':
    started = pathlib.Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
    domain = 'linux:' + pathlib.Path('/etc/machine-id').read_text().strip() + ':' + os.readlink('/proc/self/ns/pid')
else:
    started = subprocess.check_output(['ps', '-p', str(os.getpid()), '-o', 'lstart='], text=True).strip()
    domain = sys.platform
previous = None
while True:
    value = (root / session).read_text()
    if value != previous:
        data = dict(sessionId=session, pid=os.getpid(), procStart=started, pidDomain=domain,
                    status=value, statusUpdatedAt=time.time() * 1000)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(data))
        temp.replace(path)
        previous = value
    time.sleep(.02)
'''


class TmuxTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="input-delay-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(PATH=os.environ["PATH"], HOME=str(self.root), SHELL="/bin/bash",
                        TMUX_TMPDIR=str(self.root), XDG_CONFIG_HOME=str(self.root / "config"),
                        XDG_CACHE_HOME=str(self.root / "cache"), XDG_STATE_HOME=str(self.root / "state"),
                        CODEX_HOME=str(self.root / "codex"), CODEX_CONFIG_DIR=str(self.root / "codex"),
                        ZDOTDIR=str(self.root), PYTHONDONTWRITEBYTECODE="1")
        self.socket = str(self.root / "tmux.sock")
        self.addCleanup(lambda: subprocess.run(["tmux", "-S", self.socket, "kill-server"],
                                              env=self.env, capture_output=True, timeout=10))
        (self.root / "agent.py").write_text(FAKE_AGENT)
        (self.root / "one").write_text("busy")
        command = shlex.join(["python3", str(self.root / "agent.py"), str(self.root), "one"])
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "test", command)
        self.env["TMUX"] = self.tmux("display-message", "-p", "#{socket_path},#{pid},0")
        self.pane = self.tmux("display-message", "-p", "#{pane_id}")
        self.window = self.tmux("display-message", "-p", "#{window_id}")
        self.tmux("set", "-g", "@agent-indicator-notification-message", "off")
        self.tmux("set", "-g", "@agent-indicator-style-active-window", "on")
        self.tmux("set", "-g", "@agent-indicator-notification-command",
                  'echo "$AGENT_STATE" >> ' + shlex.quote(str(self.root / "bells")))
        for state, color in [("running", "yellow"), ("needs-input", "magenta"), ("done", "red")]:
            self.tmux("set", "-g", f"@agent-indicator-{state}-window-title-bg", color)
            self.tmux("set", "-g", f"@agent-indicator-{state}-window-title-fg", "black")
        self.wait(lambda: bool(list((self.root / ".claude/sessions").glob("*.json"))))
        self.hook("running", "UserPromptSubmit")

    def tmux(self, *args):
        return subprocess.check_output(["tmux", "-S", self.socket, *args], env=self.env,
                                       text=True, stderr=subprocess.STDOUT, timeout=10).strip()

    def wait(self, condition, timeout=6):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if condition():
                return
            time.sleep(.05)
        self.fail("condition timed out")

    def native(self, value):
        (self.root / "one").write_text(value)
        self.wait(lambda: any(json.loads(p.read_text())["status"] == value
                             for p in (self.root / ".claude/sessions").glob("*.json")))

    def hook(self, state, event, tool="a", conditional=False):
        data = dict(session_id="one", prompt_id="turn", hook_event_name=event, tool_use_id=tool)
        args = [str(ROOT / "scripts/agent-state.sh"), "--hook", "--agent", "claude", "--state", state]
        if conditional:
            args += ["--if-state", "needs-input"]
        subprocess.run(args, input=json.dumps(data), env=dict(self.env, TMUX_PANE=self.pane),
                       text=True, capture_output=True, check=True, timeout=15)

    def state(self):
        return self.tmux("show-option", "-wqv", "-t", self.window, "@agent-indicator-state")

    def bells(self):
        path = self.root / "bells"
        return path.read_text().splitlines() if path.exists() else []

    def sweep(self):
        subprocess.run([str(ROOT / "scripts/window-state.sh"), "--notifications"],
                       env=self.env, capture_output=True, check=True, timeout=15)

    def test_fast_accept_stays_yellow_without_tool_completion(self):
        self.native("waiting")
        self.hook("needs-input", "PermissionRequest")
        self.assertEqual(self.state(), "running")
        self.assertIn("bg=yellow", self.tmux("show-option", "-wv", "window-status-style"))
        time.sleep(.4)
        self.native("busy")
        self.wait(lambda: not self.tmux("show-option", "-pqv", "-t", self.pane,
                                       "@agent-indicator-input-pending"))
        time.sleep(3)
        self.assertEqual(self.state(), "running")
        self.assertFalse(self.bells())
        self.assertEqual(self.tmux("show-option", "-wqv", "@agent-indicator-completion").split(":")[-1], "0")

    def test_unanswered_rings_once_then_accept_restores_yellow(self):
        self.native("waiting")
        started = time.monotonic()
        self.hook("needs-input", "PermissionRequest")
        self.assertFalse(self.bells())
        self.wait(lambda: self.bells() == ["needs-input"])
        self.assertGreaterEqual(time.monotonic() - started, 3)
        self.assertEqual(self.state(), "needs-input")
        self.assertIn("bg=magenta", self.tmux("show-option", "-wv", "window-status-style"))
        self.hook("needs-input", "PermissionRequest")
        self.assertEqual(self.bells(), ["needs-input"])
        snapshot = self.tmux("show-option", "-gqv", "@agent-indicator-notifications")
        time.sleep(1.2)
        self.assertEqual(snapshot, self.tmux("show-option", "-gqv", "@agent-indicator-notifications"),
                         "An unanswered question must not keep spawning state writers")
        self.native("busy")
        time.sleep(4)
        self.sweep()
        self.assertEqual(self.state(), "running")
        self.assertEqual(self.bells(), ["needs-input"])

    def test_reader_does_not_reconcile_between_hook_and_deadline(self):
        self.native("waiting")
        self.hook("needs-input", "PermissionRequest")
        snapshot = self.tmux("show-option", "-gqv", "@agent-indicator-notifications")
        time.sleep(1)
        self.assertEqual(snapshot, self.tmux("show-option", "-gqv", "@agent-indicator-notifications"))
        self.wait(lambda: self.bells() == ["needs-input"])

    def test_option_snapshot_preserves_empty_quotes_and_newlines(self):
        values = {"@agent-indicator-test-empty": "", "@agent-indicator-test-quoted": 'a "quote" \\ text',
                  "@agent-indicator-test-lines": "one\ntwo\tthree", "@agent-indicator-test-unicode": "☀ magenta"}
        for name, value in values.items():
            self.tmux("set", "-g", name, value)
        raw = subprocess.check_output(["python3", str(ROOT / "scripts/notification-state.py"), "options"],
                                      env=self.env, text=True, timeout=10).split("\0")
        snapshot = dict(zip(raw[::2], raw[1::2]))
        for name, value in values.items():
            self.assertEqual(snapshot[name], value)
        command = 'source "$1"; load_state_snapshot; tmux_get_option_or_default "$2" fallback'
        for name, value in values.items():
            result = subprocess.check_output(["bash", "-c", command, "test", str(ROOT / "scripts/state-lib.sh"), name],
                                             env=self.env, text=True, timeout=10)
            self.assertEqual(result, value + "\n")

    def test_parallel_post_tool_does_not_hide_question(self):
        self.native("waiting")
        self.hook("needs-input", "PermissionRequest")
        self.hook("running", "PostToolUse", tool="other", conditional=True)
        self.wait(lambda: self.bells() == ["needs-input"])
        self.assertEqual(self.state(), "needs-input")

    def test_interrupt_cancels_and_old_timer_cannot_repaint(self):
        self.native("waiting")
        self.hook("needs-input", "PermissionRequest")
        self.hook("off", "Interrupt")
        time.sleep(3.5)
        self.assertEqual(self.state(), "idle")
        self.assertFalse(self.bells())

    def test_stale_busy_status_cannot_suppress_alert(self):
        # No fresh status update after the question: this is unknown, not accepted.
        self.hook("needs-input", "PermissionRequest")
        self.wait(lambda: self.bells() == ["needs-input"])
        self.assertEqual(self.state(), "needs-input")

    def test_pending_question_blocks_sibling_completion(self):
        self.native("waiting")
        self.hook("needs-input", "PermissionRequest")
        sibling = self.tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "/bin/bash --noprofile --norc")
        subprocess.run([str(ROOT / "scripts/agent-state.sh"), "--agent", "claude", "--state", "done"],
                       env=dict(self.env, TMUX_PANE=sibling), capture_output=True, check=True, timeout=15)
        self.assertFalse(self.bells())
        self.assertEqual(self.state(), "running")
        self.native("busy")
        self.wait(lambda: not self.tmux("show-option", "-pqv", "-t", self.pane,
                                       "@agent-indicator-input-pending"))
        self.assertFalse(self.bells())
        self.hook("done", "Stop")
        self.wait(lambda: self.bells() == ["done"])

    def test_disabled_input_state_cancels_pending_alert(self):
        self.native("waiting")
        self.hook("needs-input", "PermissionRequest")
        self.tmux("set", "-g", "@agent-indicator-needs-input-enabled", "off")
        self.wait(lambda: self.state() == "idle")
        time.sleep(3)
        self.assertFalse(self.bells())

    def test_stale_writer_cannot_apply_to_replacement_process(self):
        command = 'source "$1"; if apply_agent_state "$2" claude needs-input 999999999; then exit 1; fi'
        subprocess.run(["bash", "-c", command, "test", str(ROOT / "scripts/state-lib.sh"), self.pane],
                       env=self.env, check=True, capture_output=True, timeout=10)
        self.assertEqual(self.state(), "running")
        self.assertFalse(self.bells())


if __name__ == "__main__":
    unittest.main()
