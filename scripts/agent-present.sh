#!/usr/bin/env bash
# Batch presence detection outside the state-writer lock, then reconcile once.
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/opt/homebrew/bin:$PATH"
exec python3 "$script_dir/agent-present.py" "$@"
