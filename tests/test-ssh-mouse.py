#!/usr/bin/env python3
"""Send real terminal mouse events through nested, isolated tmux servers."""
import fcntl
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import struct
import subprocess
import tempfile
import termios
import threading
import time

ROOT = Path(__file__).resolve().parent.parent


def wait_for(check, timeout=10):
    deadline = time.monotonic() + timeout
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

        def start_drag(x, end, y):
            time.sleep(0.4)
            os.write(master, f"\033[<0;{x};{y}M\033[<32;{end};{y}M".encode())

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

            # A local click must not run an SSH helper behind the state lock:
            # that blocks this client's subsequent drag/keyboard events too.
            server_pid = t("display", "-p", "#{pid}")
            lock_path = Path(f"{outer}.agent-indicator-{server_pid}.lock")
            t("bind", "-T", "root", "F11", "set", "-g", "@mouse-responsive", "yes")
            with lock_path.open("a+") as lock:
                fcntl.lockf(lock, fcntl.LOCK_EX)
                click(3, 3)
                os.write(master, b"\033[23~")
                wait_for(lambda: t("show", "-gqv", "@mouse-responsive") == "yes", timeout=1.5)
            assert routed("")
            print("PASS: local mouse clicks keep the client responsive while the state writer is locked", flush=True)

            # Routing into SSH must not hold up the next click while an agent
            # update owns the state lock. Rapidly leaving SSH invalidates the
            # earlier routing request as well.
            with lock_path.open("a+") as lock:
                fcntl.lockf(lock, fcntl.LOCK_EX)
                click(116, 24)
                wait_for(lambda: active() == remote_pane, timeout=1)
                click(2, 24)
                wait_for(lambda: active() == local_pane, timeout=1)
            wait_for(lambda: routed(""))
            print("PASS: switching back from SSH stays responsive during an agent update", flush=True)

            # Even another routing operation cannot block the client's next click.
            route_lock = Path(f"{outer}.agent-ssh-{server_pid}.lock")
            with route_lock.open("a+") as lock:
                fcntl.lockf(lock, fcntl.LOCK_EX)
                click(116, 24)
                wait_for(lambda: active() == remote_pane, timeout=1)
                click(2, 24)
                wait_for(lambda: active() == local_pane, timeout=1)
            wait_for(lambda: routed(""))
            print("PASS: queued SSH routing does not block subsequent mouse events", flush=True)

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

            # Pause each automatic change after its initial selection check but
            # before tmux applies it. A newer selection must win in both directions.
            wrapper_dir = work / "bin"
            wrapper_dir.mkdir()
            ready, release = work / "route-ready", work / "route-release"
            wrapper = wrapper_dir / "tmux"
            wrapper.write_text("#!/usr/bin/env python3\nimport os,sys,time\nfrom pathlib import Path\n"
                "if sys.argv[1:] == ['source-file', '-']:\n"
                f"    Path({str(ready)!r}).touch()\n"
                "    deadline = time.monotonic() + 10\n"
                f"    while not Path({str(release)!r}).exists():\n"
                "        if time.monotonic() >= deadline: sys.exit(1)\n"
                "        time.sleep(.02)\n"
                f"os.execv({shutil.which('tmux')!r}, [{shutil.which('tmux')!r}, *sys.argv[1:]])\n")
            wrapper.chmod(0o755)
            session = t("display", "-p", "-t", "local:", "#{session_id}")
            route_env = dict(env, TMUX=f"{outer},{server_pid},0",
                             PATH=str(wrapper_dir) + os.pathsep + env["PATH"])
            for target, replacement in ((remote_pane, local_pane), (local_pane, remote_pane)):
                t("run-shell", shlex.join([helper, "off", session]))
                if target == local_pane:
                    t("run-shell", shlex.join([helper, "on", session, remote_pane]))
                t("select-window", "-t", target)
                ready.unlink(missing_ok=True)
                release.unlink(missing_ok=True)
                delayed = subprocess.Popen([helper, "auto", session, target], env=route_env,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    wait_for(ready.exists)
                    t("select-window", "-t", replacement)
                finally:
                    release.touch()
                    out, err = delayed.communicate(timeout=10)
                assert delayed.returncode == 0, (out, err)
                assert routed("" if target == remote_pane else remote_pane)
            print("PASS: switching panes during routing cannot apply stale keyboard settings", flush=True)

            # Manual F12 still works and an old click cannot reroute a new selection.
            os.write(master, b"\033[24~")
            wait_for(lambda: routed(""))
            t("run-shell", f"{helper} auto '$0' '{split}'")
            assert routed(""), "a stale click changed keyboard routing"
            os.write(master, b"\033[24~")
            wait_for(lambda: routed(remote_pane))
            print("PASS: manual toggling survives and stale clicks are ignored", flush=True)

            # A manual toggle must not detach from its binding after the normal
            # state-hook timeout: its next key must use the restored key table.
            t("set", "-g", "@mouse-responsive", "")
            with route_lock.open("a+") as lock:
                fcntl.lockf(lock, fcntl.LOCK_EX)
                os.write(master, b"\033[24~\033[23~")
                time.sleep(2.3)
                assert not t("show", "-gqv", "@mouse-responsive")
                assert routed(remote_pane)
            wait_for(lambda: routed("") and t("show", "-gqv", "@mouse-responsive") == "yes")
            print("PASS: the key after a contended manual toggle uses the restored local table", flush=True)

            # A plain SSH terminal does not request mouse reporting. Passthrough
            # must still enter local copy mode on drag and copy on mouse release.
            # The clipboard command writes only inside this disposable fixture.
            fake_ssh = work / "ssh"
            shutil.copyfile(shutil.which("sleep"), fake_ssh)
            fake_ssh.chmod(0o755)
            plain = t("new-window", "-t", "local:", "-n", "plain-ssh", "-P", "-F", "#{pane_id}",
                      "printf '\\033[2J\\033[Hcopy-me please\\n'; exec "
                      + shlex.join([str(fake_ssh), "300"]))
            wait_for(lambda: t("display", "-p", "-t", plain, "#{pane_current_command}") == "ssh")
            copied = work / "copied"
            t("set", "-s", "copy-command", "cat > " + shlex.quote(str(copied)))
            for table in ("copy-mode", "copy-mode-vi"):
                t("bind", "-T", table, "MouseDragEnd1Pane", "send-keys", "-X", "copy-pipe-and-cancel")
            click(2, 1)
            wait_for(lambda: routed(plain))
            assert t("display", "-p", "-t", plain, "#{mouse_any_flag}") == "0"
            for mode in ("emacs", "vi"):
                t("set", "-w", "-t", plain, "mode-keys", mode)
                copied.unlink(missing_ok=True)
                start_drag(1, 8, 1)
                wait_for(lambda: t("display", "-p", "-t", plain, "#{pane_in_mode}") == "1")
                os.write(master, b"\033[<0;8;1m")
                wait_for(lambda: copied.exists() and copied.read_text().strip() == "copy-me")
                assert t("show-buffer") == "copy-me"
                wait_for(lambda: t("display", "-p", "-t", plain, "#{pane_in_mode}") == "0")
                assert routed(plain), "copying changed SSH keyboard routing"
            print("PASS: plain SSH supports drag-to-copy in both copy modes while passthrough remains active", flush=True)

            # Mouse-aware SSH applications still receive the drag. For nested
            # tmux, only the remote server should select and invoke its copy command.
            remote_copied = work / "remote-copied"
            remote_content = t("display", "-p", "-t", "remote:0", "#{pane_id}", socket=inner)
            t("respawn-pane", "-k", "-t", remote_content,
              "printf '\\033[2J\\033[Hremote-copy please\\n'; exec sleep 300", socket=inner)
            t("set", "-s", "copy-command", "cat > " + shlex.quote(str(remote_copied)), socket=inner)
            t("bind", "-T", "copy-mode", "MouseDragEnd1Pane", "send-keys", "-X", "copy-pipe-and-cancel", socket=inner)
            t("select-window", "-t", remote_pane)
            t("select-pane", "-t", remote_pane)
            wait_for(lambda: "remote-copy please" in t("capture-pane", "-p", "-t", remote_pane))
            click(2, 1)
            wait_for(lambda: routed(remote_pane))
            copied.unlink()
            start_drag(1, 12, 1)
            wait_for(lambda: t("display", "-p", "-t", remote_content, "#{pane_in_mode}", socket=inner) == "1")
            assert t("display", "-p", "-t", remote_pane, "#{pane_in_mode}") == "0"
            os.write(master, b"\033[<0;12;1m")
            wait_for(lambda: remote_copied.exists() and remote_copied.read_text().strip() == "remote-copy")
            assert not copied.exists(), "remote selection invoked the local clipboard command"
            assert routed(remote_pane)
            print("PASS: nested tmux receives mouse selection and copies remotely without entering local copy mode", flush=True)
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
