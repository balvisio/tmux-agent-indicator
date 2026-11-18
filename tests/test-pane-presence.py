#!/usr/bin/env python3
"""Host panes must not inherit labels/icons from colliding container tty names."""

import os
from pathlib import Path
import subprocess
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[1]


def main():
    socket = "agent-presence-test-" + uuid.uuid4().hex[:12]
    environment = {key: value for key, value in os.environ.items() if key != "TMUX"}

    def tmux(*arguments):
        return subprocess.check_output(["tmux", "-L", socket, *arguments], env=environment,
                                       text=True, stderr=subprocess.STDOUT, timeout=8).strip()

    with tempfile.TemporaryDirectory(prefix="agent-presence-fixture-") as directory:
        fixture = Path(directory)
        snapshot = fixture / "processes"
        ps = fixture / "ps"
        ps.write_text('''#!/bin/sh
if [ "$1" = -t ]; then
    awk -v tty="$2" '$3 == tty { $3=""; print }' "$AGENT_TEST_PS_FILE"
elif case "$*" in *lstart=*) true ;; *) false ;; esac; then
    awk '{printf "%s %s %s Mon Nov 16 12:00:00 2026 ", $1, $2, $3; $1=$2=$3=""; sub(/^ +/, ""); print}' "$AGENT_TEST_PS_FILE"
else
    cat "$AGENT_TEST_PS_FILE"
fi
''')
        ps.chmod(0o755)
        try:
            pane, root, window, tty = tmux("-f", "/dev/null", "new-session", "-d", "-P", "-F",
                                      "#{pane_id}|#{pane_pid}|#{window_id}|#{pane_tty}", "-s", "test",
                                      "/bin/bash --noprofile --norc").split("|")
            tty = tty[5:] if tty.startswith("/dev/") else tty
            path, pid = tmux("display-message", "-p", "#{socket_path}|#{pid}").rsplit("|", 1)
            # A caller may retain an old install path; use this script's own helpers.
            inside = dict(environment, HOME=directory, TMUX=f"{path},{pid},0", TMUX_PANE=pane,
                          TMUX_AGENT_INDICATOR_DIR=str(fixture / "missing-install"), AGENT_TEST_PS_FILE=str(snapshot),
                          PATH=directory + os.pathsep + environment["PATH"])

            def run(script, *args):
                return subprocess.check_output([str(ROOT / "scripts" / script), *args],
                                               env=inside, text=True, timeout=10).strip()

            def option(key):
                return tmux("show-options", "-wqv", "-t", window, key)

            # Simulate host and container processes sharing a pts/N name.
            # The foreign processes belong to a container, not this pane's tree.
            unrelated = (f"900000001 900000000 {tty} /bin/bash\n"
                         f"900000002 900000001 {tty} claude\n"
                         f"900000003 900000001 {tty} node /bin/codex\n"
                         f"900000004 900000001 {tty} ssh wrong-host\n")
            base = f"{root} 1 {tty} /bin/bash\n" + unrelated
            snapshot.write_text(base)
            tmux("set", "-w", "@agent_present", "1")
            tmux("set", "-w", "@agent_label", "claude")
            tmux("set", "-w", "@ssh_host", "wrong-host")
            tmux("set", "-w", "window-status-style", "bg=blue,fg=white")
            run("agent-present.sh", "sweep")
            assert not option("@agent_present"), "container marked shell as an agent"
            assert not option("@agent_label"), "stale Claude label was not cleared"
            assert not option("@ssh_host"), "container SSH leaked to the host window"
            assert "bg=blue" not in option("window-status-style"), "false idle color remains"
            assert not run("indicator.sh"), "container leaked into the fallback icon"
            print("PASS: colliding container ttys cannot label or color a plain host shell")

            snapshot.write_text(base + f"900000010 {root} {tty} claude\n"
                                f"900000011 900000010 {tty} node /bin/codex\n"
                                f"900000012 {root} {tty} ssh -p 2222 correct-host\n")
            run("agent-present.sh", "sweep")
            assert option("@agent_present") == "1"
            assert "🤖" in option("@agent_label") and "🧠" in option("@agent_label")
            assert option("@ssh_host") == "correct-host"
            assert "bg=blue" in option("window-status-style")
            assert run("indicator.sh"), "real descendant agent was lost"
            print("PASS: real descendant agents and SSH connections still label their own pane")

            # Detection must also inspect another pane in the same window.
            second, second_root, second_tty = tmux("split-window", "-d", "-P", "-F", "#{pane_id}|#{pane_pid}|#{pane_tty}",
                                       "-t", pane, "/bin/bash --noprofile --norc").split("|")
            second_tty = second_tty[5:] if second_tty.startswith("/dev/") else second_tty
            snapshot.write_text(base + f"{second_root} 1 {second_tty} /bin/bash\n900000020 {second_root} {second_tty} codex\n")
            run("agent-present.sh", "sweep")
            assert "🧠" in option("@agent_label") and "🤖" not in option("@agent_label")
            assert "🧠" in run("indicator.sh"), "inactive sibling agent lost its icon"
            assert not option("@ssh_host")
            snapshot.write_text(base + f"{second_root} 1 {second_tty} /bin/bash\n")
            run("agent-present.sh", "sweep")
            assert not option("@agent_present") and not option("@agent_label")
            assert not run("indicator.sh")
            print("PASS: sibling agents remain visible and exiting agents lose their labels")
        finally:
            subprocess.run(["tmux", "-L", socket, "kill-server"], env=environment, capture_output=True)


if __name__ == "__main__":
    main()
