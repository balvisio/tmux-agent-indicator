#!/usr/bin/env python3
"""Acquire a server-scoped POSIX record lock, then become the Bash state writer.

Record locks survive exec, are not inherited across fork, and are released on
process death (including SIGKILL). Keeping descriptor 9 open lets the shell
release the lock explicitly before spawning notifications.
"""

import errno
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


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

    # Each waiter owns a ticket. Keep acquisition FIFO even after a hook returns
    # to its agent; conditional updates must not overtake the event they follow.
    queue = Path(path + ".queue")
    queue.mkdir(mode=0o700, exist_ok=True)
    ticket = queue / f"{time.monotonic_ns():020d}-{uuid.uuid4().hex}"
    initial = ticket.with_suffix(".tmp")
    initial.write_text(str(os.getpid()))
    os.replace(initial, ticket)
    deadline = time.monotonic() + WAIT_SECONDS
    detached = False
    try:
        while True:
            for candidate in sorted(p for p in queue.iterdir() if not p.name.endswith(".tmp")):
                try:
                    owner = candidate.read_text()
                    os.kill(int(owner), 0)
                except ProcessLookupError:
                    try:
                        if candidate.read_text() == owner:
                            candidate.unlink(missing_ok=True)
                    except FileNotFoundError:
                        pass
                    continue
                except FileNotFoundError:
                    continue
                if candidate == ticket:
                    try:
                        fcntl.lockf(LOCK_FD, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError as error:
                        if error.errno not in (errno.EACCES, errno.EAGAIN):
                            raise
                    else:
                        current = subprocess.check_output(
                            ["tmux", "display-message", "-p", "#{socket_path}|#{pid}"],
                            text=True, timeout=1,
                        ).strip()
                        if current != server:
                            return 0
                        ticket.unlink()
                        os.execvp("bash", ["bash", sys.argv[1], "--agent-state-lock-held", *sys.argv[2:]])
                break

            if time.monotonic() >= deadline:
                if not detached:
                    # Hand the ticket to a detached child before returning success.
                    # Closing all standard streams prevents hook runners from waiting
                    # for the child or killing it with the original process group.
                    ready_read, ready_write = os.pipe()
                    child = os.fork()
                    if child:
                        os.close(ready_write)
                        ready = os.read(ready_read, 1)
                        os.close(ready_read)
                        if ready != b"1":
                            raise RuntimeError("state retry could not detach")
                        os._exit(0)
                    os.close(ready_read)
                    os.setsid()
                    with open(os.devnull, "r+") as null:
                        for stream in (0, 1, 2):
                            os.dup2(null.fileno(), stream)
                    replacement = ticket.with_suffix(".tmp")
                    replacement.write_text(str(os.getpid()))
                    os.replace(replacement, ticket)
                    os.write(ready_write, b"1")
                    os.close(ready_write)
                    detached = True
                # A stopped server cannot consume this update. Do not leave retries
                # behind, or let an old event reach a new server on the same socket.
                current = subprocess.check_output(
                    ["tmux", "display-message", "-p", "#{socket_path}|#{pid}"],
                    text=True, timeout=1,
                ).strip()
                if current != server:
                    return 0
                deadline = time.monotonic() + WAIT_SECONDS
            time.sleep(0.05)
    finally:
        ticket.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
        print(f"tmux-agent-indicator: cannot acquire state lock: {error}", file=sys.stderr)
        sys.exit(1)
