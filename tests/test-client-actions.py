#!/usr/bin/env python3
"""Exercise asynchronous cancellation and native session hooks with real clients."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shlex
import struct
import subprocess
import tempfile
import termios
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def wait_for(check, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.03)
    raise AssertionError('timed out waiting for client action')


def main():
    with tempfile.TemporaryDirectory(prefix='test-client-actions-') as tmp:
        work = Path(tmp)
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'USER') if key in os.environ}
        env.update(HOME=tmp, ZDOTDIR=tmp, XDG_STATE_HOME=tmp, XDG_CONFIG_HOME=tmp,
                   TMUX_TMPDIR=tmp, SHELL='/bin/bash', TERM='xterm-256color')
        subprocess.run(['bash', '-c', 'before="$-:$SHELLOPTS"; source "$1"; test "$before" = "$-:$SHELLOPTS"',
                        'fixture', str(ROOT / 'scripts/session-hooks.sh')], env=env, check=True)
        socket = work / 'socket'
        stop = threading.Event()
        clients = []
        output = bytearray()

        def t(*args, check=True):
            result = subprocess.run(['tmux', '-S', str(socket), *args], env=env,
                                    capture_output=True, text=True, timeout=15)
            if check and result.returncode:
                raise AssertionError((args, result.stdout, result.stderr))
            return result.stdout.strip()

        def attach(session):
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 120, 0, 0))
            def drain():
                while not stop.is_set():
                    if select.select([master], [], [], .1)[0]:
                        try:
                            output.extend(os.read(master, 65536))
                        except OSError:
                            return
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            client = subprocess.Popen(['tmux', '-S', str(socket), 'attach', '-t', session],
                                      env=env, stdin=slave, stdout=slave, stderr=slave)
            clients.append((client, master, slave, reader))
            wait_for(lambda: str(client.pid) in t('list-clients', '-F', '#{client_pid}').splitlines())
            name = next(row.split('|')[1] for row in t('list-clients', '-F', '#{client_pid}|#{client_name}').splitlines()
                        if row.split('|')[0] == str(client.pid))
            return client, master, name

        def state(pane, turn):
            call_env = dict(env, TMUX=t('display', '-p', '#{socket_path},#{pid},0'), TMUX_PANE=pane)
            subprocess.run([str(ROOT / 'scripts/agent-state.sh'), '--hook', '--agent', 'claude',
                            '--state', 'running'], env=call_env, text=True, check=True,
                           input=json.dumps(dict(session_id='fixture', prompt_id=turn, hook_event_name='UserPromptSubmit')),
                           capture_output=True, timeout=15)
            t('run', shlex.join(['python3', str(ROOT / 'scripts/state-lock.py'), '--wait', '/dev/null']))

        def seen(session):
            return t('show', '-qv', '-t', session, '@agent-indicator-session-seen') == '1'

        try:
            marker = work / 'interrupt-seen'
            fixture = work / 'fixture.py'
            fixture.write_text('import signal,time\nfrom pathlib import Path\n'
                               f'signal.signal(signal.SIGINT, lambda *_: Path({str(marker)!r}).touch())\n'
                               'print("ready", flush=True)\nwhile True: time.sleep(.1)\n')
            pane = t('-f', '/dev/null', 'new-session', '-d', '-s', 'one', '-P', '-F', '#{pane_id}',
                     shlex.join(['python3', str(fixture)]))
            t('set', '-g', '@agent-indicator-notification-enabled', 'off')
            # An installed older hook must disappear when the plugin is reloaded.
            t('set-hook', '-g', 'client-session-changed', f'run-shell "{ROOT}/scripts/session-changed.sh"')
            t('run', str(ROOT / 'agent-indicator.tmux'))
            t('bind', '-n', 'F11', 'set', '-g', '@key-responsive', 'yes')
            client, master, name = attach('one')
            wait_for(lambda: 'ready' in t('capture-pane', '-p', '-t', pane))
            state(pane, 'first')
            lock_path = Path(f"{socket}.agent-indicator-{t('display', '-p', '#{pid}')}.lock")
            with lock_path.open('a') as lock:
                fcntl.lockf(lock, fcntl.LOCK_EX)
                # tmux considers a raw format value beginning with 0 false.
                # Force that boundary in both the work record and its key binding.
                token = '0' + 'a' * 31
                data = json.loads(t('show', '-gqv', '@agent-indicator-notifications'))
                data['panes'][pane]['interrupt'] = token
                t('set', '-gq', '@agent-indicator-notifications', json.dumps(data), ';',
                  'set', '-pq', '-t', pane, '@agent-indicator-interrupt', token)
                os.write(master, b'\x03\033[23~')
                wait_for(marker.exists, seconds=2)
                wait_for(lambda: t('show', '-gqv', '@key-responsive') == 'yes', seconds=2)
                assert t('show', '-pqv', '-t', pane, '@agent-indicator-interrupt')
            wait_for(lambda: not t('show', '-pqv', '-t', pane, '@agent-indicator-interrupt'))
            wait_for(lambda: not t('show-environment', '-g', f'TMUX_AGENT_PANE_{pane}_STATE', check=False))
            print('PASS: Ctrl+C with a zero-prefixed token cleans up without blocking the next key', flush=True)

            state(pane, 'old')
            old = t('show', '-pqv', '-t', pane, '@agent-indicator-interrupt')
            state(pane, 'new')
            current = t('show', '-pqv', '-t', pane, '@agent-indicator-interrupt')
            assert current and old != current
            t('run', shlex.join([str(ROOT / 'scripts/pane-interrupt.sh'), pane, old]))
            assert t('show', '-pqv', '-t', pane, '@agent-indicator-interrupt') == current
            assert t('show-environment', '-g', f'TMUX_AGENT_PANE_{pane}_STATE').endswith('=running')
            print('PASS: delayed cancellation cannot clear a newer Claude turn', flush=True)

            strange = 'odd \' " ; $PATH ##{client_pid}'
            second = t('new-session', '-d', '-s', strange, '-P', '-F', '#{session_id}', 'sleep 300')
            strange = t('display', '-p', '-t', second, '#{session_name}')
            assert '#{client_pid}' in strange, 'fixture must retain literal format syntax in its name'
            extra = t('new-window', '-d', '-t', second, '-P', '-F', '#{window_id}', 'sleep 300')
            third = t('new-session', '-d', '-s', 'three', '-P', '-F', '#{session_id}', 'sleep 300')
            other, other_master, other_name = attach(third)
            t('set-hook', '-g', 'client-session-changed[999]',
              'set-option -gF @switch-context "#{hook_client}|#{hook_session}|#{client_name}|#{session_id}"')
            t('switch-client', '-c', name, '-t', second)
            wait_for(lambda: seen('one'))
            t('set', '-qu', '-t', 'one', '@agent-indicator-session-seen')
            t('switch-client', '-c', name, '-t', extra)
            time.sleep(.1)
            assert not seen('one'), 'same-session window switch acknowledged an old session again'
            t('switch-client', '-c', other_name, '-t', 'one')
            wait_for(lambda: seen('three'))
            assert not seen(second), 'one client acknowledged the other client\'s session'
            # The destination already has a more recently active terminal.
            # Hook formats must use the switching client, not the destination's
            # preferred client. A real key ensures the other client is newest.
            time.sleep(1.1)
            t('set', '-g', '@key-responsive', '')
            os.write(other_master, b'\033[23~')
            wait_for(lambda: t('show', '-gqv', '@key-responsive') == 'yes')
            one = t('display', '-p', '-t', 'one', '#{session_id}')
            t('switch-client', '-c', name, '-t', 'one')
            wait_for(lambda: t('show', '-gqv', '@agent-indicator-client-session-' + name) == one)
            assert seen(second), 'switching into an occupied session missed the session being left'
            assert not seen('one'), 'switching credited the destination client instead of the source'
            assert t('show', '-gqv', '@agent-indicator-client-session-' + other_name) == one
            t('switch-client', '-c', name, '-t', second)
            wait_for(lambda: seen('one'))
            t('set', '-qu', '-t', second, '@agent-indicator-session-seen')
            print('PASS: switching into another active terminal\'s session credits only the switching terminal', flush=True)
            t('switch-client', '-c', name, '-t', third)
            wait_for(lambda: seen(second))
            t('set-hook', '-gu', 'client-session-changed[999]')
            hooks = t('show-hooks', '-g', 'client-session-changed')
            assert 'session-changed.sh' not in hooks and 'run-shell -C' in hooks
            t('run', str(ROOT / 'agent-indicator.tmux'))
            assert len(t('show-hooks', '-g', 'client-session-changed').splitlines()) == 1
            t('set', '-qu', '-t', second, '@agent-indicator-session-seen')
            t('switch-client', '-c', name, '-t', third)
            time.sleep(.1)
            assert not seen(second), 'reload lost the client\'s current session'
            t('detach-client', '-t', other_name)
            wait_for(lambda: not t('show', '-gqv', '@agent-indicator-client-session-' + other_name))
            print('PASS: native session hooks track each client, skip window-only switches, quote names, and survive reload', flush=True)

            # The session-dot renderer consumes the same marker, including
            # after renames. A new question clears acknowledgement by ID.
            odd_pane = t('display', '-p', '-t', second, '#{pane_id}')
            call_env = dict(env, TMUX=t('display', '-p', '#{socket_path},#{pid},0'), TMUX_PANE=odd_pane)
            for key, value in [('active', 'A'), ('inactive', 'I'), ('attention', 'X'),
                               ('color', ''), ('active-color', ''), ('attention-color', '')]:
                t('set', '-g', '@agent-indicator-session-dots-' + key, value)
            def question():
                subprocess.run([str(ROOT / 'scripts/agent-state.sh'), '--agent', 'claude',
                                '--state', 'needs-input'], env=call_env, check=True, capture_output=True)
                t('run', shlex.join(['python3', str(ROOT / 'scripts/state-lock.py'), '--wait', '/dev/null']))
            def dots():
                return subprocess.check_output([str(ROOT / 'scripts/session-dots.sh'), 'three'],
                                               env=call_env, text=True)
            question()
            assert not seen(second) and dots().count('X') == 1
            t('set', '-t', second, '@agent-indicator-session-seen', '1')
            assert dots().count('X') == 0
            t('rename-session', '-t', second, 'renamed ##{client_pid}')
            assert seen(second) and dots().count('X') == 0
            question()
            assert not seen(second) and dots().count('X') == 1
            t('set', '-t', second, '@agent-indicator-session-seen', '1')
            t('run', str(ROOT / 'scripts/reset_all.sh'))
            t('run', shlex.join(['python3', str(ROOT / 'scripts/state-lock.py'), '--wait', '/dev/null']))
            assert not seen(second) and not t('show', '-pqv', '-t', pane, '@agent-indicator-interrupt')
            print('PASS: session dots retain acknowledgement across renames and clear it on new work or reset', flush=True)
            t('kill-session', '-t', third)
            current = t('list-clients', '-F', '#{session_id}')
            wait_for(lambda: t('show', '-gqv', '@agent-indicator-client-session-' + name) == current)
            assert "can't find session" not in t('show-messages', '-t', name)
            print('PASS: closing the active session updates native bookkeeping without an error', flush=True)
        except Exception:
            print('clients:', t('list-clients', '-F', '#{client_name}|#{session_id}|#{client_activity}', check=False))
            print('switch context:', t('show', '-gqv', '@switch-context', check=False))
            print('remembered:', '\n'.join(line for line in t('show', '-g', check=False).splitlines()
                                           if line.startswith('@agent-indicator-client-session-')))
            print('seen:', t('list-sessions', '-F', '#{session_id}|#{session_name}|#{@agent-indicator-session-seen}', check=False))
            print('native hook:', t('show-hooks', '-g', 'client-session-changed', check=False))
            print('terminal tail:', repr(output[-500:]))
            raise
        finally:
            t('kill-server', check=False)
            stop.set()
            for client, master, slave, reader in clients:
                client.wait(timeout=5)
                reader.join(timeout=2)
                os.close(master)
                os.close(slave)


if __name__ == '__main__':
    main()
