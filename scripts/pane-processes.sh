#!/usr/bin/env bash
# Commands attached to a pane's tty and descended from its starting process.
# Linux hosts can see identical pts/N names from multiple container devpts mounts.
set -euo pipefail

pane_pid="${1:-}"
pane_tty="${2:-}"
case "$pane_pid" in ''|*[!0-9]*) exit 0 ;; esac
[ -n "$pane_tty" ] || exit 0

ps -t "${pane_tty#/dev/}" -o pid=,ppid=,command= 2>/dev/null | awk -v root="$pane_pid" '
    {
        pid = $1
        parent[pid] = $2
        order[++count] = pid
        sub(/^[[:space:]]*[0-9]+[[:space:]]+[0-9]+[[:space:]]+/, "")
        command[pid] = $0
    }
    END {
        for (i = 1; i <= count; i++) {
            pid = order[i]
            for (depth = 0; depth < 100 && pid > 1; depth++) {
                if (pid == root) {
                    print command[order[i]]
                    break
                }
                if (!(pid in parent) || parent[pid] == pid) break
                pid = parent[pid]
            }
        }
    }
' || true
