#!/usr/bin/env python3
"""Exercise VPN gating without connecting to a network or restarting a real service."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("vpn", ROOT / "scripts/vpn-guard.py")
vpn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vpn)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env = patch.dict(os.environ, {
            "HOME": str(self.root),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.platform = patch.object(vpn.sys, "platform", "darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.config = self.root / "config/tmux-agent-indicator/vpn.json"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({"provider": "globalprotect", "auto_restart": True}))
        self.now = 10000
        self.current = {"pid": "123", "ready": False, "event": "connected-1", "intent": "authenticated"}
        for name, replacement in (
                ("observation", lambda: dict(self.current)),
                ("restart_service", None)):
            mock = patch.object(vpn, name, side_effect=replacement)
            value = mock.start()
            self.addCleanup(mock.stop)
            if name == "restart_service":
                self.restart = value
        clock = patch.object(vpn.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def check(self, elapsed=3, host="internal"):
        self.now += elapsed
        return vpn.check(host)

    def test_timeout_one_restart_and_recovery(self):
        self.assertFalse(self.check()["ready"])
        self.check(119)
        self.restart.assert_not_called()
        self.assertIn("Restarting", self.check(3)["reason"])
        self.restart.assert_called_once()
        self.current.update(pid="456", event="connected-2")
        self.check()
        self.assertIn("reconnect manually", self.check(130)["reason"])
        self.restart.assert_called_once()
        self.current["ready"] = True
        self.assertTrue(self.check()["ready"])

    def test_login_and_manual_disconnect_do_not_restart(self):
        self.current.update(event=None, intent=None)
        self.check()
        self.check(1000)
        self.current.update(event="connected", intent="authenticated")
        self.check()
        self.current.update(event="disable", intent="inactive")
        self.check()
        self.assertFalse(self.check(1000)["ready"])
        self.restart.assert_not_called()
        # Stale network configuration must not override a recorded disconnect.
        self.current["ready"] = True
        self.assertFalse(self.check()["ready"])

    def test_interface_loss_after_connected_starts_timeout(self):
        self.current["ready"] = True
        self.assertTrue(self.check()["ready"])
        self.current["ready"] = False
        self.check()
        self.check(121)
        self.restart.assert_called_once()

    def test_unreadable_log_disarms_recovery(self):
        self.check()
        self.current.update(event=None, intent=None, log_readable=False)
        self.check(121)
        self.restart.assert_not_called()
        self.current["ready"] = True
        self.assertTrue(self.check()["ready"])

    def test_cooldown_survives_success(self):
        self.check()
        self.check(121)
        self.current["ready"] = True
        self.check()
        self.current.update(ready=False, event="connected-again")
        self.check()
        self.assertIn("cooldown", self.check(121)["reason"])
        self.restart.assert_called_once()
        self.check(900)
        self.assertEqual(self.restart.call_count, 2)

    def test_restart_failure_is_visible_without_retries(self):
        self.restart.side_effect = RuntimeError("Operation not permitted")
        self.check()
        reason = self.check(121)["reason"]
        self.assertIn("Operation not permitted", reason)
        self.assertEqual(reason, self.check(1000)["reason"])
        self.restart.assert_called_once()
        self.assertIn(reason, (self.root / "state/tmux-agent-indicator/vpn/events.log").read_text())

    def test_busy_guard_and_shared_state_prevent_duplicate_restart(self):
        self.check()
        lock = self.root / "state/tmux-agent-indicator/vpn/lock"
        self.now += 121
        with ThreadPoolExecutor(max_workers=2) as pool:
            with lock.open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                results = [pool.submit(vpn.check, host) for host in ("first", "second")]
                time.sleep(0.05)
                self.assertTrue(all(not future.done() for future in results))
                self.restart.assert_not_called()
            reasons = [future.result(timeout=2)["reason"] for future in results]
        self.assertEqual(reasons[0], reasons[1])
        self.restart.assert_called_once()
        self.current["ready"] = True
        self.now += 3
        with ThreadPoolExecutor(max_workers=3) as pool:
            self.assertTrue(all(state["ready"] for state in pool.map(vpn.check, ("first", "second", "third"))))

    def test_opt_in_platform_and_host_selection(self):
        self.config.unlink()
        self.assertTrue(self.check()["ready"])
        self.config.write_text(json.dumps({"provider": "off"}))
        self.assertTrue(self.check()["ready"])
        self.config.write_text(json.dumps({"provider": "globalprotect", "hosts": ["internal*", "10.*"]}))
        self.assertTrue(self.check(host="public.example")["ready"])
        self.assertFalse(self.check(host="user@internal")["ready"])
        with patch.object(vpn.sys, "platform", "linux"):
            self.assertTrue(self.check()["ready"])

    def test_invalid_config_and_restart_disabled(self):
        self.config.write_text("[]")
        with self.assertRaisesRegex(ValueError, "JSON object"):
            self.check()
        self.config.write_text(json.dumps({"provider": "globalprotect"}))
        self.check()
        self.check(1000)
        self.restart.assert_not_called()


class ObservationTests(unittest.TestCase):
    def test_matching_globalprotect_interface_and_intent(self):
        network = """<dictionary> {
  Addresses : <array> {
    0 : 10.2.3.4
  }
  InterfaceName : utun5
}"""
        interface = "inet 10.2.3.4 --> 10.2.3.4 netmask 0xffffffff"

        def command(*args, **kwargs):
            if args[:2] == ("/bin/launchctl", "print"):
                output = "\tpid = 123\n"
            elif args[0] == "/usr/sbin/scutil":
                self.assertIn("gpd.pan/IPv4", kwargs["input"])
                output = network
            else:
                self.assertEqual(args, ("/sbin/ifconfig", "utun5"))
                output = interface
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "PanGPS.log"
            log.write_text("P123-T1 Debug: tunnel to gateway connected\n"
                           "P999-T1 Debug: ----Disable starts----\n")
            real_path = Path
            with patch.object(vpn, "command", side_effect=command), patch.object(
                    vpn, "Path", side_effect=lambda unused: real_path(log)):
                self.assertTrue(vpn.observation()["ready"])
                self.assertEqual(vpn.observation()["intent"], "authenticated")
                interface = "inet6 fe80::123%utun5"
                self.assertFalse(vpn.observation()["ready"])
                log.write_text(log.read_text() + "P123-T1 Debug: ----Disable starts----\n")
                self.assertEqual(vpn.observation()["intent"], "inactive")
                log.unlink()
                interface = "inet 10.2.3.4"
                self.assertTrue(vpn.observation()["ready"])
                self.assertIsNone(vpn.observation()["intent"])
                self.assertFalse(vpn.observation()["log_readable"])


class SshTests(unittest.TestCase):
    def test_probe_wait_and_exec_preserve_arguments_and_umask(self):
        original_mask = os.umask(0o022)
        self.addCleanup(os.umask, original_mask)
        with patch.object(vpn, "check", return_value={"ready": False, "reason": "Waiting"}), \
                patch.object(vpn.sys, "argv", ["vpn", "--probe", "host", "-o", "BatchMode=yes", "host"]), \
                patch.object(vpn.os, "execvp") as execute, patch.object(vpn.time, "sleep") as sleep:
            self.assertEqual(vpn.main(), 75)
            execute.assert_not_called()
            sleep.assert_not_called()
        os.umask(0o022)
        with patch.object(vpn, "check", return_value={"ready": True}), \
                patch.object(vpn.sys, "argv", ["vpn", "--probe", "host", "-o", "BatchMode=yes", "host"]), \
                patch.dict(os.environ, {"TMUX_AGENT_REAL_SSH": "/fake/ssh"}), \
                patch.object(vpn.os, "execvp", side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit):
                vpn.main()
            execute.assert_called_once_with("/fake/ssh", ["/fake/ssh", "-o", "BatchMode=yes", "host"])
            self.assertEqual(os.umask(0o022), 0o022)

    @unittest.skipUnless(shutil.which("autossh"), "autossh is required for retry integration")
    def test_real_autossh_waits_on_every_retry_and_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "tmux-agent-indicator"
            config.mkdir()
            (config / "vpn.json").write_text('{"provider":"globalprotect"}')
            ready, calls, action, child_pid = (root / name for name in ("ready", "calls", "action", "pid"))
            driver = root / "guard"
            driver.write_text(f"""#!{sys.executable}
import importlib.util, os, sys
from pathlib import Path
sys.dont_write_bytecode = True
s = importlib.util.spec_from_file_location("vpn", {str(ROOT / 'scripts/vpn-guard.py')!r})
v = importlib.util.module_from_spec(s)
s.loader.exec_module(v)
sys.platform = "darwin"
Path({str(child_pid)!r}).write_text(str(os.getpid()))
v.observation = lambda: {{"pid":"123", "ready":Path({str(ready)!r}).exists(), "event":None, "intent":None}}
sys.exit(v.main())
""")
            ssh = root / "ssh"
            ssh.write_text(f"""#!{sys.executable}
from pathlib import Path
import sys, time
with Path({str(calls)!r}).open("a") as f:
    f.write("attempt\\n")
action = Path({str(action)!r})
while not action.exists():
    time.sleep(0.05)
result = action.read_text()
action.unlink()
sys.exit(0 if result == "exit" else 1)
""")
            driver.chmod(0o755)
            ssh.chmod(0o755)
            env = dict(os.environ, XDG_CONFIG_HOME=directory, XDG_STATE_HOME=str(root / "state"),
                       AUTOSSH_PATH=str(driver), TMUX_AGENT_REAL_SSH=str(ssh), TMUX_AGENT_VPN_HOST="host",
                       AUTOSSH_GATETIME="0", AUTOSSH_POLL="1", AUTOSSH_FIRST_POLL="1")
            env.pop("AUTOSSH_PIDFILE", None)
            env.pop("AUTOSSH_LOGFILE", None)

            def wait_for(condition, timeout=12):
                end = time.monotonic() + timeout
                while not condition():
                    if time.monotonic() >= end:
                        self.fail("SSH retry fixture timed out")
                    time.sleep(0.05)

            with (root / "stderr").open("w") as stderr:
                process = subprocess.Popen(["autossh", "-M", "0", "host"], env=env,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr)
                try:
                    wait_for(child_pid.exists)
                    self.assertFalse(calls.exists(), "initial SSH must wait for VPN")
                    ready.touch()
                    wait_for(calls.exists)
                    ready.unlink()
                    # Let the shared readiness cache expire before the dropped SSH retries.
                    time.sleep(2.1)
                    first_child = child_pid.read_text()
                    action.write_text("retry")
                    wait_for(lambda: child_pid.read_text() != first_child)
                    time.sleep(2.1)
                    self.assertEqual(calls.read_text().count("attempt"), 1, "retry bypassed VPN guard")
                    ready.touch()
                    wait_for(lambda: calls.read_text().count("attempt") == 2)
                    action.write_text("exit")
                    self.assertEqual(process.wait(timeout=5), 0, "intentional exit must end autossh")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)
                # Termination while waiting must also stop the guard, without leaving a retry worker.
                ready.unlink()
                time.sleep(2.1)
                first_child = child_pid.read_text()
                process = subprocess.Popen(["autossh", "-M", "0", "host"], env=env,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr)
                try:
                    wait_for(lambda: child_pid.read_text() != first_child)
                    waiting_pid = int(child_pid.read_text())
                    process.terminate()
                    process.wait(timeout=5)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(waiting_pid, 0)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
