#!/usr/bin/env bash
# Huginn gate — asks the daemon whether a gated app is earned before launching it.
# Usage: huginn-gate-launch.sh <target> <real-exec> [args...]
set -euo pipefail

target="$1"; shift
real_exec="$1"; shift

send="/home/nate/dotfiles/huginn/backend/huginn_send.py"
notify="/home/nate/.local/bin/huginn-notify"  # absolute — desktop launchers don't inherit ~/.local/bin on $PATH

reply="$(python3 "$send" gate_check "$target" 2>/dev/null | grep -m1 '"type": *"gate_verdict"' || true)"

if [[ -z "$reply" ]]; then
    # Daemon unreachable — fail open so a dead daemon never locks you out entirely.
    "$notify" --type warn --title "ᚹ Huginn" --body "Gatekeeper unreachable — letting $target through." 2>/dev/null || true
    exec "$real_exec" "$@"
fi

approved="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('approved', False))" "$reply")"
message="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('message', ''))" "$reply")"

if [[ "$approved" == "True" ]]; then
    "$notify" --type ok --title "ᚹ Huginn" --body "$message" 2>/dev/null || true
    exec "$real_exec" "$@"
else
    "$notify" --type warn --title "ᚹ Huginn" --body "$message" 2>/dev/null || true
    exit 1
fi
