#!/usr/bin/env python3
"""Acquire a server-scoped POSIX record lock, then become the Bash state writer.

Record locks survive exec, are not inherited across fork, and are released on
process death (including SIGKILL). Keeping descriptor 9 open lets the shell
release the lock explicitly before spawning notifications.
"""

import errno
import fcntl
import os
import subprocess
import sys
import time


LOCK_FD = 9
WAIT_SECONDS = 2.0


def main():
    if len(sys.argv) < 2:
        print("usage: state-lock.py <script> [arguments...]", file=sys.stderr)
        return 2

    server = subprocess.check_output(
        ["tmux", "display-message", "-p", "#{socket_path}|#{pid}"],
        text=True, timeout=1,
    ).strip()
    socket, pid = server.rsplit("|", 1)
    # Use the socket directory so all clients agree, regardless of HOME/TMPDIR.
    # Never unlink a lock file: existing waiters must keep using the same inode.
    path = f"{socket}.agent-indicator-{pid}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    if fd != LOCK_FD:
        os.dup2(fd, LOCK_FD)
        os.close(fd)
    os.set_inheritable(LOCK_FD, True)

    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        try:
            fcntl.lockf(LOCK_FD, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            if time.monotonic() >= deadline:
                print("tmux-agent-indicator: state lock busy for 2 seconds", file=sys.stderr)
                return 75
            time.sleep(0.05)

    # Exec keeps the PID and its record lock; no supervisor or lock-holder child remains.
    os.execvp("bash", ["bash", sys.argv[1], "--agent-state-lock-held", *sys.argv[2:]])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"tmux-agent-indicator: cannot acquire state lock: {error}", file=sys.stderr)
        sys.exit(1)
