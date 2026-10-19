# Bash tab completion for ssh-reconnect: the Host entries of ~/.ssh/config (see ssh-hosts.sh).
# Sourced from the Bash startup files by install.sh, interactive Bash only. No process substitution
# or other syntax a POSIX-mode Bash rejects, in case a startup file reaches it anyway.
_ssh_reconnect_complete() {
    local cur=${COMP_WORDS[COMP_CWORD]} host
    COMPREPLY=()
    while IFS= read -r host; do
        [ -n "$host" ] && COMPREPLY+=("$host")
    done <<EOF_HOSTS
$(compgen -W "$("${BASH_SOURCE[0]%/*}/ssh-hosts.sh")" -- "$cur")
EOF_HOSTS
}
complete -F _ssh_reconnect_complete ssh-reconnect
