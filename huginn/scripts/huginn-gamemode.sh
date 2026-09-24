#!/bin/bash
# Toggle game mode — pauses Huginn LLM and Garage Watch scoring
FLAG="$HOME/.local/share/huginn/game-mode"
mkdir -p "$(dirname "$FLAG")"

# See huginn-gamewatch.sh: num_gpu:0 only takes effect on a model's next
# load, so a model already GPU-resident needs unloading now, not just
# gated going forward.
unload_all_ollama_models() {
    curl -s --max-time 3 http://localhost:11434/api/ps 2>/dev/null \
        | python3 -c '
import json, sys
try:
    for m in json.load(sys.stdin).get("models", []):
        print(m.get("name") or m.get("model") or "")
except Exception:
    pass
' 2>/dev/null | while read -r model; do
        [ -z "$model" ] && continue
        curl -s --max-time 3 -X POST http://localhost:11434/api/generate \
            -d "{\"model\": \"$model\", \"keep_alive\": 0}" > /dev/null 2>&1
    done
}

if [ -f "$FLAG" ]; then
    rm "$FLAG"
    huginn-notify --type info --title "ᚹ Huginn" --body "Game mode off — back on duty." 2>/dev/null || true
    echo "Game mode: OFF"
else
    touch "$FLAG"
    unload_all_ollama_models
    huginn-notify --type info --title "ᚹ Huginn" --body "Game mode — standing down, GPU cleared." 2>/dev/null || true
    echo "Game mode: ON"
fi
