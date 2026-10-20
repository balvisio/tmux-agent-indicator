#!/usr/bin/env python3
"""Send real terminal mouse events through nested, isolated tmux servers."""
import fcntl
import os
from pathlib import Path
import pty
import select
import shlex
import struct
import subprocess
import tempfile
import termios
import threading
import time

ROOT = Path(__file__).resolve().parent.parent


def wait_for(check):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting for mouse selection")


def main():
    with tempfile.TemporaryDirectory(prefix="test-ssh-mouse-") as tmp:
        work = Path(tmp)
        home = work / "home"
        plugins = home / ".tmux/plugins"
        plugins.mkdir(parents=True)
        (plugins / "tmux-agent-indicator").symlink_to(ROOT)
        env = {k: v for k, v in os.environ.items()
               if k not in ("TMUX", "TMUX_PANE", "BASH_ENV", "ENV")}
        env.update(HOME=str(home), TMUX_TMPDIR=tmp, ZDOTDIR=str(home),
                   XDG_STATE_HOME=str(home / ".local/state"), TERM="xterm-256color",
                   SHELL="/bin/bash")
        (home / ".tmux/agent-indicator.local.conf").write_text(
            "set -g @agent-indicator-recovery off\n")
        outer, inner = work / "outer", work / "inner"
        helper = str(ROOT / "scripts/ssh-passthrough.sh")

        def t(*args, socket=outer, check=True):
            result = subprocess.run(["tmux", "-S", str(socket), *args], env=env,
                                    capture_output=True, text=True, timeout=10)
            if check and result.returncode:
                raise AssertionError(f"tmux {args}: {result.stderr}")
            return result.stdout.strip()

        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 120, 0, 0))
        stop = threading.Event()
        client = None
        output = bytearray()

        def drain():
            while not stop.is_set():
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        return

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()

        def click(x, y):
            # SGR mouse coordinates are one-based. Avoid triggering double-click bindings.
            time.sleep(0.4)
            os.write(master, f"\033[<0;{x};{y}M\033[<0;{x};{y}m".encode())

        def active():
            return t("display", "-p", "-t", "local:", "#{pane_id}")

        def routed(pane):
            return t("show", "-qv", "-t", "local", "@ssh_passthrough_pane") == pane

        try:
            t("-f", "/dev/null", "new-session", "-d", "-s", "remote", "-x", "120",
              "-y", "23", "sleep 300", socket=inner)
            t("new-window", "-d", "-t", "remote", "sleep 300", socket=inner)
            t("set", "-g", "mouse", "on", socket=inner)
            t("set", "-g", "status-format[0]",
              "#[range=window|0]one #[range=window|1]two #[norange]", socket=inner)
            local_pane = t("-f", "/dev/null", "new-session", "-d", "-s", "local",
                           "-n", "local", "-x", "120", "-y", "24", "-P", "-F",
                           "#{pane_id}", "sleep 300")
            t("set", "-g", "mouse", "on")
            t("set", "-g", "automatic-rename", "off")
            t("set", "-t", "local", "prefix", "C-a")
            t("set", "-t", "local", "prefix2", "C-x")
            t("source-file", str(ROOT / "config/agent-indicator.conf"))
            for table, action in (("root", "on"), ("off", "off")):
                t("bind", "-T", table, "F12", "run-shell",
                  f"{helper} {action} '#{{session_id}}' '#{{pane_id}}'")
            remote_pane = t("new-window", "-d", "-t", "local:", "-n", "remote",
                            "-P", "-F", "#{pane_id}",
                            "env -u TMUX tmux -S " + shlex.quote(str(inner)) + " attach -t remote")
            t("set", "-p", "-t", remote_pane, "@ssh_reconnect_host", "remote")
            t("set", "-p", "-t", remote_pane, "@ssh_reconnect_pid", str(os.getpid()))
            t("set", "-w", "-t", remote_pane, "@ssh_host", "remote")
            client = subprocess.Popen(["tmux", "-S", str(outer), "attach", "-t", "local"],
                                      env=env, stdin=slave, stdout=slave, stderr=slave)
            wait_for(lambda: str(client.pid) in t("list-clients", "-F", "#{client_pid}"))

            # Click the SSH entry in the shipped right-hand window list, then return locally.
            click(116, 24)
            wait_for(lambda: active() == remote_pane and routed(remote_pane))
            click(2, 24)
            wait_for(lambda: active() == local_pane and routed(""))
            assert t("show", "-v", "-t", "local", "prefix") == "C-a"
            assert t("show", "-v", "-t", "local", "prefix2") == "C-x"
            print("PASS: outer status clicks enter SSH and restore local keyboard controls", flush=True)

            # Select SSH without passthrough; the first click on its nested bar must reach it.
            t("select-window", "-t", remote_pane)
            wait_for(lambda: "one two" in t("capture-pane", "-p", "-t", remote_pane))
            assert routed("")
            click(6, 23)
            wait_for(lambda: t("display", "-p", "-t", "remote", "#{window_index}",
                               socket=inner) == "1" and routed(remote_pane))
            click(2, 23)
            wait_for(lambda: t("display", "-p", "-t", "remote", "#{window_index}",
                               socket=inner) == "0")
            print("PASS: first and subsequent remote status clicks reach the nested tmux", flush=True)

            # A local split must take control back; clicking the SSH split must give it back.
            split = t("split-window", "-d", "-h", "-t", remote_pane, "-P", "-F",
                      "#{pane_id}", "sleep 300")
            left = int(t("display", "-p", "-t", split, "#{pane_left}"))
            click(left + 2, 3)
            wait_for(lambda: active() == split and routed(""))
            click(2, 3)
            wait_for(lambda: active() == remote_pane and routed(remote_pane))
            print("PASS: clicking between local and SSH split panes switches controls", flush=True)

            # Manual F12 still works and an old click cannot reroute a new selection.
            os.write(master, b"\033[24~")
            wait_for(lambda: routed(""))
            t("run-shell", f"{helper} auto '$0' '{split}'")
            assert routed(""), "a stale click changed keyboard routing"
            os.write(master, b"\033[24~")
            wait_for(lambda: routed(remote_pane))
            print("PASS: manual toggling survives and stale clicks are ignored", flush=True)
        except Exception:
            print("outer panes:", t("list-panes", "-a", "-F",
                  "#{pane_id} #{window_index} #{pane_active} #{pane_width}x#{pane_height} #{@ssh_reconnect_host}"))
            print("routing:", t("show", "-t", "local", "-qv", "@ssh_passthrough_pane"))
            print("terminal:", repr(output[-1500:]))
            raise
        finally:
            for socket in (outer, inner):
                t("kill-server", socket=socket, check=False)
            if client is not None:
                client.wait(timeout=5)
            stop.set()
            reader.join(timeout=2)
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    main()
