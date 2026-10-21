#!/usr/bin/env python3
"""Allow SSH on an internal network or ready VPN, and recover stalled tunnel setup."""
from datetime import datetime
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

STATE_SCHEMA = 3
LOG_BYTES = 1024 * 1024
CODE_ID = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]


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


def network_context():
    # Persist only a digest: network addresses and DNS configuration remain private.
    network = command("/usr/sbin/scutil", input=(
        "show State:/Network/Global/IPv4\n"
        "show State:/Network/Global/IPv6\n"
        "show State:/Network/Global/DNS\n"))
    clock = command("/usr/sbin/sysctl", "-n", "kern.boottime", "kern.waketime")
    times = [int(value) for value in re.findall(r"\bsec = (\d+)", clock.stdout)]
    available = (network.returncode == 0 and clock.returncode == 0 and len(times) == 2
                 and "PrimaryInterface :" in network.stdout)
    return {"network": hashlib.sha256((network.stdout + clock.stdout).encode()).hexdigest(),
            "network_available": available, "network_since": max(times, default=0)}


def event_time(line):
    stamp = re.search(r"\b\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}\b", line)
    if stamp:
        try:
            local = datetime.strptime(stamp[0], "%m/%d/%Y %H:%M:%S")
            # During fall-back the log cannot identify which occurrence of the hour
            # it means. Use the earlier instant; subsequent records use file order.
            return min(local.replace(fold=fold).timestamp() for fold in (0, 1))
        except ValueError:
            pass
    return None


def log_observation(lines, pid):
    event, intent, location, location_event, location_index = None, None, None, None, None
    for index, line in enumerate(lines):
        if not line.startswith("P" + pid + "-"):
            continue
        inactive = re.search(
            r"----(?:Disable|Logout|Disconnect) starts----|msgtype = (?:disable|disconnect|logout)\b", line, re.I)
        discovery = re.search(
            r"----Network Discover starts----|\bSet network discover in progress\b|"
            r"\bNetwork changed\b|\breason is Network change\b", line, re.I)
        mode = re.search(r"\bnetwork type is (internal|external|unknown)\b", line, re.I)
        if inactive or discovery:
            location, location_event = "unknown", line
            event, intent = line, "inactive" if inactive else None
        elif re.search(r"\bInternal network and no tunnel\b", line, re.I):
            location, location_event = "internal", line
        elif mode:
            location, location_event = mode[1].lower(), line
        if location_event == line:
            location_index = index
        if inactive or re.search(r"\btunnel is down\b", line, re.I):
            event, intent = line, "inactive"
        elif re.search(r"\btunnel to .* connected\b", line, re.I):
            event, intent = line, "authenticated"
    return {"event": hashlib.sha256(event.encode()).hexdigest() if event else None,
            "intent": intent, "location": location,
            "location_event": hashlib.sha256(location_event.encode()).hexdigest() if location_event else None,
            "location_index": location_index,
            "location_time": event_time(location_event) if location_event else None}


def read_provider_log(pid):
    path = Path("/Library/Logs/PaloAltoNetworks/GlobalProtect/PanGPS.log")
    try:
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            start = max(0, stat.st_size - LOG_BYTES)
            stream.seek(start)
            if start:
                stream.readline()  # Discard a partial first record.
            start = stream.tell()
            data = stream.read(LOG_BYTES)
        # A writer may still be finishing the last record. Revisit it next time.
        end = data.rfind(b"\n") + 1
        records = data[:end].splitlines(keepends=True)
        result = log_observation([line.decode(errors="replace") for line in records], pid)
        index = result.pop("location_index")
        result.update(log_readable=True, log_id=f"{stat.st_dev}:{stat.st_ino}", log_end=start + end,
                      location_offset=start + sum(map(len, records[:index])) if index is not None else None)
        return result
    except OSError:
        return {"event": None, "intent": None, "location": None, "log_readable": False}


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

    return {"pid": pid, "ready": ready, **read_provider_log(pid), **network_context()}


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


def log_event(root, reason, **details):
    # Only caller-selected diagnostics and digests belong here, never provider/config text.
    details.update(writer=os.getpid(), code=CODE_ID)
    fields = " ".join(f"{key}={value}" for key, value in details.items())
    with (root / "events.log").open("a") as stream:
        stream.write(time.strftime("%Y-%m-%d %H:%M:%S ") + reason + " [" + fields + "]\n")


def restart_service():
    # The job is in the user's launchd domain. Never prompt for credentials from a timer.
    result = command("/bin/launchctl", "kickstart", "-k", service_target())
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "launchctl refused the restart")


def provider_check(config, root, recover=True):
    key = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    with (root / "lock").open("a") as lock:
        # Concurrent hosts share the completed check instead of reporting a busy lock as an outage.
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(root / "state.json")
        now = time.time()
        if (state.get("schema") == STATE_SCHEMA and state.get("code") == CODE_ID and state.get("config") == key
                and (not recover or state.get("recovery_checked"))
                and 0 <= now - state.get("checked", 0) < 2):
            return state
        old_reason, old_code = state.get("reason"), state.get("code")
        current = observation()
        same_context = (state.get("network") == current.get("network")
                        and state.get("pid") == current["pid"])
        # A changed network invalidates old internal detection even if GlobalProtect has
        # not yet logged its next discovery. Wake/boot timestamps also cover polling gaps.
        internal_after = max(state.get("internal_after", 0), current.get("network_since", 0))
        if "network" in state and current.get("network") != state["network"]:
            internal_after = max(internal_after, state.get("checked", 0))
        internal = bool(same_context and state.get("internal_confirmed"))
        location = current.get("location")
        if location is not None:
            fresh_time = (current.get("location_time") is not None
                          and internal_after <= current["location_time"] <= now + 1)
            # Newly appended decisions on an unchanged network do not depend on local
            # wall-clock interpretation (DST, clock corrections). Never compare offsets
            # across rotations or truncation.
            fresh_record = (same_context and current.get("log_id") is not None
                            and current["log_id"] == state.get("log_id")
                            and current.get("location_offset") is not None
                            and state.get("log_end") is not None
                            and current["location_offset"] >= state["log_end"])
            internal = location == "internal" and (internal or fresh_time or fresh_record)
        internal = bool(internal and current.get("network_available", False)
                        and current.get("log_readable", True))
        state.update(network=current.get("network"), internal_after=internal_after,
                     internal_confirmed=internal, log_id=current.get("log_id"), log_end=current.get("log_end"))
        if current["pid"] != state.get("pid"):
            state.update(pid=current["pid"], event=None, attempt_started=None, intent=None)
        if current["event"] and current["event"] != state.get("event"):
            state.update(event=current["event"], intent=current["intent"],
                         attempt_started=now if current["intent"] == "authenticated" else None)
        if not current.get("log_readable", True):
            state.update(event=None, intent=None, attempt_started=None)
        # Internal operation needs no tunnel; an old authenticated event must not arm
        # a restart there, including while waiting for fresh internal detection.
        if internal or location == "internal":
            state["attempt_started"] = None
            if state.get("intent") == "authenticated":
                state["intent"] = None
        state.update(schema=STATE_SCHEMA, code=CODE_ID, writer=os.getpid(), config=key, checked=now, recovery_checked=True,
                     network_available=current.get("network_available", False),
                     ready=internal or (current["ready"] and state.get("intent") != "inactive"))
        if state["ready"]:
            reason = "Internal network (VPN not required)" if internal else "GlobalProtect connected"
            state.update(reason=reason, attempt_started=None,
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
                elif not recover:
                    state["recovery_checked"] = False
                else:
                    state.update(reason="Restarting stalled GlobalProtect", restart_attempted=True,
                                 last_restart=now, ready=False)
                    write_state(root, state)
                    try:
                        restart_service()
                    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                        state["reason"] = "GlobalProtect restart failed: " + str(error)[:400]
                        state["restart_error"] = state["reason"]
        if state["reason"] != old_reason or not same_context or old_code != CODE_ID:
            log_event(root, state["reason"], network=(state.get("network") or "unknown")[:12],
                      location=location or "unreported", same_context=same_context,
                      internal=internal, log_readable=current.get("log_readable", True))
        write_state(root, state)
        return state


TCP_PROBE = """
import contextlib, errno, json, selectors, socket, sys, time
host, port, family, source = json.load(sys.stdin)
family = {'inet': socket.AF_INET, 'inet6': socket.AF_INET6}.get(family, socket.AF_UNSPEC)
addresses = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
end = time.monotonic() + 2
with contextlib.ExitStack() as sockets, selectors.DefaultSelector() as selector:
    for af, socktype, proto, _, address in addresses:
        try:
            sock = sockets.enter_context(socket.socket(af, socktype, proto))
            sock.setblocking(False)
            if source:
                sock.bind((source, 0))
            error = sock.connect_ex(address)
            if error == 0:
                sys.exit(0)
            if error in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                selector.register(sock, selectors.EVENT_WRITE)
        except OSError:
            pass
    while selector.get_map() and time.monotonic() < end:
        for key, _ in selector.select(max(0, end - time.monotonic())):
            if key.fileobj.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                sys.exit(0)
            selector.unregister(key.fileobj)
sys.exit(1)
"""


def direct_ssh_reachable(args):
    """Probe the configured direct SSH endpoint without authenticating or sending data."""
    executable = os.environ.get("TMUX_AGENT_REAL_SSH", "/usr/bin/ssh")
    try:
        result = subprocess.run([executable, "-G", *args], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=3)
        if result.returncode:
            return False
        options = dict(line.split(None, 1) for line in result.stdout.splitlines() if " " in line)
        # A TCP connection to the destination cannot validate a configured proxy path.
        # Leave those connections to the provider guard rather than bypassing their route.
        if any(options.get(key, "none") != "none" for key in ("proxycommand", "proxyjump", "bindinterface")):
            return False
        host, port = options["hostname"], int(options["port"])
        if not host or not 0 < port < 65536:
            return False
        source = options.get("bindaddress", "none")
        result = subprocess.run([sys.executable, "-c", TCP_PROBE],
                                input=json.dumps([host, port, options.get("addressfamily"),
                                                  None if source == "none" else source]),
                                text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        return result.returncode == 0
    except (OSError, KeyError, ValueError, subprocess.SubprocessError):
        return False


def destination_reachable(args, network, root):
    # Per-destination checks run outside the shared provider lock. One unreachable
    # server must not delay other hosts or authorize their SSH attempts.
    key = hashlib.sha256(json.dumps(args).encode()).hexdigest()
    directory = root / "destinations"
    directory.mkdir(exist_ok=True, mode=0o700)
    path = directory / (key + ".json")
    with (directory / (key + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(path)
        now = time.time()
        if state.get("network") == network and 0 <= now - state.get("checked", 0) < (2 if state.get("ready") else 15):
            return state["ready"]
        ready = direct_ssh_reachable(args)
        if ready != state.get("ready") or state.get("network") != network:
            log_event(root, "Direct SSH endpoint reachable" if ready else "Direct SSH endpoint unavailable",
                      destination=key[:12], network=(network or "unknown")[:12])
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"ready": ready, "checked": time.time(), "network": network}) + "\n")
        temporary.replace(path)
        return ready


def check(host=None, ssh_args=None):
    config = configuration()
    if not config or (host is not None and not any(
            fnmatch.fnmatchcase(host.rsplit("@", 1)[-1], pattern) for pattern in config["hosts"])):
        return {"ready": True, "reason": "VPN guard disabled"}
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))/"tmux-agent-indicator/vpn"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = provider_check(config, root, recover=ssh_args is None)
    if state["ready"] or ssh_args is None:
        return state
    if state.get("network_available") and destination_reachable(ssh_args, state.get("network"), root):
        # This result applies only to this invocation; never publish it as VPN readiness.
        latest = provider_check(config, root, recover=False)
        if latest["ready"]:
            return latest
        if latest.get("network_available") and latest.get("network") == state.get("network"):
            return {**latest, "ready": True, "reason": "SSH destination reachable (VPN not required)"}
    return provider_check(config, root)


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
            state = check(host, ssh_args=None if status else args)
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
