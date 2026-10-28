#!/usr/bin/env python3
"""Exercise the menu bar with mock Hammerspoon and real isolated tmux queries."""

import base64
import os
import pty
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]


def main():
    lua = shutil.which("lua") or shutil.which("lua5.4")
    if not lua:
        print("SKIP: Hammerspoon integration checks require Lua (installed in CI)")
        return
    subprocess.run([lua, str(ROOT / "tests/test-slack-badge.lua"),
                    str(ROOT / "integrations/hammerspoon/slack_badge.lua")], check=True)
    node = shutil.which("node")
    if node:
        subprocess.run([node, str(ROOT / "tests/test-slack-badge.js"),
                        str(ROOT / "scripts/slack-badge.js")], check=True)
    else:
        print("SKIP: direct badge preference checks require Node.js")
    socket = "agent-dots-test-" + uuid.uuid4().hex[:12]
    environment = {key: value for key, value in os.environ.items() if key != "TMUX"}
    tmux_bin = shutil.which("tmux")

    def tmux(*arguments):
        return subprocess.check_output(
            [tmux_bin, "-L", socket, *arguments], env=environment,
            text=True, stderr=subprocess.STDOUT, timeout=8,
        ).strip()

    with tempfile.TemporaryDirectory(prefix="agent-dots-fixture-") as directory:
        fixture = Path(directory)
        harness = [lua, str(ROOT / "tests/test-hammerspoon-dots.lua"),
                   str(ROOT / "integrations/hammerspoon/agent_dots.lua"), directory]
        subprocess.run(harness, check=True, capture_output=True, text=True)
        # An interactive SSH configuration must not make a probe fork or reuse a
        # background master, which could retain Hammerspoon's output pipes.
        ssh = shutil.which("ssh")
        if ssh:
            config = fixture / "ssh_config"
            config.write_text("Host *\n  ControlMaster auto\n  ControlPath /tmp/fixture-master\n"
                              "  ControlPersist 60\n  ForkAfterAuthentication yes\n")
            args = (fixture / "ssh-args.txt").read_text().splitlines()
            effective = subprocess.check_output(
                [ssh, "-G", "-F", str(config), *args[2:-2], "fixture.invalid"],
                text=True, stderr=subprocess.DEVNULL, timeout=5,
            )
            settings = dict(line.split(" ", 1) for line in effective.splitlines())
            assert settings["controlmaster"] == "false", settings["controlmaster"]
            assert settings["controlpersist"] in ("no", "0"), settings["controlpersist"]
            assert settings["forkafterauthentication"] == "no", settings["forkafterauthentication"]
            assert settings.get("controlpath", "none") == "none", settings.get("controlpath")
            print("PASS: real SSH configuration cannot enable background masters for indicator probes")
        clients, terminals = [], []
        try:
            tmux("-f", "/dev/null", "new-session", "-d", "-s", "test", "/bin/bash --noprofile --norc")
            tmux("set", "-g", "status-style", "bg=green,fg=white")
            tmux("set", "-wg", "window-status-style", "bg=cyan,fg=black")
            tmux("set", "-wg", "window-status-current-style", "bg=#123abc,fg=black")
            tmux("set", "-w", "@agent_present", "1")
            tmux("set", "-w", "@agent_label", "agent")
            tmux("set", "-w", "@agent-indicator-state", "running")
            query = shlex.split((fixture / "query.txt").read_text())
            arguments = query[query.index("list-windows"):]
            styles = []

            def check(expected):
                row = tmux(*arguments).splitlines()[0].split("\t")
                assert len(row) == 7, row
                assert row[5] == "running", row
                styles.append(expected + "\t" + row[6])
                return row[6]

            check("#123ABC")
            tmux("set", "-wg", "window-status-current-style", "default")
            check("#06B6D4")
            tmux("set", "-wg", "window-status-current-style", "bold")
            check("#22C55E")
            tmux("new-window", "-t", "test", "/bin/bash --noprofile --norc")
            check("#06B6D4")
            tmux("set", "-wg", "window-status-last-style", "bg=colour208")
            expected_style = check("#FF8700")
            (fixture / "styles.tsv").write_text("\n".join(styles) + "\n")

            # Execute the transmitted remote probe locally. Prevent Docker discovery
            # so this test only touches its own tmux server and never a real remote.
            docker = fixture / "docker"
            docker.write_text("#!/bin/sh\nexit 0\n")
            docker.chmod(0o755)
            socket_path, pid = tmux("display-message", "-p", "#{socket_path}|#{pid}").rsplit("|", 1)
            inside = dict(environment, TMUX=f"{socket_path},{pid},0",
                          PATH=directory + os.pathsep + environment["PATH"])
            probe = subprocess.check_output(
                ["bash", str(fixture / "probe.sh"), "poll"], env=inside, text=True, timeout=8,
            )
            windows = [line.split("\t") for line in probe.splitlines() if line.startswith("W\t")]
            assert len(windows) == 1 and len(windows[0]) == 12, windows
            assert windows[0][8:] == ["running", expected_style, "SSH", ""], windows
            result = subprocess.run(harness, check=True, capture_output=True, text=True)
            for line in result.stdout.splitlines():
                if line.startswith("PASS:"):
                    print(line)
            print("PASS: remote probe transmits the same effective style as the local query")

            # A local proxy keeps ssh open without connecting to any network host.
            ssh_window = tmux("new-window", "-P", "-F", "#{window_id}",
                              "exec /usr/bin/ssh -F /dev/null -o 'ProxyCommand=sleep 60' fixture.invalid")
            for _ in range(30):
                if tmux("display", "-p", "-t", ssh_window, "#{pane_current_command}") == "ssh":
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("SSH fixture did not start")
            helper = next(line for line in (ROOT / "integrations/hammerspoon/README.md").read_text().splitlines()
                          if line.startswith("set -g @ssh_agent_title_style "))
            tmux(*shlex.split(helper))
            for option in ("window-status-format", "window-status-current-format"):
                tmux("set", "-g", option, "#{E:@ssh_agent_title_style}#I:#W#F")
            tmux("set", "-w", "-t", ssh_window, "@ssh_host", "colossus")
            publish = shlex.split((fixture / "ssh-color-command.txt").read_text())
            publish = publish[publish.index("set-option"):]
            publish = [ssh_window if value == "@6" else value for value in publish]
            tmux(*publish)
            assert tmux("show", "-wqv", "-t", ssh_window, "@ssh_agent_color") == "magenta"

            def title(option="window-status-format"):
                return tmux("display", "-p", "-t", ssh_window, "#{E:" + option + "}")

            for option in ("window-status-format", "window-status-current-format"):
                assert title(option).startswith("#[bg=magenta,fg=black,noreverse]"), title(option)
            tmux("set", "-w", "-t", ssh_window, "@agent_label", "local-agent")
            assert not title().startswith("#["), "SSH color must not override local agent titles"
            tmux("set", "-wu", "-t", ssh_window, "@agent_label")
            tmux("set", "-w", "-t", ssh_window, "@ssh_host", "other-host")
            assert not title().startswith("#["), "host change must immediately invalidate cached color"
            tmux("set", "-w", "-t", ssh_window, "@ssh_host", "colossus")
            tmux("set", "-w", "-t", ssh_window, "@ssh_agent_color", "green")
            assert title().startswith("#[bg=green,fg=black,noreverse]"), title()
            tmux("set", "-w", "-t", ssh_window, "@ssh_agent_color", "")
            assert not title().startswith("#["), "failed polls must restore the normal title"
            print("PASS: published SSH colors render in tmux and respect host/local-agent guards")

            # Replay the color payload through the transmitted probe, using only this
            # isolated server and a fake ssh -G. No connection is opened by the relay.
            ssh_config = fixture / "effective-ssh-config"
            fake_ssh = fixture / "ssh"
            fake_ssh.write_text("#!/bin/sh\n[ \"$1\" = -G ] || exit 99\n"
                                "cat " + shlex.quote(str(ssh_config)) + "\n")
            fake_ssh.chmod(0o755)
            tmux("set", "-w", "-t", ssh_window, "@ssh_host", "fixture-alias")
            inside["SSH_CONNECTION"] = "192.0.2.1 40000 192.0.2.10 22"

            def relay(config, colors="192.0.2.10\t22\tfixture-user\tblue\n"):
                ssh_config.write_text(config)
                payload = base64.b64encode(colors.encode()).decode()
                output = subprocess.check_output(
                    ["bash", str(fixture / "probe.sh"), "poll", "", payload],
                    env=inside, text=True, timeout=8,
                )
                assert any(row.startswith("I\t192.0.2.10\t22\t") for row in output.splitlines()), output
                agent_rows = [row.split("\t") for row in output.splitlines() if row.startswith("W\t")]
                assert len(agent_rows) == 1 and all(row[3] != ssh_window for row in agent_rows), agent_rows
                return tmux("show", "-wqv", "-t", ssh_window, "@ssh_agent_color")

            effective = "hostname 192.0.2.10\nport 22\nuser fixture-user\n"
            assert relay(effective) == "blue"
            for option in ("window-status-format", "window-status-current-format"):
                assert title(option).startswith("#[bg=blue,fg=white,noreverse]"), title(option)
            assert relay(effective, "192.0.2.10\t22\tfixture-user\t#123abc\n") == "#123abc"
            assert relay(effective.replace("192.0.2.10", "192.0.2.11")) == "", "an alias is not a host identity"
            assert relay(effective.replace("fixture-user", "other-user")) == "", "accounts must match"
            assert relay(effective.replace("port 22", "port 2222")) == "", "ports must match"
            for routing in ("proxyjump gateway", "proxycommand helper", "bindaddress 192.0.2.5", "bindinterface vpn0"):
                assert relay(effective) == "blue"
                assert relay(effective + routing + "\n") == "", "routing overrides must clear stale colors"
            assert relay(effective, "\n") == "", "an empty palette must clear stale colors"
            assert relay(effective, "192.0.2.10\t22\tfixture-user\tblue\n192.0.2.10\t22\tfixture-user\tred\n") == "", \
                "ambiguous endpoint colors must not be applied"
            assert relay(effective.replace("192.0.2.10", "localhost"),
                         "127.0.0.1\t22\tfixture-user\tblue\n::1\t22\tfixture-user\tblue\n") == "blue"
            print("PASS: relayed SSH titles match address, port, and account without adding remote dots")

            # Real clients identify their source independently of the session name.
            editor_window = tmux("new-session", "-d", "-P", "-F", "#{window_id}", "-s", "project",
                                 "/bin/bash --noprofile --norc")
            tmux("set", "-w", "-t", editor_window, "@agent_present", "1")

            def attach(session, vscode=False):
                master, slave = pty.openpty()
                terminals.extend([master, slave])
                env = {k: v for k, v in environment.items()
                       if k != "TERM_PROGRAM" and not k.startswith("VSCODE_")}
                env["TERM"] = "xterm-256color"
                if vscode:
                    env["TERM_PROGRAM"] = "vscode"
                client = subprocess.Popen([tmux_bin, "-L", socket, "attach-session", "-t", session],
                                          env=env, stdin=slave, stdout=slave, stderr=slave)
                clients.append(client)
                for _ in range(40):
                    if str(client.pid) in tmux("list-clients", "-F", "#{client_pid}").splitlines():
                        return client
                    if client.poll() is not None:
                        raise AssertionError("tmux client did not attach")
                    time.sleep(0.05)
                raise AssertionError("tmux client attachment timed out")

            terminal = attach("test")
            editor = attach("project", vscode=True)

            def sources():
                output = subprocess.check_output(["bash", str(fixture / "probe.sh"), "poll"],
                                                 env=inside, text=True, timeout=8)
                rows = [row.split("\t") for row in output.splitlines() if row.startswith("W\t")]
                return {row[7]: row[10] for row in rows}

            assert sources() == {"test": "SSH", "project": "VS"}, sources()
            tmux("detach-client", "-s", "project")
            editor.wait(timeout=5)
            assert sources()["project"] == "VS", "detaching must preserve the session's source"
            editor = attach("project", vscode=True)
            shared = attach("project")
            assert sources()["project"] == "SSH", "a shared session must appear once under SSH"
            shared.terminate()
            shared.wait(timeout=5)
            assert sources()["project"] == "VS", "the remaining VS client must own its session"

            # Clicking a dot must leave clients from the other application in their own session.
            for session, window, source in [("test", "@0", "SSH"), ("project", editor_window, "VS")]:
                subprocess.run(["bash", str(fixture / "probe.sh"), "jump", "host", session, window, source],
                               env=inside, check=True, capture_output=True, text=True, timeout=8)
                client_sessions = dict(row.split("|", 1) for row in
                                       tmux("list-clients", "-F", "#{client_pid}|#{session_name}").splitlines())
                assert client_sessions[str(terminal.pid)] == "test", client_sessions
                assert client_sessions[str(editor.pid)] == "project", client_sessions
            print("PASS: real tmux clients classify host sessions, retain detached sources, and keep jumps separate")
        finally:
            subprocess.run([tmux_bin, "-L", socket, "kill-server"], env=environment, capture_output=True)
            for client in clients:
                if client.poll() is None:
                    client.terminate()
                client.wait(timeout=5)
            for fd in terminals:
                os.close(fd)


if __name__ == "__main__":
    main()
