"""Cheap process identity and server discovery shared by one-shot entry points."""
import ctypes
import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

ROOT = Path(os.environ.get('TMUX_AGENT_RUNTIME_ROOT', '/tmp/tmux-agent-indicator-' + str(os.getuid())))


class BSDInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in
                ('flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in ('nfiles', 'pgid', 'jobc', 'tty', 'tpgid', 'nice')]
    _fields_ += [('seconds', ctypes.c_uint64), ('micros', ctypes.c_uint64)]


_libproc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True) if sys.platform == 'darwin' else None
_boot = None


def ps_process(pid):
    """Fallback for an inaccessible native process record, including login(1)."""
    try:
        raw = subprocess.check_output(['ps', '-p', str(pid), '-o', 'ppid=,stat=,lstart=,comm='],
                                      text=True, stderr=subprocess.DEVNULL, timeout=5,
                                      env=dict(os.environ, LC_ALL='C')).split(None, 7)
        if raw[1].startswith('Z'):
            return None
        return dict(pid=pid, ppid=int(raw[0]), started=start_number(' '.join(raw[2:7])), name=raw[7].strip())
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def process(pid):
    """Use native identity on macOS/Linux, with ps only for permission failures."""
    global _boot
    try:
        pid = int(pid)
        if pid < 1:
            return None
        if _libproc:
            info = BSDInfo()
            ctypes.set_errno(0)
            size = _libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            if size != ctypes.sizeof(info):
                if ctypes.get_errno() in (errno.EACCES, errno.EPERM):
                    return ps_process(pid)
                return None
            if info.status == 5:
                return None
            return dict(pid=pid, ppid=info.ppid, started=info.seconds * 1000000 + info.micros,
                        name=(info.name or info.comm).decode(errors='replace'))
        if sys.platform == 'linux':
            raw = Path(f'/proc/{pid}/stat').read_text()
            head, tail = raw.rsplit(')', 1)
            fields = tail.split()
            if fields[0] == 'Z':
                return None
            if _boot is None:
                _boot = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines()
                             if line.startswith('btime '))
            return dict(pid=pid, ppid=int(fields[1]), name=head.split('(', 1)[1],
                        started=_boot * 1000000 + int(fields[19]) * 1000000 // os.sysconf('SC_CLK_TCK'))
        return ps_process(pid)
    except PermissionError:
        return ps_process(pid)
    except (OSError, ValueError, IndexError, StopIteration, subprocess.SubprocessError):
        return None


def ancestors(pid):
    result = {}
    for _ in range(100):
        # Neither init nor ancestors above the owning server identify a pane.
        # Avoid a ps fallback for root-owned launchd on every ordinary hook.
        if pid == 1:
            break
        info = process(pid)
        if not info or info['pid'] in result:
            break
        result[info['pid']] = info
        if Path(info['name']).name.startswith('tmux'):
            break
        pid = info['ppid']
    return result


def start_number(value):
    if isinstance(value, (int, float)):
        return int(value)
    return int(time.mktime(time.strptime(' '.join(value.split()), '%a %b %d %H:%M:%S %Y'))) * 1000000


def same_start(saved, current):
    try:
        a, b = start_number(saved), start_number(current)
        # Existing live records use ps timestamps with whole-second precision.
        return a == b if isinstance(saved, (int, float)) and isinstance(current, (int, float)) else a // 1000000 == b // 1000000
    except (TypeError, ValueError, OverflowError):
        return False


def private_directory(name=''):
    for path in (ROOT, ROOT / name) if name else (ROOT,):
        path.mkdir(mode=0o700, exist_ok=True)
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            raise PermissionError('unsafe indicator runtime directory')
        path.chmod(0o700)
    return ROOT / name


def server(socket, pid):
    try:
        path = Path(socket)
        inode = path.stat()
        owner = process(pid)
        if (not stat.S_ISSOCK(inode.st_mode) or inode.st_uid != os.getuid()
                or not owner or not Path(owner['name']).name.startswith('tmux')):
            return None
        return dict(socket=str(path), pid=int(pid), started=owner['started'], device=inode.st_dev, inode=inode.st_ino)
    except (OSError, ValueError):
        return None


def server_alive(identity):
    return server(identity['socket'], identity['pid']) == identity


def from_environment():
    try:
        socket, pid, _session = os.environ['TMUX'].rsplit(',', 2)
        cached = json.loads((ROOT / 'servers' / (str(int(pid)) + '.json')).read_text())
        current = server(socket, pid)
        return current if cached == current else None
    except (OSError, KeyError, ValueError, TypeError):
        return None


def remember(identity):
    directory = private_directory('servers')
    path = directory / (str(identity['pid']) + '.json')
    value = json.dumps(identity, sort_keys=True)
    if path.exists() and path.read_text() == value:
        return
    temporary = path.with_suffix('.' + str(os.getpid()) + '.tmp')
    temporary.write_text(value)
    temporary.replace(path)
    for old in directory.glob('*.json'):
        try:
            if not server_alive(json.loads(old.read_text())):
                old.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, TypeError):
            pass


def identity():
    result = from_environment()
    if result:
        return result
    # Standalone maintenance commands may intentionally use the default server.
    raw = subprocess.check_output(['tmux', 'display-message', '-p', '#{socket_path}|#{pid}'],
                                  text=True, timeout=5).strip()
    socket, pid = raw.rsplit('|', 1)
    result = server(socket, pid)
    if not result:
        raise RuntimeError('tmux server identity changed')
    os.environ['TMUX'] = f'{socket},{pid},0'
    remember(result)
    return result


def hook_server(parents):
    """A hook missing TMUX must find its own ancestor server, never guess one."""
    current = from_environment()
    if current and current['pid'] in parents:
        return current
    owner = next((pid for pid, info in parents.items() if Path(info['name']).name.startswith('tmux')), None)
    if owner is None:
        return None
    try:
        cached = json.loads((ROOT / 'servers' / (str(owner) + '.json')).read_text())
        if server_alive(cached) and cached['pid'] == owner:
            return cached
    except (OSError, ValueError, KeyError, TypeError):
        pass
    # First use before plugin bootstrap: discover only sockets owned by the
    # known ancestor server. The normal path uses the validated registry above.
    candidates = list((Path(os.environ.get('TMUX_TMPDIR', '/tmp')) / ('tmux-' + str(os.getuid()))).glob('*'))
    if os.environ.get('TMUX'):
        candidates.insert(0, Path(os.environ['TMUX'].rsplit(',', 2)[0]))
    if sys.platform == 'linux':
        try:
            inodes = {os.readlink(p)[8:-1] for p in Path(f'/proc/{owner}/fd').iterdir()
                      if os.readlink(p).startswith('socket:[')}
            for line in Path('/proc/net/unix').read_text().splitlines():
                fields = line.split(None, 7)
                if len(fields) == 8 and fields[6] in inodes:
                    candidates.append(Path(fields[7]))
        except OSError:
            pass
    else:
        try:
            raw = subprocess.check_output(['/usr/sbin/lsof', '-a', '-p', str(owner), '-U', '-Fn'],
                                          text=True, stderr=subprocess.DEVNULL, timeout=5)
            candidates.extend(Path(line[1:]) for line in raw.splitlines() if line.startswith('n/'))
        except (OSError, subprocess.SubprocessError):
            pass
    for path in dict.fromkeys(candidates):
        candidate = server(path, owner)
        if not candidate:
            continue
        try:
            pid = subprocess.check_output(['tmux', '-N', '-S', str(path), 'display-message', '-p', '#{pid}'],
                                          text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
            if pid == str(owner):
                remember(candidate)
                return candidate
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def marker_alive(path):
    try:
        fields = path.read_text().split()
        owner = process(int(fields[0]))
        return bool(owner and (owner['started'] == int(fields[2]) if len(fields) > 2
                              else owner['started'] <= path.stat().st_mtime_ns // 1000))
    except (OSError, ValueError, IndexError):
        return False


def marker_text(owner, server_pid=None, pane=None):
    info = process(owner)
    return f'{owner} {server_pid or 0} {info["started"]} {pane or "*"}'


def update_marker(path, owner, server_pid=None, pane=None):
    stamp = path.stat().st_mtime_ns
    path.write_text(marker_text(owner, server_pid, pane))
    os.utime(path, ns=(stamp, stamp))


def marker_scope(path):
    try:
        fields = path.read_text().split()
        if not fields and time.time() - path.stat().st_mtime < 2:
            return '*'  # The owner has opened its marker but not written it yet.
        if marker_alive(path):
            return fields[3] if len(fields) >= 4 else '*'
    except OSError:
        pass
    return None


def marker_affects(path, pane):
    return marker_scope(path) in ('*', pane)


def arrivals(before=None, exclude=None, server_pid=None, pane=None):
    directory = private_directory('arrivals')
    for path in directory.iterdir():
        if path == exclude:
            continue
        try:
            fields = path.read_text().split()
            if server_pid and len(fields) > 1 and fields[1] != str(server_pid):
                continue
            if pane and len(fields) > 3 and fields[3] not in ('*', pane):
                continue
            if not marker_alive(path):
                # An open-but-not-yet-written marker is conservatively pending.
                if path.stat().st_size == 0 and time.time() - path.stat().st_mtime < 2 and (before is None or path.stat().st_mtime_ns < before):
                    yield path
                else:
                    path.unlink(missing_ok=True)
            elif before is None or path.stat().st_mtime_ns < before:
                yield path
        except FileNotFoundError:
            pass


if __name__ == '__main__':
    if sys.argv[1:] == ['register']:
        remember(identity())
        private_directory('arrivals')
