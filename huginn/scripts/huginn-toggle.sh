#!/usr/bin/env bash
# Toggle the Huginn overlay. Bind this to a hotkey in niri.
# e.g. in niri config: bind "Super+Space" { spawn "bash" "-c" "~/.local/bin/huginn-toggle.sh"; }

FLAG=/tmp/huginn-visible

if [ -f "$FLAG" ]; then
    rm -f "$FLAG"
else
    touch "$FLAG"
fi
