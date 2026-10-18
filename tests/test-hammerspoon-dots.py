#!/usr/bin/env python3
"""Exercise the menu bar with mock Hammerspoon and real isolated tmux queries."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[1]


def main():
    lua = shutil.which("lua") or shutil.which("lua5.4")
    if not lua:
        print("SKIP: Hammerspoon integration checks require Lua (installed in CI)")
        return
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
            assert len(windows) == 1 and len(windows[0]) == 10, windows
            assert windows[0][8:] == ["running", expected_style], windows
            result = subprocess.run(harness, check=True, capture_output=True, text=True)
            for line in result.stdout.splitlines():
                if line.startswith("PASS:"):
                    print(line)
            print("PASS: remote probe transmits the same effective style as the local query")
        finally:
            subprocess.run([tmux_bin, "-L", socket, "kill-server"], env=environment, capture_output=True)


if __name__ == "__main__":
    main()
