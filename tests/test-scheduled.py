#!/usr/bin/env python3
"""Exercise native admission, expiry and generation fencing on a real server."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import scheduled


def main():
    with tempfile.TemporaryDirectory(prefix='agent-schedule-') as directory:
        socket = str(Path(directory) / 'socket')
        env = dict(os.environ)
        env.pop('TMUX', None)
        def t(*args):
            return subprocess.check_output(['tmux', '-S', socket, *args], text=True, env=env, timeout=15).strip()
        try:
            t('-f', '/dev/null', 'new-session', '-d', '-s', 'test', 'sleep 300')
            os.environ['TMUX'] = t('display', '-p', '#{socket_path},#{pid},0')
            log = Path(directory) / 'runs'
            worker = Path(directory) / 'worker'
            worker.write_text('#!/bin/sh\nprintf "%s\\n" "$2" >> ' + shlex.quote(str(log)) + '\n')
            worker.chmod(0o755)
            scheduled.install('sweep', [worker])
            assert t('display', '-p', '#{E:@agent-indicator-sweep-gate}') == '1'
            for _ in range(5):
                t('run-shell', '-C', '#{@agent-indicator-sweep-launch}')
            deadline = time.monotonic() + 10
            while not log.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            token = t('show', '-gqv', '@agent-indicator-sweep-claim')
            assert log.read_text().splitlines() == [token], (log.read_text(), token)
            assert t('display', '-p', '#{E:@agent-indicator-sweep-gate}') == '0'
            assert scheduled.owns('sweep', token)
            scheduled.finish('sweep', token, 60)
            assert not scheduled.owns('sweep', token)
            assert t('display', '-p', '#{E:@agent-indicator-sweep-gate}') == '0'
            assert '#(' not in t('display', '-p', '#{E:@agent-indicator-sweep-status}')
            print('PASS: native sweep admission coalesces clients and closes idle status jobs', flush=True)
            t('set', '-g', '@agent-indicator-sweep-due', '0')
            t('run-shell', '-C', '#{@agent-indicator-sweep-launch}')
            replacement = t('show', '-gqv', '@agent-indicator-sweep-claim')
            assert token != replacement
            scheduled.finish('sweep', token, 600)
            assert scheduled.owns('sweep', replacement)
            t('set', '-g', '@agent-indicator-sweep-claimed-at', str(int(time.time()) - 31))
            assert t('display', '-p', '#{E:@agent-indicator-sweep-gate}') == '1'
            scheduled.install('sweep', [worker])
            t('run-shell', '-C', '#{@agent-indicator-sweep-launch}')
            assert not scheduled.owns('sweep', replacement)
            print('PASS: failed jobs expire and stale completions cannot clear a newer generation', flush=True)
            t('set', '-g', '@agent-indicator-recovery', 'on')
            scheduled.install('recovery', [worker])
            assert t('display', '-p', '#{E:@agent-indicator-recovery-gate}') == '1'
            t('set', '-g', '@continuum-save-interval', '0')
            assert t('display', '-p', '#{E:@agent-indicator-recovery-gate}') == '0'
            scheduled.clear('sweep')
            assert not t('show', '-gqv', '@agent-indicator-sweep-status')
            print('PASS: unset recovery interval is enabled; zero disables it and cleanup removes gates', flush=True)
        finally:
            subprocess.run(['tmux', '-S', socket, 'kill-server'], env=env, capture_output=True)


if __name__ == '__main__':
    main()
