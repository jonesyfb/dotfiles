#!/bin/bash
# Watches for any running Steam/Proton game and auto-toggles Huginn game mode.
#
# Detection is two patterns, either one sufficient:
#   1. steamapps/common/**/*.exe — the Linux-side launcher/wrapper chain
#      (reaper, proton's waitforexitandrun, SteamLinuxRuntime's bwrap) that
#      Steam keeps alive for the entire play session and which still
#      carries the real Linux path with forward slashes.
#   2. *-Win64-Shipping.exe / *-Win32-Shipping.exe — Unreal Engine's
#      standard name for the actual GPU-heavy game process. Confirmed live
#      against a running Marvel Rivals session (2026-09-05): the real game
#      process is Marvel-Win64-Shipping.exe, invoked by wine with a
#      backslash Windows-style path (Z:\home\nate\...), which pattern 1
#      alone does NOT match — only the wrapper chain does. Pattern 2 exists
#      so detection still works even if that wrapper chain ever exits
#      early, and it generalizes to any other Unreal-based Proton game,
#      not just this one.
# Neither is a per-title whitelist — this catches every Windows game Steam
# runs (Overwatch.exe, Marvel.exe/Marvel-Win64-Shipping.exe, whatever's
# installed next) without an entry added per game. The old Overwatch-only
# pgrep pattern never matched Marvel Rivals at all, so game mode never
# engaged and Ollama kept running unrestricted on GPU during it — the
# desktop crashed as a result (see project_ollama_crashes memory). Neither
# pattern covers native Linux game binaries (no .exe), which have no
# generic signature to match on.
FLAG="$HOME/.local/share/huginn/game-mode"
mkdir -p "$(dirname "$FLAG")"

# Best-effort: force every currently-loaded Ollama model off the GPU right
# now. num_gpu:0 on a live request only takes effect on the model's *next*
# load — a model already GPU-resident when the game launched would otherwise
# keep sitting on VRAM, unaffected, for the rest of the session. Unloading
# here (keep_alive:0) guarantees the next load — whenever Huginn's
# personality tier next renders — picks up num_gpu:0 fresh instead of
# reusing a GPU-loaded instance.
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

# Reconcile with reality on startup rather than blindly trusting a leftover
# flag: FLAG is a persistent file that survives reboots/crashes, but
# was_gaming always started at 0 regardless — if the flag was left set from
# a real session before a reboot (or a crash of this service) and no game is
# actually running now, the old logic only ever clears FLAG on a witnessed
# 1->0 transition, which this process never saw. Result: game mode stuck on
# indefinitely after any reboot that happens mid-session or shortly after.
if pgrep -fi "/steamapps/common/.*\.exe|-win(32|64)-shipping\.exe" > /dev/null 2>&1; then
    was_gaming=1
    touch "$FLAG"
else
    was_gaming=0
    rm -f "$FLAG"
fi

while true; do
    if pgrep -fi "/steamapps/common/.*\.exe|-win(32|64)-shipping\.exe" > /dev/null 2>&1; then
        if [ "$was_gaming" -eq 0 ]; then
            touch "$FLAG"
            unload_all_ollama_models
            huginn-notify --type info --title "ᚹ Huginn" --body "Game detected — standing down, GPU cleared." 2>/dev/null || true
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
