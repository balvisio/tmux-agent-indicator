#!/usr/bin/env python3
"""Read explicit Codex names without stale identity, preview text, or extra scans."""
import importlib.util
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


presence = module("agent-present")
recovery = module("agent-recovery")
STARTED = "Mon Nov 16 12:00:00 2026"


class CodexNames(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.database = self.home / "state_5.sqlite"
        self.session = str(uuid.uuid4())
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, preview TEXT)")
            db.execute("INSERT INTO threads VALUES (?, ?, ?)", (self.session, "Review", "Generated preview"))
        self.record = dict(kind="codex", pid=101, started=STARTED, session=self.session,
                           env={"CODEX_HOME": str(self.home)})
        self.processes = {"100": ("1", "pts/1", "bash", STARTED),
                          "101": ("100", "pts/1", "codex --no-daemon", STARTED)}

    def pane(self, **changes):
        return ["%1", "100", "0", "@1", "/dev/pts/1", "", "", json.dumps(dict(self.record, **changes))]

    def test_reads_only_explicit_names_and_refreshes_on_rename(self):
        panes = [self.pane()]
        self.assertEqual(presence.codex_names(panes, self.processes), {"%1": "Review"})
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE threads SET name=?", ('new | "name" $HOME #{pane_id}\nline',))
        self.assertEqual(presence.codex_names(panes, self.processes), {"%1": 'new | "name" $HOME #{pane_id}\nline'})
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE threads SET name=NULL")
        self.assertEqual(presence.codex_names(panes, self.processes), {})

    def test_each_codex_home_is_opened_once(self):
        second = self.pane()
        second[0] = "%2"
        with patch.object(sqlite3, "connect", wraps=sqlite3.connect) as connect:
            names = presence.codex_names([self.pane(), second], self.processes)
            self.assertEqual(names, {"%1": "Review", "%2": "Review"})
            self.assertEqual(connect.call_count, 1)
            self.assertIn("mode=ro", connect.call_args.args[0])

    def test_reused_pid_wrong_agent_and_invalid_identity_are_ignored(self):
        for change in (dict(started="previous process"), dict(kind="claude"),
                       dict(pid=999), dict(session="invalid"), dict(env=[])):
            with self.subTest(change=change):
                self.assertEqual(presence.codex_names([self.pane(**change)], self.processes), {})
        self.processes["101"] = ("100", "pts/1", "bash", STARTED)
        self.assertEqual(presence.codex_names([self.pane()], self.processes), {})

    def test_foreign_process_with_colliding_tty_is_ignored(self):
        self.processes["101"] = ("999", "pts/1", "codex", STARTED)
        self.assertEqual(presence.codex_names([self.pane()], self.processes), {})

    def test_closed_and_respawned_panes_do_not_keep_the_name(self):
        pane = self.pane()
        pane[2] = "1"
        self.assertEqual(presence.codex_names([pane], self.processes), {})
        pane[2], pane[1] = "0", "200"
        self.assertEqual(presence.codex_names([pane], self.processes), {})

    def test_changed_conversation_invalidates_the_sweep_signature(self):
        self.assertNotEqual(presence.signature([self.pane()]),
                            presence.signature([self.pane(session=str(uuid.uuid4()))]))

    def test_missing_corrupt_or_locked_database_does_not_fail_the_sweep(self):
        self.assertEqual(presence.codex_names([self.pane(env={"CODEX_HOME": str(self.home / "missing")})], self.processes), {})
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("BEGIN EXCLUSIVE")
            self.assertEqual(presence.codex_names([self.pane()], self.processes), {})
        self.database.write_bytes(b"not a database")
        self.assertEqual(presence.codex_names([self.pane()], self.processes), {})

    def test_claude_name_keeps_precedence_in_a_paired_window(self):
        claude = ["%2", "200", "0", "@1", "/dev/pts/2", "", "", ""]
        processes = dict(self.processes, **{"200": ("1", "pts/2", "claude", STARTED)})
        label = presence.presence([self.pane(), claude], processes, {"%2": "Claude name"},
                                  {"%1": "Codex name"}).split("\0")[3]
        self.assertEqual(label, "🧠🤖 Claude name")
        label = presence.presence([self.pane(), claude], processes, {}, {"%1": "Codex name"}).split("\0")[3]
        self.assertEqual(label, "🧠🤖 Codex name")

    def test_native_window_names_flatten_controls_without_expanding_text(self):
        name = '0 title\nwith\ttabs\rcontrols\x1b\x7f and #{pane_id} $HOME "quotes"'
        expected = '0 title with tabs controls   and #{pane_id} $HOME "quotes"'
        for agent, icon in (("claude", "🤖"), ("codex", "🧠")):
            with self.subTest(agent=agent):
                processes = dict(self.processes, **{"101": ("100", "pts/1", agent, STARTED)})
                names = {"%1": name}
                result = presence.presence([self.pane()], processes, names, names).split("\0")[3]
                self.assertEqual(result, icon + " " + expected)

    def test_identity_is_recorded_without_recovery_or_backup_side_effects(self):
        event = dict(session_id=self.session, cwd=str(self.home), hook_event_name="SessionStart")
        procs = {100: (1, STARTED, "bash"), 101: (100, STARTED, "codex --no-daemon")}
        with patch.dict(os.environ, {"TMUX": "isolated,1,0"}), \
                patch.object(recovery.runtime, "hook_server", return_value=None), \
                patch.object(recovery.runtime, "process", return_value=None), \
                patch.object(recovery, "processes", return_value=procs), \
                patch.object(recovery.os, "getppid", return_value=101), \
                patch.object(recovery, "request_save") as save, \
                patch.object(recovery, "tmux", side_effect=lambda *args: "%1\t100\t0\toff\t" if args[0] == "list-panes" else "") as tmux:
            with patch.object(recovery.sys, "stdin", io.StringIO(json.dumps(event))):
                recovery.record("codex", [])
            record = json.loads(tmux.call_args.args[-1])
            self.assertEqual(record["session"], self.session)
            self.assertEqual(record["pid"], 101)
            save.assert_not_called()
            tmux.reset_mock()
            for change in (dict(hook_event_name="SessionEnd"), dict(transcript_path=None), dict(agent_id="child")):
                with patch.object(recovery.sys, "stdin", io.StringIO(json.dumps(dict(event, **change)))):
                    recovery.record("codex", [])
            self.assertFalse(any(call.args[0] == "set-option" for call in tmux.call_args_list))
            save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
