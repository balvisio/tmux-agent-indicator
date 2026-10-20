#!/usr/bin/env python3
"""Gate SSH attempts on GlobalProtect readiness and recover stalled tunnel setup."""
import fcntl
import fnmatch
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def configuration():
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    path = root / "tmux-agent-indicator/vpn.json"
    if sys.platform != "darwin" or not path.exists():
        return None
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise ValueError("vpn.json must contain a JSON object")
    if config.get("provider", "off") == "off":
        return None
    if config.get("provider") != "globalprotect":
        raise ValueError("vpn.json provider must be globalprotect or off")
    config.setdefault("hosts", ["*"])
    config.setdefault("auto_restart", False)
    config.setdefault("connect_timeout", 120)
    config.setdefault("restart_cooldown", 900)
    if not isinstance(config["hosts"], list) or not all(isinstance(h, str) for h in config["hosts"]):
        raise ValueError("vpn.json hosts must be a list of host patterns")
    if type(config["auto_restart"]) is not bool:
        raise ValueError("vpn.json auto_restart must be true or false")
    for key, minimum in (("connect_timeout", 30), ("restart_cooldown", 300)):
        if type(config[key]) is not int or config[key] < minimum:
            raise ValueError(f"vpn.json {key} must be an integer of at least {minimum} seconds")
    return config


def command(*args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, timeout=10, **kwargs)


def service_target():
    return f"gui/{os.getuid()}/com.paloaltonetworks.gp.pangps"


def observation():
    service = command("/bin/launchctl", "print", service_target())
    pid = re.search(r"^\s*pid = (\d+)$", service.stdout, re.M)
    if not pid:
        return {"pid": None, "ready": False, "event": None, "intent": None}
    pid = pid[1]
    network = command("/usr/sbin/scutil", input=(
        "show State:/Network/Service/gpd.pan/IPv4\n"
        "show State:/Network/Service/gpd.pan/IPv6\n"))
    ready = False
    interfaces = set(re.findall(r"InterfaceName : (\w+)", network.stdout))
    expected = set()
    for block in re.findall(r"Addresses : <array> \{([^}]+)\}", network.stdout):
        for line in block.splitlines():
            if " : " in line:
                expected.add(line.split(" : ", 1)[1].strip())
    for interface in interfaces:
        actual = command("/sbin/ifconfig", interface).stdout
        for address in re.findall(r"\binet6? ([0-9a-fA-F:.%]+)", actual):
            try:
                ip = ipaddress.ip_address(address.split("%")[0])
            except ValueError:
                continue
            if str(ip) in expected and not (ip.is_link_local or ip.is_loopback or ip.is_unspecified):
                ready = True

    # Only an authenticated tunnel reaching interface setup arms automatic recovery.
    # A user-requested disconnect cancels it, even if the service hangs during disconnect.
    event, intent, log_readable = None, None, True
    path = Path("/Library/Logs/PaloAltoNetworks/GlobalProtect/PanGPS.log")
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 262144))
            lines = stream.read().decode(errors="replace").splitlines()
    except OSError:
        # Interface readiness still works without logs; automatic recovery stays unarmed.
        lines, log_readable = [], False
    for line in reversed(lines):
        if not line.startswith("P" + pid + "-"):
            continue
        if re.search(r"----(?:Disable|Logout|Disconnect) starts----|msgtype = (?:disable|disconnect|logout)\b|tunnel is down", line, re.I):
            event, intent = line, "inactive"
            break
        if re.search(r"\btunnel to .* connected\b", line, re.I):
            event, intent = line, "authenticated"
            break
    return {"pid": pid, "ready": ready,
            "event": hashlib.sha256(event.encode()).hexdigest() if event else None,
            "intent": intent, "log_readable": log_readable}


def read_state(path):
    try:
        state = json.loads(path.read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(root, state):
    temporary = root / "state.tmp"
    temporary.write_text(json.dumps(state) + "\n")
    temporary.replace(root / "state.json")


def restart_service():
    # The job is in the user's launchd domain. Never prompt for credentials from a timer.
    result = command("/bin/launchctl", "kickstart", "-k", service_target())
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "launchctl refused the restart")


def check(host=None):
    config = configuration()
    if not config or (host is not None and not any(
            fnmatch.fnmatchcase(host.rsplit("@", 1)[-1], pattern) for pattern in config["hosts"])):
        return {"ready": True, "reason": "VPN guard disabled"}
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))/"tmux-agent-indicator/vpn"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    with (root / "lock").open("a") as lock:
        # Concurrent hosts share the completed check instead of reporting a busy lock as an outage.
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(root / "state.json")
        now = time.time()
        if state.get("config") == key and 0 <= now - state.get("checked", 0) < 2:
            return state
        old_reason = state.get("reason")
        current = observation()
        if current["pid"] != state.get("pid"):
            state.update(pid=current["pid"], event=None, attempt_started=None, intent=None)
        if current["event"] and current["event"] != state.get("event"):
            state.update(event=current["event"], intent=current["intent"],
                         attempt_started=now if current["intent"] == "authenticated" else None)
        if not current.get("log_readable", True):
            state.update(event=None, intent=None, attempt_started=None)
        state.update(config=key, checked=now, ready=current["ready"] and state.get("intent") != "inactive")
        if state["ready"]:
            state.update(reason="GlobalProtect connected", attempt_started=None,
                         restart_attempted=False, restart_error=None)
        else:
            state["reason"] = "Waiting for GlobalProtect"
            if state.get("intent") == "authenticated" and state.get("attempt_started") is None:
                state["attempt_started"] = now
            started = state.get("attempt_started")
            stalled = started is not None and now - started >= config["connect_timeout"]
            if stalled and config["auto_restart"]:
                if state.get("restart_attempted"):
                    state["reason"] = state.get("restart_error") or "GlobalProtect recovery did not finish; reconnect manually"
                elif now - state.get("last_restart", 0) < config["restart_cooldown"]:
                    state["reason"] = "Waiting for GlobalProtect (restart cooldown)"
                else:
                    state.update(reason="Restarting stalled GlobalProtect", restart_attempted=True,
                                 last_restart=now, ready=False)
                    write_state(root, state)
                    try:
                        restart_service()
                    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                        state["reason"] = "GlobalProtect restart failed: " + str(error)[:400]
                        state["restart_error"] = state["reason"]
        if state["reason"] != old_reason:
            with (root / "events.log").open("a") as stream:
                stream.write(time.strftime("%Y-%m-%d %H:%M:%S ") + state["reason"] + "\n")
        write_state(root, state)
        return state


def main():
    original_umask = os.umask(0o077)
    args = sys.argv[1:]
    probe = args[:1] == ["--probe"]
    status = args[:1] == ["--check"]
    if probe:
        if len(args) < 3:
            raise ValueError("usage: vpn-guard.py --probe HOST [ssh arguments...]")
        host, args = args[1], args[2:]
    else:
        host = os.environ.get("TMUX_AGENT_VPN_HOST")
    if not status and (not args or not host):
        raise ValueError("SSH destination is required")
    previous = None
    while True:
        try:
            state = check(host)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            state = {"ready": False, "reason": "VPN check failed: " + str(error)[:400]}
        if status:
            print(json.dumps(state))
            return 0 if state["ready"] else 75
        if state["ready"]:
            executable = os.environ.get("TMUX_AGENT_REAL_SSH", "/usr/bin/ssh")
            os.umask(original_umask)
            os.execvp(executable, [executable, *args])
        if state["reason"] != previous:
            print(state["reason"], file=sys.stderr, flush=True)
            previous = state["reason"]
        if probe:
            return 75
        time.sleep(5)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except ValueError as error:
        print(error, file=sys.stderr)
        sys.exit(2)
