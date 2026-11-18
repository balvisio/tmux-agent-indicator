#!/usr/bin/env python3
"""Exercise failures in the real focus/layout workers on isolated tmux servers."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(os.environ.get('AGENT_TEST_PLUGIN', Path(__file__).resolve().parents[1]))
TMUX = shutil.which('tmux')


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent-navigation-failure-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bins = self.root / 'bin'
        bins.mkdir()
        self.env = dict(PATH=str(bins) + os.pathsep + os.environ['PATH'], HOME=str(self.root),
                        SHELL='/bin/bash', TERM='xterm-256color', PYTHONDONTWRITEBYTECODE='1',
                        TMUX_AGENT_RUNTIME_ROOT=str(self.root / 'runtime'),
                        REVIEW_REAL_TMUX=TMUX, REVIEW_REAL_PYTHON=sys.executable)
        (bins / 'python3').write_text('''#!/bin/sh
if [ "${REVIEW_FAIL:-}" = focus-ack ] && [ "${1##*/}" = notification-state.py ] && [ "${2:-}" = ack ]; then
    exit 23
fi
exec "$REVIEW_REAL_PYTHON" "$@"
''')
        (bins / 'tmux').write_text('''#!/bin/sh
for argument do
    if [ "${REVIEW_FAIL:-}" = focus-read ] && [ "$argument" = '#{pane_active}:#{window_active}:#{window_id}' ]; then
        exit 23
    fi
    if [ "${REVIEW_FAIL:-}" = layout-write ] && [ "$1" = set-window-option ] && [ "$argument" = @agent-indicator-state ]; then
        exit 23
    fi
done
exec "$REVIEW_REAL_TMUX" "$@"
''')
        for path in bins.iterdir():
            path.chmod(0o755)
        self.socket = str(self.root / 'socket')
        self.addCleanup(lambda: subprocess.run([TMUX, '-S', self.socket, 'kill-server'],
                                              env=self.env, capture_output=True, timeout=10))
        self.t('-f', '/dev/null', 'new-session', '-d', '-s', 'test', '/bin/bash --noprofile --norc')
        self.env['TMUX'] = self.t('display', '-p', '#{socket_path},#{pid},0')
        self.pane, self.window = self.t('display', '-p', '#{pane_id}|#{window_id}').split('|')
        self.t('set', '-g', '@agent-indicator-notification-enabled', 'off')
        for kind in ('focus', 'layout'):
            self.t('set', '-g', f'@agent-indicator-{kind}-launch',
                   'set-option -g @test-rechecked yes')

    def t(self, *args):
        return subprocess.check_output([TMUX, '-S', self.socket, *args], env=self.env,
                                       text=True, stderr=subprocess.STDOUT, timeout=15).strip()

    def run_worker(self, kind, fail='', token='17', pending='17'):
        self.t('set', '-w', '-t', self.window, f'@agent-indicator-{kind}-request', pending,
               ';', 'set', '-gu', '@test-rechecked')
        command = ([str(ROOT / 'scripts/pane-focus-in.sh'), self.pane, self.window, token]
                   if kind == 'focus' else
                   [str(ROOT / 'scripts/window-state.sh'), '--layout', self.window, token])
        return subprocess.run(command, env=dict(self.env, REVIEW_FAIL=fail), capture_output=True,
                              text=True, timeout=30)

    def test_failed_workers_release_request_without_rearming_and_can_recover(self):
        for kind, fail in (('focus', 'focus-ack'), ('layout', 'layout-write')):
            with self.subTest(kind=kind):
                result = self.run_worker(kind, fail)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.t('show', '-wqv', f'@agent-indicator-{kind}-request'), '')
                self.assertEqual(self.t('show', '-gqv', '@test-rechecked'), '')
                result = self.run_worker(kind, token='18', pending='18')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.t('show', '-wqv', f'@agent-indicator-{kind}-request'), '')
                self.assertEqual(self.t('show', '-gqv', '@test-rechecked'), 'yes')

    def test_failed_focus_read_is_not_a_successful_stale_event(self):
        result = self.run_worker('focus', 'focus-read')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.t('show', '-wqv', '@agent-indicator-focus-request'), '')
        self.assertEqual(self.t('show', '-gqv', '@test-rechecked'), '')

    def test_old_workers_never_clear_or_rearm_newer_requests(self):
        for kind, fail in (('focus', 'focus-ack'), ('layout', 'layout-write')):
            for error in ('', fail):
                with self.subTest(kind=kind, fail=error):
                    result = self.run_worker(kind, error, pending='18')
                    self.assertEqual(result.returncode == 0, not error, result.stderr)
                    self.assertEqual(self.t('show', '-wqv', f'@agent-indicator-{kind}-request'), '18')
                    self.assertEqual(self.t('show', '-gqv', '@test-rechecked'), '')

    def test_successful_stale_focus_rechecks_the_current_pane(self):
        self.t('split-window', '-h', '/bin/bash --noprofile --norc')
        result = self.run_worker('focus')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.t('show', '-wqv', '@agent-indicator-focus-request'), '')
        self.assertEqual(self.t('show', '-gqv', '@test-rechecked'), 'yes')


if __name__ == '__main__':
    unittest.main()
