"""Native tmux admission gates for periodic work; no idle checker process."""
from pathlib import Path
import shlex
import subprocess
import time
import uuid


def commands(items):
    escapes = {'\\': '\\\\', '"': '\\"', '$': '\\$', '\n': '\\n', '\r': '\\r', '\t': '\\t'}
    def quote(value):
        return '"' + ''.join(escapes.get(c, c) for c in str(value)) + '"'
    return subprocess.check_output(['tmux', 'source-file', '-'],
        input='\n'.join(' '.join(quote(v) for v in item) for item in items) + '\n', text=True, timeout=10)


def install(kind, command):
    prefix = '@agent-indicator-' + kind
    now = '#{T:@agent-indicator-now}'
    claim = '#{' + prefix + '-claim}'
    due = '#{?' + prefix + '-due,#{' + prefix + '-due},0}'
    expiry = 180 if kind == 'recovery' else 30
    expired = '#{e|>=:' + now + ',#{e|+:#{' + prefix + '-claimed-at},' + str(expiry) + '}}'
    gate = '#{&&:#{e|>=:' + now + ',' + due + '},#{||:#{==:' + claim + ',},' + expired + '}}'
    if kind == 'recovery':
        gate = '#{&&:#{==:#{@agent-indicator-recovery},on},#{&&:#{!=:#{@continuum-save-interval},0},' + gate + '}}'
    worker = shlex.join([str(arg) for arg in command]) + " --scheduled '" + claim + "'"
    # Native commands execute in one queue. Later clients see the claim before
    # they can launch a second worker. A reload changes the generation, fencing
    # off old workers even when the next claim falls within the same second.
    serial = '#{e|+:#{?' + prefix + '-serial,#{' + prefix + '-serial},0},1}'
    launch = ("if-shell -F '#{E:" + prefix + "-gate}' { set-option -gF " + prefix + "-serial '" + serial
              + "' ; set-option -gF " + prefix + "-claim '#{" + prefix + "-generation}-#{" + prefix + "-serial}'"
              + " ; set-option -gF " + prefix + "-claimed-at '" + now + "' ; run-shell -b " + shlex.quote(worker) + ' }')
    wrapper = shlex.join([str(Path(__file__).with_name('schedule.sh')), kind])
    status = '#{?#{E:' + prefix + '-gate},#(' + wrapper + '),}'
    commands([['set-option', '-gq', '@agent-indicator-now', '%s'],
              ['set-option', '-gq', prefix + '-generation', uuid.uuid4().hex],
              ['set-option', '-gqu', prefix + '-claim'],
              ['set-option', '-gq', prefix + '-gate', gate],
              ['set-option', '-gq', prefix + '-launch', launch],
              ['set-option', '-gq', prefix + '-status', status]])


def valid(token):
    return bool(token) and all(c in '0123456789abcdef-' for c in token)


def owns(kind, token):
    if token is None:
        return True
    if not valid(token):
        return False
    return commands([['display-message', '-p', '#{==:#{@agent-indicator-' + kind + '-claim},' + token + '}']]).strip() == '1'


def finish(kind, token, delay):
    if token is None or not valid(token):
        return
    prefix = '@agent-indicator-' + kind
    commands([['if-shell', '-F', '#{==:#{' + prefix + '-claim},' + token + '}',
               f'set-option -gq {prefix}-due {int(time.time()) + delay} ; set-option -gqu {prefix}-claim']])


def clear(kind):
    commands([['set-option', '-gqu', '@agent-indicator-' + kind + '-' + field]
              for field in ('claim', 'claimed-at', 'generation', 'serial', 'due', 'gate', 'launch', 'status')])
