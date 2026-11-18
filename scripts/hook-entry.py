#!/usr/bin/env python3
"""Validate one hook, then skip proven no-ops or enter the existing FIFO writer."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.dont_write_bytecode = True
import runtime


def pending_scopes(identity, marker):
    queue = Path(f"{identity['socket']}.agent-indicator-{identity['pid']}.lock.queue")
    paths = list(queue.iterdir()) if queue.exists() else []
    paths.extend(runtime.arrivals(exclude=marker, server_pid=identity['pid']))
    return {scope for path in paths if (scope := runtime.marker_scope(path)) is not None}


def prepare(script, arguments):
    received = os.environ.get('TMUX_AGENT_RECEIVED_AT') or str(time.time())
    marker = Path(os.environ['TMUX_AGENT_ARRIVAL']) if os.environ.get('TMUX_AGENT_ARRIVAL') else None
    if marker is None:
        marker = runtime.private_directory('arrivals') / (str(os.getpid()) + '-' + str(time.monotonic_ns()))
        marker.write_text(str(os.getpid()))
    os.environ['TMUX_AGENT_ARRIVAL'] = str(marker)
    os.environ['TMUX_AGENT_RECEIVED_AT'] = received
    try:
        agent = arguments[arguments.index('--agent') + 1]
        expected = arguments[arguments.index('--if-state') + 1] if '--if-state' in arguments else None
        payload = None
        if '--hook' in arguments and not sys.stdin.isatty():
            import importlib.util
            spec = importlib.util.spec_from_file_location('notification_state', Path(script).with_name('notification-state.py'))
            state = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(state)
            payload = json.load(sys.stdin)
            if not state.terminal_hook(agent, payload):
                return
        parents = runtime.ancestors(os.getpid())
        ancestor_server = next((pid for pid, p in parents.items() if Path(p['name']).name.startswith('tmux')), None)
        if agent != 'codex' and os.environ.get('TMUX'):
            # Claude/OpenCode entrypoints also support explicit pane targeting
            # by external callers. A valid supplied server is authoritative.
            try:
                identity = runtime.identity()
            except (OSError, RuntimeError, subprocess.SubprocessError):
                identity = runtime.hook_server(parents)
        elif ancestor_server is not None:
            identity = runtime.hook_server(parents)
        elif os.environ.get('TMUX'):
            identity = runtime.identity()
        else:
            identity = runtime.hook_server(parents)
        if not identity:
            return
        os.environ['TMUX'] = f"{identity['socket']},{identity['pid']},0"
        runtime.remember(identity)
        runtime.update_marker(marker, os.getpid(), identity['pid'])
        # Remember overlapping writers before reading state. A writer can finish
        # and remove its marker while this query returns an older snapshot.
        busy = pending_scopes(identity, marker) if expected else set()
        # One client supplies ownership, pending questions and the legacy states.
        raw = subprocess.check_output(['tmux', 'list-panes', '-a', '-F',
            'P #{pane_id} #{pane_pid} #{pane_dead} #{@agent-indicator-input-pending}',
            ';', 'show-environment', '-g'], text=True, timeout=10)
        panes, states = {}, {}
        for line in raw.splitlines():
            if line.startswith('P '):
                fields = line.split(' ', 4)
                if len(fields) == 5 and fields[3] == '0':
                    panes[fields[1]] = (fields[2], fields[4])
            elif line.startswith('TMUX_AGENT_PANE_') and '=' in line:
                key, value = line.split('=', 1)
                states[key] = value
        if agent == 'codex':
            matches = [pane for pane, (pid, _) in panes.items() if int(pid) in parents]
            pane = matches[0] if len(matches) == 1 else None
        else:
            pane = os.environ.get('TMUX_PANE')
        if pane not in panes:
            return
        pid, pending = panes[pane]
        runtime.update_marker(marker, os.getpid(), identity['pid'], pane)
        if expected:
            busy.update(pending_scopes(identity, marker))
            current = states.get(f'TMUX_AGENT_PANE_{pane}_STATE', 'off')
            if current != expected and not pending and not busy.intersection(('*', pane)):
                return
        if payload is not None:
            os.environ['TMUX_AGENT_HOOK_DATA'] = json.dumps(state.hook_data(payload), separators=(',', ':'))
        os.environ['TMUX_AGENT_EVENT_PANE'] = pane
        os.environ['TMUX_AGENT_EVENT_PANE_PID'] = pid
        os.execvp('bash', ['bash', script, '--agent-event-prepared', *arguments])
    finally:
        marker.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        prepare(sys.argv[1], sys.argv[2:])
    except (OSError, ValueError, IndexError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'tmux-agent-indicator: hook preparation failed: {error}', file=sys.stderr)
        sys.exit(1)
