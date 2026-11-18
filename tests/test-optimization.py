#!/usr/bin/env python3
"""Regression coverage for admission, ownership and unchanged recovery saves."""
import importlib.util
import io
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
sys.path.insert(0, str(ROOT / 'scripts'))
import runtime
import scheduled

spec = importlib.util.spec_from_file_location('recovery', ROOT / 'scripts/agent-recovery.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
spec = importlib.util.spec_from_file_location('hook_entry', ROOT / 'scripts/hook-entry.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class RecoveryGate(unittest.TestCase):
    def test_fingerprint_and_failed_backstop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'last').write_text('saved')
            with patch.object(recovery, 'fingerprint', return_value='one'), patch.object(recovery, 'save', return_value=True) as save:
                self.assertTrue(recovery.checked_save(root))
                self.assertEqual(recovery.checked_save(root), 'unchanged')
                self.assertEqual(save.call_count, 1)
                self.assertTrue(recovery.checked_save(root, force=True))
                self.assertEqual(save.call_count, 2)
                (root / 'health.json').write_text(json.dumps(dict(error='save failed')))
                self.assertTrue(recovery.checked_save(root))
                self.assertEqual(save.call_count, 3)
                (root / 'health.json').unlink()
                old = json.loads((root / 'fingerprint.json').read_text())
                old['saved'] -= 901
                (root / 'fingerprint.json').write_text(json.dumps(old))
                self.assertTrue(recovery.checked_save(root))
                self.assertEqual(save.call_count, 4)

    def test_failed_or_changed_save_does_not_advance_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(recovery, 'fingerprint', return_value='one'), patch.object(recovery, 'save', return_value=False):
                self.assertFalse(recovery.checked_save(root))
                self.assertFalse((root / 'fingerprint.json').exists())
            with patch.object(recovery, 'fingerprint', side_effect=['one', 'two']), patch.object(recovery, 'save', return_value=True):
                self.assertFalse(recovery.checked_save(root))
                self.assertFalse((root / 'fingerprint.json').exists())

    def test_process_birth_and_marker_owner(self):
        info = runtime.process(os.getpid())
        self.assertEqual(info['ppid'], os.getppid())
        self.assertTrue(runtime.same_start(info['started'], info['started']))
        self.assertFalse(runtime.same_start(info['started'] + 1, info['started']))
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'marker'
            marker.write_text(str(os.getpid()))
            original = marker.stat().st_mtime_ns
            runtime.update_marker(marker, os.getpid(), 123)
            self.assertEqual(marker.stat().st_mtime_ns, original)
            self.assertTrue(runtime.marker_alive(marker))
            marker.write_text(f'{os.getpid()} 123 1')
            self.assertFalse(runtime.marker_alive(marker))

    def test_noop_cannot_use_a_snapshot_overlapped_by_a_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = dict(socket=str(root / 'socket'), pid=os.getpid())
            queue = Path(f'{identity["socket"]}.agent-indicator-{identity["pid"]}.lock.queue')
            queue.mkdir()
            active = queue / 'prior.run'
            active.write_text(runtime.marker_text(os.getpid(), identity['pid'], '%0'))

            def snapshot(*args, **kwargs):
                # The query saw running, then the permission writer published
                # its question and retired before the query's caller resumed.
                active.unlink()
                return 'P %0 1 0 \nTMUX_AGENT_PANE_%0_STATE=running\n'

            with patch.object(runtime, 'ROOT', root / 'runtime'), \
                    patch.object(runtime, 'identity', return_value=identity), \
                    patch.object(runtime, 'remember'), \
                    patch.object(entry.subprocess, 'check_output', side_effect=snapshot), \
                    patch.object(entry.os, 'execvp') as full, \
                    patch.dict(os.environ, dict(TMUX='fixture,1,0', TMUX_PANE='%0'), clear=True):
                entry.prepare(str(ROOT / 'scripts/agent-state.sh'),
                              ['--agent', 'claude', '--state', 'running', '--if-state', 'needs-input'])
            self.assertTrue(full.called, 'a possibly stale snapshot must take the locked path')

    def test_unwritten_arrival_is_conservatively_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'arrival'
            marker.touch()
            self.assertTrue(runtime.marker_affects(marker, '%0'))


def integration():
    with tempfile.TemporaryDirectory(prefix='agent-fast-hooks-') as directory:
        home = Path(directory)
        env = {k:v for k,v in os.environ.items() if not k.startswith(('TMUX', 'CLAUDE', 'CODEX', 'KIBITZ'))}
        # Assign explicitly: keep test fixtures independent of inherited servers.
        env['TMUX_AGENT_RUNTIME_ROOT'] = str(home / 'runtime')
        servers = []
        children = []
        def t(socket, *args):
            return subprocess.check_output(['tmux', '-S', socket, *args], env=env, text=True, timeout=20).strip()
        def wait(predicate, seconds=40):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if predicate(): return
                time.sleep(.05)
            raise AssertionError('timed out')
        try:
            for name in ('one', 'two'):
                socket = str(home / name)
                servers.append(socket)
                t(socket, '-f', '/dev/null', 'new-session', '-d', '-s', name, '/bin/bash --noprofile --norc')
                t(socket, 'set', '-g', '@agent-indicator-notification-enabled', 'off')
                t(socket, 'set', '-g', '@agent-indicator-input-delay', '0')
                t(socket, 'run', shlex.join(['python3', str(ROOT/'scripts/runtime.py'), 'register']))
            one, two = servers
            addresses = [t(socket, 'display', '-p', '#{socket_path},#{pid},0') for socket in servers]
            hook = str(ROOT / 'scripts/agent-state.sh')
            for prefix in (['env', '-u', 'TMUX', '-u', 'TMUX_PANE'], ['env', 'TMUX=' + addresses[1], 'TMUX_PANE=%99']):
                done = home/'done'
                done.unlink(missing_ok=True)
                cmd = shlex.join([*prefix, hook, '--agent', 'codex', '--state', 'running'])
                t(one, 'send-keys', '-t', '%0', '-l', cmd + ' > ' + shlex.quote(str(home/'output')) + ' 2>&1; touch ' + shlex.quote(str(done)))
                t(one, 'send-keys', '-t', '%0', 'Enter')
                wait(done.exists)
                assert 'TMUX_AGENT_PANE_%0_STATE=running' in t(one, 'show-environment', '-g'), (home/'output').read_text()
                assert 'TMUX_AGENT_PANE_%0_STATE=' not in t(two, 'show-environment', '-g')
                t(one, 'set-environment', '-gu', 'TMUX_AGENT_PANE_%0_STATE')
            print('PASS: missing/stale TMUX and pane variables route Codex to its ancestor server', flush=True)
            inside = dict(env, TMUX=addresses[0], TMUX_PANE='%0')
            def launch(state, extra=()):
                p = subprocess.Popen([hook, '--agent', 'claude', '--state', state, '--hook', *extra],
                                     env=inside, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                children.append(p)
                return p
            permission = launch('needs-input')
            arrivals = home/'runtime/arrivals'
            wait(lambda: arrivals.exists() and len(list(arrivals.iterdir())) > 0)
            # Wait for the first hook to reach its blocking stdin read, before
            # releasing a later conditional event whose snapshot is still off.
            time.sleep(.3)
            answer = launch('running', ('--if-state', 'needs-input'))
            answer.stdin.write(json.dumps(dict(session_id='order', hook_event_name='PostToolUse', tool_use_id='same')))
            answer.stdin.close()
            answer.stdin = None
            time.sleep(.5)
            permission.communicate(json.dumps(dict(session_id='order', hook_event_name='PermissionRequest', tool_use_id='same')), timeout=40)
            assert permission.returncode == 0
            answer.communicate(timeout=40)
            assert answer.returncode == 0
            wait(lambda: t(one, 'show', '-wqv', '@agent-indicator-state') == 'running')
            print('PASS: a conditional hook cannot overtake a question still reading its payload', flush=True)
            t(one, 'run', str(ROOT / 'agent-indicator.tmux'))
            t(one, 'run', shlex.join(['python3', str(ROOT/'scripts/state-lock.py'), '--wait', '--barrier']))
            for state in ('off', 'running'):
                t(one, 'set', '-g', '@agent-indicator-running-window-title-bg', 'yellow', ';',
                  'set', '-g', '@agent-indicator-running-window-title-fg', 'black', ';',
                  'set', '-g', '@agent-indicator-style-active-window', 'off')
                subprocess.run([hook, '--agent', 'claude', '--state', state], env=inside,
                               check=True, capture_output=True, timeout=40)
            assert t(one, 'show', '-wqv', '-t', '@0', '@agent-indicator-repaint-needed') == '1'
            assert t(one, 'display', '-p', '-t', '@0',
                     '#{==:#{@agent-indicator-layout},#{P:#{pane_id}:#{pane_pid}:#{pane_dead}|}}') == '1'
            t(one, 'new-window', '-n', 'other', 'sleep 300')
            wait(lambda: set(t(one, 'show', '-wqv', '-t', '@0', 'window-status-style').split(',')) == {'fg=black', 'bg=yellow'})
            wait(lambda: t(one, 'show', '-wqv', '-t', '@0', '@agent-indicator-repaint-needed') == '0')
            spare = t(one, 'split-window', '-d', '-t', '@0', '-P', '-F', '#{pane_id}', 'sleep 300')
            t(one, 'join-pane', '-d', '-s', '%0', '-t', '@1')
            wait(lambda: t(one, 'show', '-wqv', '-t', '@1', '@agent-indicator-state') == 'running')
            wait(lambda: t(one, 'show', '-wqv', '-t', '@0', '@agent-indicator-state') == 'idle')
            t(one, 'join-pane', '-d', '-s', '%0', '-t', spare)
            wait(lambda: t(one, 'show', '-wqv', '-t', '@0', '@agent-indicator-state') == 'running')
            wait(lambda: t(one, 'show', '-wqv', '-t', '@1', '@agent-indicator-state') == 'idle')
            print('PASS: moving a tracked pane reconciles both its old window and a previously plain destination', flush=True)
            # Leave a dead pane in place; pane IDs alone would miss this change.
            t(one, 'set', '-w', '-t', '@0', 'remain-on-exit', 'on')
            t(one, 'respawn-pane', '-k', '-t', '%0', '/bin/true')
            wait(lambda: t(one, 'show', '-wqv', '-t', '@0', '@agent-indicator-state') == 'idle')
            print('PASS: leaving an unstyled active window repaints it, and dead panes invalidate the layout gate', flush=True)
            # Native request coalescing and fencing, without executing a save.
            previous = os.environ.get('TMUX')
            os.environ['TMUX'] = addresses[0]
            try:
                t(one, 'set', '-g', '@agent-indicator-now', '%s', ';', 'set', '-g', '@agent-indicator-recovery', 'on', ';',
                  'set', '-g', '@agent_recovery_generation', 'abc')
                for _ in range(5): recovery.request_save(120)
                token = t(one, 'show', '-gqv', '@agent_recovery_pending')
                assert token == 'abc-1', token
                assert not recovery.take_request('abc-0')
                assert recovery.take_request(token)
                recovery.request_save(120)
                assert not recovery.take_request(token)
                assert t(one, 'show', '-gqv', '@agent_recovery_pending') == 'abc-2'
                # Ending an old SSH launcher must not clear a newer record.
                record = json.dumps(dict(kind='ssh', pid=1234))
                t(one, 'set', '-pq', '-t', '%0', '@ssh_reconnect_pid', '1234', ';',
                  'set', '-pq', '-t', '%0', '@agent_recovery', record)
                recovery.finish_ssh('%0', '123')
                assert t(one, 'show', '-pqv', '-t', '%0', '@agent_recovery') == record
                recovery.finish_ssh('%0', '1234')
                assert not t(one, 'show', '-pqv', '-t', '%0', '@agent_recovery')
                print('PASS: recovery requests coalesce, stale requests are fenced, SSH cleanup checks ownership', flush=True)
            finally:
                if previous is None: os.environ.pop('TMUX', None)
                else: os.environ['TMUX'] = previous
        except Exception:
            for socket in servers:
                print('FAILED SERVER:', socket, flush=True)
                for command in (
                        ['list-windows', '-a', '-F', '#{window_id}|#{@agent-indicator-state}|request=#{@agent-indicator-layout-request}|saved=#{@agent-indicator-layout}|live=#{P:#{pane_id}:#{pane_pid}:#{pane_dead}|}'],
                        ['list-panes', '-a', '-F', '#{pane_id}|#{window_id}|#{pane_pid}|#{pane_dead}|owner=#{@agent-indicator-track-owner}'],
                        ['show-environment', '-g'],
                        ['show-hooks', '-g', 'window-layout-changed']):
                    result = subprocess.run(['tmux', '-S', socket, *command], env=env,
                                            text=True, capture_output=True, timeout=10)
                    if command[0] == 'show-environment':
                        print('\n'.join(line for line in result.stdout.splitlines()
                                        if line.startswith('TMUX_AGENT_PANE_')), flush=True)
                    else:
                        print(result.stdout + result.stderr, flush=True)
            raise
        finally:
            for p in children:
                if p.poll() is None: p.kill(); p.wait()
            for socket in servers:
                subprocess.run(['tmux', '-S', socket, 'kill-server'], env=env, capture_output=True)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RecoveryGate))
    if not result.wasSuccessful(): sys.exit(1)
    integration()
