#!/usr/bin/env python3
"""Install native gates; focus and layout state mutations retain the FIFO writer."""
from pathlib import Path
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True
import scheduled
import runtime

DIRECTORY = Path(__file__).resolve().parent


def register(event, command, match, slot=900):
    previous = subprocess.check_output(['tmux', 'show-hooks', '-g', event], text=True)
    remove = [['set-hook', '-gu', line.split(' ', 1)[0]] for line in previous.splitlines() if match in line]
    occupied = {line.split(' ', 1)[0] for line in previous.splitlines() if match not in line}
    while f'{event}[{slot}]' in occupied:
        slot += 1
    scheduled.commands(remove + [['set-hook', '-g', f'{event}[{slot}]', command]])


def claim_command(kind, gate, worker):
    option = '@agent-indicator-' + kind + '-request'
    available = '#{||:#{==:#{' + option + '},},#{e|>=:#{T:@agent-indicator-now},#{e|+:#{' + option + '},60}}}'
    return ("if-shell -F '#{&&:" + gate + ',' + available + "}' { set-option -wqF " + option
            + " '#{T:@agent-indicator-now}' ; run-shell -b " + shlex.quote(worker + " '#{" + option + "}'") + ' }')


def setup():
    focus = shlex.quote(str(DIRECTORY / 'pane-focus-in.sh')) + " '#{pane_id}' '#{window_id}'"
    gate = '#{&&:#{&&:#{pane_active},#{window_active}},#{||:#{@agent-indicator-ack-needed},#{@agent-indicator-ack-held}}}'
    focus_command = claim_command('focus', gate, focus)
    scheduled.commands([['set-option', '-gq', '@agent-indicator-focus-launch', focus_command]])
    for event in ('pane-focus-in', 'after-select-window', 'after-select-pane', 'session-window-changed'):
        register(event, focus_command, 'pane-focus-in.sh')
    layout = '#{P:#{pane_id}:#{pane_pid}:#{pane_dead}|}'
    # A tracked pane can arrive in a previously plain window. Its published
    # owner follows the pane; checking only the destination window loses moves.
    incoming = '#{P:#{?#{==:#{@agent-indicator-track-owner},#{pane_pid}},1,}}'
    active = '#{||:#{@agent_present},#{&&:#{!=:#{@agent-indicator-state},},#{!=:#{@agent-indicator-state},idle}}}'
    gate = '#{&&:#{||:' + active + ',' + incoming + '},#{!=:#{@agent-indicator-layout},' + layout + '}}'
    worker = shlex.quote(str(DIRECTORY / 'window-state.sh')) + " --layout '#{window_id}'"
    layout_command = claim_command('layout', gate, worker)
    scheduled.commands([['set-option', '-gq', '@agent-indicator-layout-launch', layout_command]])
    for event in ('window-layout-changed', 'pane-exited', 'pane-died'):
        register(event, layout_command, 'window-state.sh')
    # When active-window styling is disabled, an active window can have a
    # deliberately deferred paint. Repaint just those windows on departure.
    worker = shlex.quote(str(DIRECTORY / 'window-state.sh')) + " --style-only '#{window_id}'"
    needed = '#{&&:#{@agent-indicator-repaint-needed},#{!=:#{window_active},1}}'
    commands = '#{W:#{?' + needed + ',run-shell -b ' + shlex.quote(worker) + ' ; ,}}'
    register('session-window-changed', "if-shell -F '#{W:#{?" + needed + ",1,}}' { run-shell -C "
             + shlex.quote(commands) + ' }', '@agent-indicator-repaint-needed', 902)


def disable(target):
    target = Path(target).resolve()
    try:
        installed = subprocess.check_output(['tmux', 'show-environment', '-g', 'TMUX_AGENT_INDICATOR_DIR'],
                                            text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.SubprocessError:
        return
    if installed != 'TMUX_AGENT_INDICATOR_DIR=' + str(target):
        return
    hooks = subprocess.check_output(['tmux', 'show-hooks', '-g'], text=True)
    entries = [['set-hook', '-gu', line.split(' ', 1)[0]] for line in hooks.splitlines()
               if any(str(target / 'scripts' / name) in line for name in
                      ('pane-focus-in.sh', 'window-state.sh')) or '@agent-indicator-repaint-needed' in line]
    windows = subprocess.check_output(['tmux', 'list-windows', '-a', '-F', '#{window_id}'], text=True).splitlines()
    entries.extend(['set-option', '-wqu', '-t', window, '@agent-indicator-' + field]
                   for window in set(windows) for field in
                   ('ack-needed', 'ack-held', 'focus-request', 'layout', 'layout-request', 'repaint-needed'))
    panes = subprocess.check_output(['tmux', 'list-panes', '-a', '-F', '#{pane_id}'], text=True).splitlines()
    entries.extend(['set-option', '-pqu', '-t', pane, '@agent-indicator-track-owner'] for pane in panes)
    scheduled.commands(entries)
    scheduled.clear('sweep')
    scheduled.commands([['set-option', '-gqu', field] for field in
                        ('@agent-indicator-now', '@agent-indicator-focus-launch', '@agent-indicator-layout-launch')])
    # Remove only dead arrival markers; live hooks still own their FIFO slots.
    list(runtime.arrivals())
    server = runtime.from_environment()
    if server:
        (runtime.ROOT / 'servers' / (str(server['pid']) + '.json')).unlink(missing_ok=True)


if __name__ == '__main__':
    if sys.argv[1:2] == ['disable']:
        disable(sys.argv[2])
    else:
        setup()
