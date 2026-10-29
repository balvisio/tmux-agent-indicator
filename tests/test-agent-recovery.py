#!/usr/bin/env python3
"""Exercise actual Resurrect/Continuum with disposable tmux servers and fake agent CLIs."""
import fcntl
import json
import os
from pathlib import Path
import pty
import shlex
import signal
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent


def wait_for(check, seconds=30):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if check():
            return
        time.sleep(0.2)
    raise AssertionError("timed out waiting for recovery")


def main():
    with tempfile.TemporaryDirectory(prefix="test-agent-recovery-") as tmp:
        work = Path(tmp)
        home = work / "home"
        home.mkdir()
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TERM", "USER", "LOGNAME")
               if key in os.environ}
        env.update(HOME=str(home), ZDOTDIR=str(home), TMUX_TMPDIR=str(work),
                   CLAUDE_CONFIG_DIR=str(home / ".claude"), CODEX_HOME=str(home / ".codex"),
                   CODEX_CONFIG_DIR=str(home / ".codex"), XDG_CONFIG_HOME=str(home / ".config"),
                   XDG_STATE_HOME=str(home / ".local/state"), SHELL="/bin/bash")
        plugins = home / ".tmux/plugins"
        installed = plugins / "tmux-agent-indicator"
        recovery = installed / "scripts/agent-recovery.py"
        env["RECOVERY_SCRIPT"] = str(recovery)
        env["RECOVERY_LOG"] = str(work / "agents.jsonl")
        bins = work / "bin"
        bins.mkdir()
        agent = '''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys, time
kind = pathlib.Path(sys.argv[0]).name
if '--help' in sys.argv:
    print('Usage: codex [--no-daemon]')
    sys.exit(0)
args = [arg for arg in sys.argv[1:] if arg not in ('--no-daemon', 'resume', '--resume')]
session = args[0]
reviewer = os.environ.get('KIBITZ_TEST_REVIEWER') == '1'
pair = subprocess.check_output(['tmux', 'display', '-p', '-t', os.environ['TMUX_PANE'],
    '#{@name}|#{@kibitz_host_pane}'], text=True).strip() if reviewer else ''
with open(os.environ['RECOVERY_LOG'], 'a') as out:
    out.write(json.dumps({'kind': kind, 'session': session, 'cwd': os.getcwd(),
        'args': sys.argv[1:], 'reviewer': reviewer, 'pair': pair}) + '\\n')
event = {'session_id': session, 'cwd': os.getcwd(), 'hook_event_name': 'SessionStart'}
subprocess.run([os.environ['RECOVERY_SCRIPT'], 'hook', kind], input=json.dumps(event), text=True, check=True)
while True: time.sleep(1)
'''
        for name in ("claude", "codex"):
            (bins / name).write_text(agent)
            (bins / name).chmod(0o755)
        (bins / "autossh").write_text("#!/bin/sh\nexec sleep 300\n")
        (bins / "autossh").chmod(0o755)
        launcher = home / ".local/bin/kibitz_reviewer_launch"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("""#!/usr/bin/env python3
import os, sys
assert len(sys.argv) == 4 and sys.argv[1] == 'resume', sys.argv
kind, session = sys.argv[2:]
os.environ['KIBITZ_TEST_REVIEWER'] = '1'
args = ['--no-daemon', 'resume', session] if kind == 'codex' else ['--resume', session]
os.execvp(kind, [kind, *args])
""")
        launcher.chmod(0o755)
        env["PATH"] = str(bins) + ":" + env["PATH"]
        sock = work / "socket"
        neighbors = [work / "neighbor-1", work / "neighbor-2"]
        clients = []
        terminals = []

        def t(*args, check=True):
            result = subprocess.run(["tmux", "-S", str(sock), *args], env=env,
                                    text=True, capture_output=True, cwd=work)
            if check and result.returncode:
                raise AssertionError(f"tmux {args}: {result.stderr}")
            return result.stdout.strip()

        def run(*args, check=True):
            result = subprocess.run(args, env=env, text=True, capture_output=True)
            if check and result.returncode:
                raise AssertionError(f"{args}: {result.stdout}\n{result.stderr}")
            return result

        def stop_server():
            pid = int(t("display", "-p", "#{pid}"))
            t("kill-server")

            def stopped():
                try:
                    os.kill(pid, 0)
                    return False
                except ProcessLookupError:
                    return True

            wait_for(stopped)

        def install():
            run(str(ROOT / "install.sh"), "--no-alias", "--no-hammerspoon", "--no-opencode")

        def logs():
            path = work / "agents.jsonl"
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

        def agent_panes():
            result = {}
            for line in t("list-panes", "-a", "-F", "#{pane_id}\t#{@agent_recovery}").splitlines():
                pane, _, raw = line.partition("\t")
                if raw:
                    data = json.loads(raw)
                    if data.get("session"):
                        result[data["session"]] = pane
            return result

        def attach(session):
            master, slave = pty.openpty()
            terminals.extend((master, slave))
            client_env = {key: value for key, value in env.items() if key not in ("TMUX", "TMUX_PANE")}
            client_env["TERM"] = "xterm-256color"
            client = subprocess.Popen(["tmux", "-S", str(sock), "attach-session", "-t", session],
                                      env=client_env, stdin=slave, stdout=slave, stderr=slave)
            clients.append(client)
            wait_for(lambda: str(client.pid) in t("list-clients", "-F", "#{client_pid}").splitlines())
            return client

        def client_sessions():
            return dict(line.split("\t") for line in
                        t("list-clients", "-F", "#{client_pid}\t#{session_name}").splitlines())

        try:
            # Real extra servers exercise the process-count guard without changing ps output.
            for neighbor in neighbors:
                run("tmux", "-S", str(neighbor), "-f", "/dev/null", "new-session", "-d", "-s", "neighbor", "/bin/sleep 300")
            install()
            local_config = home / ".tmux/agent-indicator.local.conf"
            customizations = (
                'set-environment -g PATH ' + shlex.quote(env["PATH"]) + '\n'
                "set -g default-command 'exec /bin/bash --noprofile --norc'\n"
                "set-hook -g 'window-layout-changed[81]' 'display-message fixture-user-hook'\n"
            )
            local_config.write_text(customizations)
            assert (plugins / "tmux-resurrect/resurrect.tmux").is_file()
            assert (plugins / "tmux-continuum/continuum.tmux").is_file()
            cwd = work / "project with spaces"
            cwd.mkdir()
            t("-f", "/dev/null", "new-session", "-d", "-s", "work", "-x", "150", "-y", "40",
              "-c", str(cwd), "/bin/bash --noprofile --norc")
            env["TMUX"] = t("display", "-p", "#{socket_path},#{pid},0")
            run("bash", "-c", '. "$1"; another_tmux_server_running_on_startup', "bash",
                str(plugins / "tmux-continuum/scripts/helpers.sh"))
            t("source-file", str(home / ".tmux.conf"))
            wait_for(lambda: "continuum_save.sh" in t("show", "-gqv", "status-right"))
            wait_for(lambda: t("show", "-gqv", "@agent_recovery_startup") == "ready")
            run("python3", "-B", "-c", '''
import hashlib, importlib.util, socket, sys
spec = importlib.util.spec_from_file_location("recovery", sys.argv[1])
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
socket.gethostname = lambda: "home-network"
first = recovery.state_dir()
socket.gethostname = lambda: "office-network"
assert recovery.state_dir() == first
address = recovery.identity().split("|")[0]
assert first.name == hashlib.sha256(address.encode()).hexdigest()[:20]
recovery.identity = lambda: address + "-another-socket|123|456"
assert recovery.state_dir() != first
''', str(recovery))
            print("PASS: snapshot lookup survives hostname changes and keeps sockets separate", flush=True)
            first = t("display", "-p", "#{pane_id}")
            t("respawn-pane", "-k", "-t", first, "-c", str(cwd), str(bins / "claude") + " claude-one")
            second = t("split-window", "-h", "-t", first, "-P", "-F", "#{pane_id}", "-c", str(cwd),
                       str(bins / "codex") + " codex-two")
            third = t("new-window", "-d", "-P", "-F", "#{pane_id}", "-c", str(cwd),
                      str(bins / "codex") + " codex-three")
            fourth = t("split-window", "-h", "-t", third, "-P", "-F", "#{pane_id}", "-c", str(cwd),
                       str(bins / "claude") + " claude-four")
            for reviewer, host, label in ((second, first, "codex"), (fourth, third, "claude-reviewer")):
                t("set", "-p", "-t", reviewer, "@name", label, ";",
                  "set", "-p", "-t", reviewer, "@kibitz_host_pane", host)
            # Restore one reviewer before its host, exercising deferred link resolution.
            t("swap-pane", "-d", "-s", third, "-t", fourth)
            ssh = t("new-window", "-d", "-P", "-F", "#{pane_id}",
                    str(installed / "scripts/ssh-reconnect.sh") + " recovery-fixture.invalid -p 2222")
            (work / "empty.txt").touch()
            t("new-window", "-d", "tail -f " + str(work / "empty.txt"))
            t("new-session", "-d", "-s", "z-editor")
            for pane in (first, second, third, fourth, ssh):
                wait_for(lambda p=pane: bool(t("show", "-pqv", "-t", p, "@agent_recovery")))
            snapshots = home / ".local/state/tmux-agent-indicator/recovery"

            def saved_all():
                run(str(recovery), "save")
                for path in snapshots.glob("*/last"):
                    snapshot = path.read_text()
                    if snapshot.count("agent-recovery.py resume") == 5 and "pane\tz-editor\t" in snapshot:
                        return True
                return False

            wait_for(saved_all)
            latest = next(snapshots.glob("*/last"))
            assert latest.is_file()
            original = latest.read_text()
            assert original.count("agent-recovery.py resume") == 5, original
            assert "state\t\t" in original, "the fixture must save without an attached client"
            windows = t("list-windows", "-t", "work", "-F", "#{window_index}|#{window_layout}")

            # Update while running: both status jobs survive, and hook entries remain singular.
            install()
            run(str(recovery), "setup")
            assert len(logs()) == 4, "reloading after a snapshot is created must not restore it"
            status = t("show", "-gqv", "status-right")
            assert status.count("continuum_save.sh") == 1 and "agent-present.sh sweep" in status
            assert status.count("?@agent_recovery_warning") == 1
            assert "fixture-user-hook" in t("show-hooks", "-g", "window-layout-changed")
            for path in (home / ".claude/settings.json", home / ".codex/hooks.json"):
                hooks = json.loads(path.read_text())["hooks"]
                assert sum("agent-recovery.py" in item.get("command", "") for entry in hooks["SessionStart"] for item in entry["hooks"]) == 1
            print("PASS: reloads preserve periodic saving alongside other real tmux servers without restoring old layouts", flush=True)

            # The normal startup path must actually auto-restore, without an explicit restore call.
            restore_script = plugins / "tmux-resurrect/scripts/restore.sh"
            restore_source = restore_script.read_text()
            restore_real = restore_script.with_name("restore-real.sh")
            restore_real.write_text(restore_source)
            restore_started = work / "restore-started"
            restore_release = work / "restore-release"
            restore_script.write_text(
                "#!/bin/sh\n" + "touch " + shlex.quote(str(restore_started)) + "\n"
                "while [ ! -f " + shlex.quote(str(restore_release)) + " ]; do sleep 0.1; done\n"
                "exec bash " + shlex.quote(str(restore_real)) + ' "$@"\n'
            )
            original = latest.read_text()
            server_pid = int(t("display", "-p", "#{pid}"))
            os.kill(server_pid, signal.SIGKILL)
            env.pop("TMUX")
            time.sleep(0.3)
            # Consume a pane ID so every restored pair must use new IDs.
            t("-f", "/dev/null", "new-session", "-d", "-s", "placeholder", "/bin/sleep 300")
            t("new-session", "-d", "-s", "work", "-x", "150", "-y", "40", "/bin/bash --noprofile --norc")
            t("kill-session", "-t", "placeholder")
            env["TMUX"] = t("display", "-p", "#{socket_path},#{pid},0")
            terminal = attach("work")
            t("source-file", str(home / ".tmux.conf"))
            wait_for(restore_started.exists)
            assert t("show", "-gqv", "@agent_recovery_startup") == "pending"
            # A restore slower than the former five-second grace period must still protect last.
            time.sleep(5.1)
            run(str(recovery), "save")
            t("set", "-g", "@continuum-save-last-timestamp", "0")
            run(str(plugins / "tmux-continuum/scripts/continuum_save.sh"))
            time.sleep(0.8)
            assert latest.read_text() == original, "startup saves replaced the unrestored snapshot"
            restore_release.touch()
            wait_for(lambda: len(logs()) >= 8)
            restored = logs()[4:]
            assert sorted(item["session"] for item in restored) == ["claude-four", "claude-one", "codex-three", "codex-two"], restored
            assert all(Path(item["cwd"]).resolve() == cwd.resolve() for item in restored)
            assert all("--no-daemon" in item["args"] for item in restored if item["kind"] == "codex")
            wait_for(lambda: "recovery-fixture.invalid" in t("list-panes", "-a", "-F", "#{@ssh_reconnect_host}"))
            wait_for(lambda: bool(t("show", "-gqv", "@agent_recovery_restored")))
            wait_for(lambda: t("show", "-gqv", "@agent_recovery_startup") == "ready")
            restore_script.write_text(restore_source)
            assert client_sessions()[str(terminal.pid)] == "work", client_sessions()
            assert len(t("list-panes", "-a").splitlines()) == 7
            agents = agent_panes()
            for reviewer, host, label, old_host in (
                    ("codex-two", "claude-one", "codex", first),
                    ("claude-four", "codex-three", "claude-reviewer", third)):
                assert agents[host] != old_host, "fixture must change the host pane IDs"
                assert t("show", "-pqv", "-t", agents[reviewer], "@kibitz_host_pane") == agents[host]
                launched = next(item for item in restored if item["session"] == reviewer)
                assert launched["reviewer"] and launched["pair"] == label + "|" + agents[host], launched
            assert all(not item["reviewer"] for item in restored if item["session"] in ("claude-one", "codex-three"))
            print("PASS: two Kibitz pairs restore distinct host links before launching their reviewer roles", flush=True)
            assert "tail" not in t("list-panes", "-a", "-F", "#{pane_current_command}").splitlines()
            assert len(t("list-windows", "-t", "work").splitlines()) == len(windows.splitlines())
            before = len(logs())
            run(str(recovery), "restore")
            run(str(recovery), "restore", "manual")
            time.sleep(0.5)
            assert len(logs()) == before
            print("PASS: startup restores conversations, splits and SSH with other servers running; saves wait for slow restoration", flush=True)

            # Each terminal keeps its own session, even when the snapshot names another one.
            editor = attach("z-editor")
            expected_sessions = {str(terminal.pid): "work", str(editor.pid): "z-editor"}
            snapshot = latest.read_text()
            restore_count = work / "restore-count"
            restore_script.write_text(
                "#!/bin/sh\n" + "echo restore >> " + shlex.quote(str(restore_count)) + "\n"
                "exec bash " + shlex.quote(str(restore_real)) + ' "$@"\n'
            )
            for saved_session in ("", "work", "z-editor"):
                latest.write_text("\n".join(
                    "state\t" + saved_session + "\t" if line.startswith("state\t") else line
                    for line in snapshot.splitlines()) + "\n")
                count = len(restore_count.read_text().splitlines()) if restore_count.exists() else 0

                def restored():
                    run(str(recovery), "restore", "manual")
                    return restore_count.exists() and len(restore_count.read_text().splitlines()) > count

                wait_for(restored)
                assert client_sessions() == expected_sessions, client_sessions()
            restore_script.write_text(restore_source)
            for client in (terminal, editor):
                name = t("list-clients", "-F", "#{client_pid}\t#{client_name}")
                for line in name.splitlines():
                    pid, target = line.split("\t")
                    if pid == str(client.pid):
                        t("detach-client", "-t", target)
                client.wait(timeout=5)
            print("PASS: restoration preserves each attached terminal's session with empty or conflicting saved selection", flush=True)

            # Killing one terminal agent removes just its resume command from the next snapshot.
            pane = t("list-panes", "-a", "-F", "#{pane_id}\t#{@agent_recovery}").splitlines()
            victim = next(line.split("\t")[0] for line in pane if '"session":"codex-two"' in line)
            t("respawn-pane", "-k", "-t", victim, "/bin/sleep 60")
            time.sleep(5.1)
            t("set", "-g", "@continuum-save-last-timestamp", "0")
            run(str(plugins / "tmux-continuum/scripts/continuum_save.sh"))
            wait_for(lambda: latest.read_text().count("agent-recovery.py resume") == 4)
            assert latest.read_text().count("agent-recovery.py resume") == 4
            saved = latest.read_text()
            # Turning the feature off must remove its callbacks without changing the agent sweep.
            t("set", "-g", "@agent-indicator-recovery", "off")
            run(str(recovery), "setup")
            assert "continuum_save.sh" not in t("show", "-gqv", "status-right")
            assert "agent-present.sh sweep" in t("show", "-gqv", "status-right")
            t("set", "-g", "@agent-indicator-recovery", "on")
            run(str(recovery), "setup")
            save_script = plugins / "tmux-resurrect/scripts/save.sh"
            save_source = save_script.read_text()
            health_file = latest.parent / "health.json"
            # Finish any successful background writer before injecting a failure and recording
            # the last good save. Snapshot publication precedes its health-file update.
            with (latest.parent / "lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                save_script.write_text("#!/bin/sh\necho 'fixture save failure details' >&2\nexit 1\n")
                success = json.loads(health_file.read_text())["last_success"]
                saved = latest.read_text()
            active = t("display", "-p", "#{pane_id}")
            assert t("display", "-p", "-t", active, "#{pane_in_mode}") == "0"
            # Exercise the actual event hook and its background run-shell command.
            t("rename-window", "-t", active, "save-failure-fixture")
            wait_for(lambda: bool(t("show", "-gqv", "@agent_recovery_warning")))
            warning = t("show", "-gqv", "@agent_recovery_warning")
            assert "Recovery: save failed" in warning and "last saved " in warning, warning
            assert time.strftime("%H:%M", time.localtime(success)) in warning, warning
            assert "Recovery: save failed" in t("display", "-p", "#{T:status-format[0]}")
            error_log = latest.parent / "errors.log"
            assert "fixture save failure details" in error_log.read_text()
            assert t("display", "-p", "-t", active, "#{pane_in_mode}") == "0", "background error interrupted the pane"
            assert json.loads(health_file.read_text())["last_success"] == success
            direct = run(str(recovery), "save", check=False)
            assert direct.returncode != 0 and direct.stderr
            for command in ((str(recovery), "save", "--background"),
                            (str(installed / "scripts/recovery-save.sh"), "quiet")):
                quiet = run(*command)
                assert not quiet.stdout and not quiet.stderr
            timeout = run("python3", "-B", "-c", '''
import importlib.util, subprocess, sys
spec = importlib.util.spec_from_file_location("recovery", sys.argv[1])
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
def timeout():
    raise subprocess.TimeoutExpired(["tmux", "show-option"], 10)
recovery.enabled = timeout
sys.argv = [sys.argv[1], "save", "--background"]
sys.exit(recovery.main())
''', str(recovery))
            assert not timeout.stdout and not timeout.stderr
            assert "timed out" in error_log.read_text()
            assert json.loads(health_file.read_text())["last_success"] == success
            # Reload keeps the warning; no attempted save has replaced the valid snapshot.
            run(str(recovery), "setup")
            assert t("show", "-gqv", "@agent_recovery_warning") == warning
            assert latest.read_text() == saved
            save_script.write_text(save_source)
            # A save can return successfully without writing when another
            # background writer holds the lock. Wait for a successful writer.
            wait_for(lambda: run(str(recovery), "save").returncode == 0
                     and not t("show", "-gqv", "@agent_recovery_warning"))
            saved = latest.read_text()
            saved_path = latest.resolve()
            save_script.write_text("#!/bin/sh\nexit 1\n")
            run(str(recovery), "save", "--background")
            assert t("show", "-gqv", "@agent_recovery_warning")
            save_script.write_text(save_source)
            wait_for(lambda: run(str(recovery), "save", "--background").returncode == 0
                     and not t("show", "-gqv", "@agent_recovery_warning"))
            assert latest.resolve() == saved_path, "unchanged layout should reuse its snapshot"
            assert not t("show", "-gqv", "@agent_recovery_warning")
            assert not json.loads(health_file.read_text())["error"]
            # Keep subsequent startup-policy fixtures from saving over their recovery input.
            save_script.write_text("#!/bin/sh\nexit 1\n")
            print("PASS: automatic failures log details and warn without entering view mode; successful saves clear the warning", flush=True)

            # A real initial config load delayed beyond ten seconds must still restore.
            stop_server()
            env.pop("TMUX")
            delayed = work / "slow-start.conf"
            delayed.write_text("run-shell 'sleep 12'\nsource-file " + shlex.quote(str(home / ".tmux.conf")) + "\n")
            before = len(logs())
            t("-f", str(delayed), "new-session", "-d", "-s", "delayed")
            env["TMUX"] = t("display", "-p", "#{socket_path},#{pid},0")
            wait_for(lambda: t("show", "-gqv", "@agent_recovery_startup") == "ready")
            assert float(t("show", "-gqv", "@agent_recovery_loaded")) - int(t("display", "-p", "#{start_time}")) >= 12
            assert t("show", "-gqv", "@agent_recovery_restored")
            wait_for(lambda: len(logs()) == before + 3)
            assert "recovery-fixture.invalid" in t("list-panes", "-a", "-F", "#{@ssh_reconnect_host}")
            assert t("show", "-gqv", "@agent_recovery_startup_reason") == "restore requested"
            t("source-file", str(home / ".tmux.conf"))
            assert len(logs()) == before + 3
            print("PASS: delayed initial configuration restores conversations and SSH after the former startup deadline", flush=True)

            # Startup decisions are retained even without a successful automatic restoration.
            before = len(logs())
            halt = home / "tmux_no_auto_restore"
            for mode in ("live-install", "restore-off", "halt-file"):
                stop_server()
                env.pop("TMUX")
                extra = ""
                if mode == "restore-off":
                    extra += "set -g @continuum-restore off\n"
                if mode == "halt-file":
                    halt.touch()
                local_config.write_text(customizations + extra)
                config = "/dev/null" if mode == "live-install" else str(home / ".tmux.conf")
                t("-f", config, "new-session", "-d", "-s", "untouched")
                env["TMUX"] = t("display", "-p", "#{socket_path},#{pid},0")
                if mode == "live-install":
                    install()
                wait_for(lambda: t("show", "-gqv", "@agent_recovery_startup") == "ready")
                assert not t("show", "-gqv", "@agent_recovery_restored"), mode
                assert t("list-sessions", "-F", "#{session_name}") == "untouched", mode
                # Enabling restore on reload must not revisit the startup decision.
                halt.unlink(missing_ok=True)
                local_config.write_text(customizations)
                t("source-file", str(home / ".tmux.conf"))
                time.sleep(1.2)
                assert len(logs()) == before and not t("show", "-gqv", "@agent_recovery_restored"), mode
                assert t("show", "-gqv", "status-right").count("continuum_save.sh") == 1
            print("PASS: live installation, disabled restore and the halt file prevent startup recovery across reloads", flush=True)

            stop_server()
            env.pop("TMUX")
            run(str(ROOT / "install.sh"), "--uninstall")
            assert not (plugins / "tmux-resurrect").exists()
            assert not (plugins / "tmux-continuum").exists()
            assert latest.exists(), "uninstall must preserve saved conversations/layouts"
            for path in (home / ".claude/settings.json", home / ".codex/hooks.json"):
                assert "agent-recovery.py" not in path.read_text()
            print("PASS: uninstall removes owned dependencies and hooks, preserving recovery data", flush=True)
            # Existing dependency installations are used as-is and remain owned by the user.
            for name in ("resurrect", "continuum"):
                directory = plugins / ("tmux-" + name)
                directory.mkdir()
                (directory / (name + ".tmux")).write_text("#!/bin/sh\nexit 0\n")
                (directory / "user-file").write_text("keep me")
            install()
            run(str(ROOT / "install.sh"), "--uninstall")
            for name in ("resurrect", "continuum"):
                assert (plugins / ("tmux-" + name) / "user-file").read_text() == "keep me"
            print("PASS: pre-existing recovery plugins survive installation and uninstall", flush=True)
        except Exception:
            print("RECOVERY LOG:", logs(), flush=True)
            print("PANES:", t("list-panes", "-a", "-F", "#{pane_id}|#{session_name}|#{pane_current_path}", check=False), flush=True)
            print("RESTORED:", t("show-options", "-gqv", "@agent_recovery_restored", check=False), flush=True)
            for pane in t("list-panes", "-a", "-F", "#{pane_id}", check=False).splitlines():
                print("SCREEN:", t("capture-pane", "-p", "-t", pane, check=False), flush=True)
            raise
        finally:
            t("kill-server", check=False)
            for client in clients:
                if client.poll() is None:
                    client.terminate()
                client.wait(timeout=5)
            for terminal in terminals:
                os.close(terminal)
            for neighbor in neighbors:
                run("tmux", "-S", str(neighbor), "kill-server", check=False)


if __name__ == "__main__":
    main()
