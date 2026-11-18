#!/usr/bin/env bash
# The native gate claims work atomically before launching its worker.
set -euo pipefail
case "${1:-}" in sweep|recovery) ;; *) exit 2 ;; esac
exec tmux run-shell -C "#{@agent-indicator-$1-launch}"
