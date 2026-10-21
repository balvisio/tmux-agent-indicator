#!/usr/bin/env python3
"""Exercise VPN gating without connecting to a network or restarting a real service."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
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
        probe = patch.object(vpn, "direct_ssh_reachable", return_value=False)
        self.direct = probe.start()
        self.addCleanup(probe.stop)

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

    def internal_network(self):
        self.current.update(location="internal", location_time=self.now, network="office",
                            network_available=True, network_since=self.now - 10)

    def test_internal_network_needs_no_tunnel_and_disarms_restart(self):
        self.check()
        self.now += 200
        self.internal_network()
        result = self.check()
        self.assertTrue(result["ready"])
        self.assertEqual(result["reason"], "Internal network (VPN not required)")
        self.assertIsNone(result["attempt_started"])
        self.assertTrue(self.check(1000)["ready"])
        self.restart.assert_not_called()

    def test_internal_to_external_waits_then_recovers_authenticated_tunnel(self):
        self.internal_network()
        self.assertTrue(self.check()["ready"])
        self.current.update(network="home", location="unknown", event="discovery", intent=None)
        self.assertFalse(self.check(1000)["ready"])
        self.restart.assert_not_called()
        self.current.update(location="external", event="connected-new", intent="authenticated")
        self.assertFalse(self.check()["ready"])
        self.check(121)
        self.restart.assert_called_once()
        self.current["ready"] = True
        self.assertTrue(self.check()["ready"])

    def test_network_change_rejects_old_internal_result_until_fresh_detection(self):
        self.internal_network()
        self.assertTrue(self.check()["ready"])
        self.current["network"] = "another-network"
        self.assertFalse(self.check()["ready"])
        self.assertFalse(self.check(1000)["ready"])
        self.restart.assert_not_called()
        self.current["location_time"] = self.now
        self.assertTrue(self.check()["ready"])

    def test_wake_rejects_old_internal_result_even_without_saved_state(self):
        self.internal_network()
        self.current["network_since"] = self.now + 1
        self.assertFalse(self.check()["ready"])
        self.current["location_time"] = self.now
        self.assertTrue(self.check()["ready"])
        self.current.update(network="after-wake", network_since=self.now + 1)
        self.assertFalse(self.check(100)["ready"])
        self.restart.assert_not_called()

    def test_stale_internal_result_does_not_override_disconnected_tunnel(self):
        self.internal_network()
        self.current.update(ready=True, event="disconnect", intent="inactive", network_since=self.now + 1)
        self.assertFalse(self.check()["ready"])
        self.current["location_time"] = self.now
        self.assertTrue(self.check()["ready"])
        self.restart.assert_not_called()

    def test_internal_detection_missing_unreadable_or_offline_fails_closed(self):
        self.internal_network()
        self.assertTrue(self.check()["ready"])
        self.current.update(network_available=False)
        self.assertFalse(self.check()["ready"])
        self.current.update(network_available=True, location_time=None)
        self.assertFalse(self.check()["ready"])
        self.current.update(location="unknown", location_time=None, log_readable=False,
                            event=None, intent=None)
        self.assertFalse(self.check(1000)["ready"])
        self.restart.assert_not_called()

    def test_log_window_or_rotation_does_not_erase_confirmed_internal_state(self):
        self.internal_network()
        self.current.update(log_id="1:10", log_end=1000, location_offset=100)
        self.assertTrue(self.check()["ready"])
        self.current.update(location=None, location_time=None, location_offset=None, log_end=2000000)
        self.assertTrue(self.check(1000)["ready"])
        self.current.update(log_id="1:11", log_end=100)
        self.assertTrue(self.check(1000)["ready"])
        self.current.update(location="unknown", event="discover", intent=None)
        self.assertFalse(self.check()["ready"])
        self.current.update(location=None)
        self.assertFalse(self.check(1000)["ready"])
        self.restart.assert_not_called()

    def test_new_internal_record_uses_file_order_across_dst(self):
        self.current.update(network="fixture", network_available=True, network_since=self.now,
                            log_id="1:10", log_end=100, location="unknown")
        self.assertFalse(self.check()["ready"])
        self.current.update(location="internal", location_time=self.now - 3600,
                            location_offset=100, log_end=200)
        self.assertTrue(self.check()["ready"])
        self.current.update(network="elsewhere", network_since=self.now + 1)
        self.assertFalse(self.check()["ready"])
        self.current.update(log_id="1:11", location_offset=0, log_end=200)
        self.assertFalse(self.check()["ready"])

    def test_stale_internal_destination_probe_is_scoped_and_cached(self):
        self.internal_network()
        self.current.update(network_since=self.now + 1)
        self.direct.side_effect = lambda args: args[-1] == "reachable"
        self.now += 3
        result = vpn.check("reachable", ssh_args=["-p", "2222", "reachable"])
        self.assertTrue(result["ready"])
        self.assertEqual(result["reason"], "SSH destination reachable (VPN not required)")
        self.assertFalse(vpn.check("unreachable", ssh_args=["unreachable"])["ready"])
        self.assertFalse(vpn.check()["ready"], "a reachable host must not mark the VPN as ready")
        vpn.check("reachable", ssh_args=["-p", "2222", "reachable"])
        self.assertEqual(self.direct.call_count, 2)
        self.now += 3
        vpn.check("unreachable", ssh_args=["unreachable"])
        self.assertEqual(self.direct.call_count, 2, "failed probes should be throttled")
        self.current.update(network="changed")
        self.now += 3
        vpn.check("unreachable", ssh_args=["unreachable"])
        self.assertEqual(self.direct.call_count, 3)
        self.restart.assert_not_called()

    def test_direct_probe_precedes_stalled_vpn_restart(self):
        self.current.update(network="fixture", network_available=True, location=None)
        self.check()
        self.now += 121
        self.direct.return_value = True
        self.assertTrue(vpn.check("host", ssh_args=["host"])["ready"])
        self.restart.assert_not_called()
        self.direct.return_value = False
        self.now += 3
        self.assertFalse(vpn.check("host", ssh_args=["host"])["ready"])
        self.restart.assert_called_once()

    def test_network_change_during_probe_cannot_authorize_old_result(self):
        self.internal_network()
        self.current.update(network_since=self.now + 1)
        self.now += 3
        def probe(args):
            self.now += 3
            self.current.update(network="new-network", location="unknown", event="discovery", intent=None)
            return True
        self.direct.side_effect = probe
        self.assertFalse(vpn.check("host", ssh_args=["host"])["ready"])

    def test_probe_does_not_hold_shared_provider_lock(self):
        self.internal_network()
        self.current.update(network_since=self.now + 1)
        self.now += 3
        def probe(args):
            with (self.root / "state/tmux-agent-indicator/vpn/lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        self.direct.side_effect = probe
        self.assertTrue(vpn.check("host", ssh_args=["host"])["ready"])
        log = (self.root / "state/tmux-agent-indicator/vpn/events.log").read_text()
        self.assertIn("writer=", log)
        self.assertIn("code=", log)
        self.assertNotIn("destination=host", log)

    def test_service_restart_does_not_reuse_internal_result(self):
        self.internal_network()
        self.assertTrue(self.check()["ready"])
        self.current.update(pid="456", location="unknown", location_time=None, event=None, intent=None)
        self.assertFalse(self.check()["ready"])
        self.assertFalse(self.check(1000)["ready"])
        self.restart.assert_not_called()


class NetworkContextTests(unittest.TestCase):
    def test_private_network_data_only_persists_as_digest(self):
        network = "PrimaryInterface : en0\nRouter : 192.0.2.1\nDomainName : example.test"
        clock = "{ sec = 100, usec = 0 }\n{ sec = 200, usec = 0 }\n"

        def command(*args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=network if args[0].endswith('scutil') else clock)

        with patch.object(vpn, "command", side_effect=command):
            first = vpn.network_context()
            self.assertTrue(first["network_available"])
            self.assertEqual(first["network_since"], 200)
            self.assertNotIn("192.0.2.1", json.dumps(first))
            self.assertNotIn("example.test", json.dumps(first))
            self.assertEqual(first, vpn.network_context())
            network += "\nDNS : 192.0.2.2"
            self.assertNotEqual(first["network"], vpn.network_context()["network"])
            clock = "{ sec = 100, usec = 0 }\n{ sec = 300, usec = 0 }\n"
            self.assertEqual(vpn.network_context()["network_since"], 300)
            network = "No such key"
            self.assertFalse(vpn.network_context()["network_available"])
            clock = ""
            self.assertFalse(vpn.network_context()["network_available"])


class LogTests(unittest.TestCase):
    def test_current_process_internal_decision_and_discovery_boundaries(self):
        lines = ["P123-T1 01/02/2026 10:00:00 Debug(1): network type is internal"]
        self.assertEqual(vpn.log_observation(lines, "123")["location"], "internal")
        self.assertIsNotNone(vpn.log_observation(lines, "123")["location_time"])
        self.assertIsNone(vpn.log_observation(lines, "456")["location"])
        # A newer unrelated process cannot erase this process's result.
        lines.append("P456-T1 01/02/2026 10:00:01 Debug(1): network type is external network")
        self.assertEqual(vpn.log_observation(lines, "123")["location"], "internal")
        for message in ("----Network Discover starts----", "Set network discover in progress",
                        "Network changed, break from ProcMonitor",
                        "CPanMSService::Disconnect(): reason is Network change, set network discover event.",
                        "network type is unknown network", "----Disable starts----"):
            with self.subTest(message=message):
                result = vpn.log_observation(lines + ["P123-T1 01/02/2026 10:00:02 Debug(1): " + message], "123")
                self.assertEqual(result["location"], "unknown")
        lines.append("P123-T1 01/02/2026 10:00:03 Debug(1): ----Network Discover starts----")
        lines.append("P123-T1 01/02/2026 10:00:04 Debug(1): Internal network and no tunnel")
        self.assertEqual(vpn.log_observation(lines, "123")["location"], "internal")
        lines.append("P123-T1 01/02/2026 10:00:05 Debug(1): tunnel is down")
        self.assertEqual(vpn.log_observation(lines, "123")["location"], "internal")
        lines.append("P123-T1 01/02/2026 10:00:06 Debug(1): network type is external network")
        self.assertEqual(vpn.log_observation(lines, "123")["location"], "external")

    def test_pending_or_skipped_discovery_is_not_a_network_transition(self):
        lines = ["P123-T1 01/02/2026 10:00:00 Debug(1): Internal network and no tunnel"]
        for message in ("Skip network discovery for remaining internal network",
                        "NetworkDiscoverThread: wait for network discover event.",
                        "NetworkConnectionMonitorThread: Detected route change, but skip network discovery."):
            lines.append("P123-T1 01/02/2026 10:00:01 Debug(1): " + message)
        self.assertEqual(vpn.log_observation(lines, "123")["location"], "internal")

    def test_unrecognized_or_truncated_log_cannot_authorize_internal_network(self):
        for line in ("", "P123-T1 internal host detection enabled", "P123-T1 network type is internal",
                     "P123-T1 99/99/2026 10:00:00 network type is internal"):
            result = vpn.log_observation([line], "123")
            self.assertIsNone(result["location_time"])

    def test_dst_ambiguous_hour_is_conservative(self):
        try:
            with patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
                time.tzset()
                first = vpn.datetime(2026, 11, 1, 1, 30, fold=0).timestamp()
                second = vpn.datetime(2026, 11, 1, 1, 30, fold=1).timestamp()
                self.assertEqual(second - first, 3600)
                self.assertEqual(vpn.event_time("P123-T1 11/01/2026 01:30:00 internal"), first)
        finally:
            time.tzset()

    def test_reader_uses_larger_window_and_records_order_without_private_text(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "provider.log"
            decision = "P123-T1 01/02/2026 10:00:00 Debug(1): Internal network and no tunnel\n"
            log.write_text(decision + "P123-T1 unrelated event\n" * 15000)
            with patch.object(vpn, "Path", return_value=log):
                result = vpn.read_provider_log("123")
                self.assertEqual(result["location"], "internal")
                self.assertGreater(log.stat().st_size, 262144)
                self.assertEqual(result["location_offset"], 0)
                self.assertEqual(result["log_end"], log.stat().st_size)
                self.assertNotIn(decision, json.dumps(result))
                # A partial final record cannot overturn the last complete decision.
                with log.open("a") as stream:
                    stream.write("P123-T1 01/02/2026 11:00:00 Debug(1): network type is external")
                self.assertEqual(vpn.read_provider_log("123")["location"], "internal")
                with log.open("a") as stream:
                    stream.write(" network\n")
                self.assertEqual(vpn.read_provider_log("123")["location"], "external")
                log.write_text("P123-T1 unrelated event\n" * 50000 + decision)
                result = vpn.read_provider_log("123")
                self.assertGreater(result["location_offset"], 1024 * 1024)
                self.assertEqual(result["log_end"], log.stat().st_size)


class ReachabilityTests(unittest.TestCase):
    def test_real_tcp_probe_uses_ssh_alias_port_and_sends_no_data(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(3)
            port = listener.getsockname()[1]
            real_run = subprocess.run
            commands = []
            def run(args, **kwargs):
                commands.append(args)
                if "-G" in args:
                    return subprocess.CompletedProcess(args, 0, stdout=(
                        f"hostname 127.0.0.1\nport {port}\naddressfamily inet\nproxycommand none\n"))
                return real_run(args, **kwargs)
            with patch.object(vpn.subprocess, "run", side_effect=run):
                args = ["-F", "/tmp/example-config", "-p", str(port), "alias", "remote-command"]
                self.assertTrue(vpn.direct_ssh_reachable(args))
                self.assertEqual(commands[0][1:], ["-G", *args])
            connection, _ = listener.accept()
            with connection:
                self.assertEqual(connection.recv(1), b"", "the probe must send no application data")

    def test_proxy_routes_and_timeouts_do_not_bypass_guard(self):
        for option in ("proxyjump jump.example", "proxycommand custom-command", "bindinterface en0"):
            with self.subTest(option=option), patch.object(vpn.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 0, stdout="hostname example.test\nport 22\n" + option)) as run:
                self.assertFalse(vpn.direct_ssh_reachable(["alias"]))
                run.assert_called_once()
        with patch.object(vpn.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 3)):
            self.assertFalse(vpn.direct_ssh_reachable(["alias"]))
        with patch.object(vpn.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, stdout="hostname example.test\nport 22\n"),
                subprocess.TimeoutExpired("tcp probe", 3)]) as run:
            self.assertFalse(vpn.direct_ssh_reachable(["alias"]))
            self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list], [3, 3])


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
            with patch.object(vpn, "network_context", return_value={}), patch.object(vpn, "command", side_effect=command), patch.object(
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
import importlib.util, os, sys, time
from pathlib import Path
sys.dont_write_bytecode = True
s = importlib.util.spec_from_file_location("vpn", {str(ROOT / 'scripts/vpn-guard.py')!r})
v = importlib.util.module_from_spec(s)
s.loader.exec_module(v)
sys.platform = "darwin"
Path({str(child_pid)!r}).write_text(str(os.getpid()))
def observation():
    path = Path({str(ready)!r})
    connected = path.exists()
    internal = connected and path.read_text() == "internal"
    return {{"pid":"123", "ready":connected and not internal, "event":None, "intent":None,
             "location":"internal" if internal else "external", "location_time":time.time(),
             "network":"fixture", "network_available":True, "network_since":0}}
v.observation = observation
v.direct_ssh_reachable = lambda args: False
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
                    ready.write_text("internal")
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
