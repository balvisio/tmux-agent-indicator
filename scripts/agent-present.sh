#!/usr/bin/env bash
# Batch presence detection outside the state-writer lock, then reconcile once.
set -euo pipefail
script_dir="${BASH_SOURCE[0]%/*}"
[ "$script_dir" != "${BASH_SOURCE[0]}" ] || script_dir=.
script_dir="$(cd "$script_dir" && pwd)"
export PATH="/opt/homebrew/bin:$PATH"
exec python3 "$script_dir/agent-present.py" "$@"
