#!/bin/bash
# Watches for Overwatch and auto-toggles Huginn game mode
FLAG="$HOME/.local/share/huginn/game-mode"
mkdir -p "$(dirname "$FLAG")"

was_gaming=0

while true; do
    if pgrep -fi "overwatch" > /dev/null 2>&1; then
        if [ "$was_gaming" -eq 0 ]; then
            touch "$FLAG"
            huginn-notify --type info --title "ᚹ Huginn" --body "Overwatch detected — standing down." 2>/dev/null || true
            was_gaming=1
        fi
    else
        if [ "$was_gaming" -eq 1 ]; then
            rm -f "$FLAG"
            huginn-notify --type info --title "ᚹ Huginn" --body "Game over — back on duty." 2>/dev/null || true
            was_gaming=0
        fi
    fi
    sleep 10
done
