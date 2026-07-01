#!/bin/bash
# Toggle game mode — pauses Huginn LLM and Garage Watch scoring
FLAG="$HOME/.local/share/huginn/game-mode"
mkdir -p "$(dirname "$FLAG")"

if [ -f "$FLAG" ]; then
    rm "$FLAG"
    huginn-notify --type info --title "ᚹ Huginn" --body "Game mode off — back on duty." 2>/dev/null || true
    echo "Game mode: OFF"
else
    touch "$FLAG"
    huginn-notify --type info --title "ᚹ Huginn" --body "Game mode — standing down." 2>/dev/null || true
    echo "Game mode: ON"
fi
