#!/usr/bin/env python3
"""Native process lookup must cross login without reviving dead processes."""
import ctypes
import errno
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import runtime


class ProcessTests(unittest.TestCase):
    def native(self, error=0, status=2, name=b'codex', parent=100):
        def read(pid, flavor, arg, buffer, size):
            ctypes.set_errno(error)
            if error:
                return 0
            info = ctypes.cast(buffer, ctypes.POINTER(runtime.BSDInfo)).contents
            info.pid, info.ppid, info.status = pid, parent, status
            info.seconds, info.micros, info.name = 1234567890, 123456, name
            return size
        return SimpleNamespace(proc_pidinfo=read)

    def test_native_success_keeps_precise_birth_and_never_starts_ps(self):
        with patch.object(runtime, '_libproc', self.native()), \
                patch.object(runtime.subprocess, 'check_output') as ps:
            found = runtime.process(300)
        self.assertEqual(found, dict(pid=300, ppid=100, started=1234567890123456, name='codex'))
        ps.assert_not_called()

    def test_denied_mac_process_uses_one_ps_read(self):
        for error in (errno.EPERM, errno.EACCES):
            with self.subTest(error=error), \
                    patch.object(runtime, '_libproc', self.native(error=error)), \
                    patch.object(runtime.subprocess, 'check_output',
                                 return_value='100 Ss Tue Nov 17 09:00:00 2026 /usr/bin/login\n') as ps:
                found = runtime.process(200)
            self.assertEqual((found['pid'], found['ppid'], found['name']), (200, 100, '/usr/bin/login'))
            self.assertIsInstance(found['started'], int)
            self.assertEqual(ps.call_count, 1)
            self.assertEqual(ps.call_args.kwargs['env']['LC_ALL'], 'C')

    def test_missing_or_zombie_mac_process_does_not_start_ps(self):
        for native in (self.native(error=errno.ESRCH), self.native(status=5)):
            with self.subTest(native=native), patch.object(runtime, '_libproc', native), \
                    patch.object(runtime.subprocess, 'check_output') as ps:
                self.assertIsNone(runtime.process(300))
                ps.assert_not_called()

    def test_root_login_does_not_break_ancestry_and_tmux_ends_the_walk(self):
        seen = []
        def read(pid, *args):
            seen.append(pid)
            readers = {300: self.native(parent=200), 200: self.native(error=errno.EPERM),
                       100: self.native(name=b'tmux: server', parent=1)}
            return readers[pid].proc_pidinfo(pid, *args)
        with patch.object(runtime, '_libproc', SimpleNamespace(proc_pidinfo=read)), \
                patch.object(runtime.subprocess, 'check_output',
                             return_value='100 Ss Tue Nov 17 09:00:00 2026 /usr/bin/login\n') as ps:
            parents = runtime.ancestors(300)
        self.assertEqual(list(parents), [300, 200, 100])
        self.assertEqual(seen, [300, 200, 100])
        self.assertEqual(ps.call_count, 1)

    def test_ancestry_does_not_probe_init_without_a_tmux_parent(self):
        with patch.object(runtime, 'process', return_value=dict(pid=200, ppid=1, name='bash')) as read:
            self.assertEqual(list(runtime.ancestors(200)), [200])
        read.assert_called_once_with(200)

    def test_denied_procfs_read_falls_back_but_missing_process_does_not(self):
        for error in (PermissionError(errno.EACCES, 'denied'), FileNotFoundError()):
            with self.subTest(error=error), patch.object(runtime, '_libproc', None), \
                    patch.object(runtime.sys, 'platform', 'linux'), \
                    patch.object(runtime.Path, 'read_text', side_effect=error), \
                    patch.object(runtime.subprocess, 'check_output',
                                 return_value='100 Ss Tue Nov 17 09:00:00 2026 /bin/login\n') as ps:
                found = runtime.process(200)
            if isinstance(error, PermissionError):
                self.assertEqual(found['ppid'], 100)
                ps.assert_called_once()
            else:
                self.assertIsNone(found)
                ps.assert_not_called()

    def test_ps_zombies_failure_and_malformed_records_stay_unknown(self):
        for value in ('100 Z Tue Nov 17 09:00:00 2026 login\n', '', 'malformed',
                      subprocess.CalledProcessError(1, 'ps'), subprocess.TimeoutExpired('ps', 5)):
            kwargs = {'side_effect': value} if isinstance(value, Exception) else {'return_value': value}
            with self.subTest(value=value), patch.object(runtime, '_libproc', self.native(error=errno.EPERM)), \
                    patch.object(runtime.subprocess, 'check_output', **kwargs):
                self.assertIsNone(runtime.process(200))


if __name__ == '__main__':
    unittest.main()
