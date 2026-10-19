#!/bin/sh
# ssh-hosts.sh — the Host entries of ~/.ssh/config, one per line, in file order, without
# duplicates. Patterns (*, ?, !) are skipped per entry, not per line: "Host colossus *.example.org"
# yields colossus. Used by the ssh-reconnect tab completions (Bash and Zsh).
awk '
    {
        sub(/^[ \t]+/, "")
        if (tolower(substr($0, 1, 4)) != "host") next
        rest = substr($0, 5)
        if (rest ~ /^[ \t]*=/) sub(/^[ \t]*=/, " ", rest)   # Host=name form
        else if (rest !~ /^[ \t]/) next                       # Hostname, HostKeyAlgorithms, ...
        n = split(rest, field, /[ \t]+/)
        for (i = 1; i <= n; i++) {
            if (field[i] == "") continue
            if (field[i] ~ /^#/) break
            if (field[i] ~ /[*?!]/) continue
            if (!seen[field[i]]++) print field[i]
        }
    }' "${1:-$HOME/.ssh/config}" 2>/dev/null
exit 0
