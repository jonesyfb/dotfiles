#!/usr/bin/env bash
# Source this in ~/.bashrc to enable Huginn shell integration:
#   source /path/to/huginn-bash.sh

_huginn_sock="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/huginn.sock"
_huginn_send="/home/nate/dotfiles/huginn/backend/huginn_send.py"

_huginn_preexec() {
    _huginn_cmd="$BASH_COMMAND"
    _huginn_t0="$SECONDS"
}

_huginn_precmd() {
    local exit_code=$?
    local cmd="$_huginn_cmd"
    local elapsed=$(( SECONDS - ${_huginn_t0:-SECONDS} ))
    _huginn_cmd=""
    _huginn_t0=""

    # Only fire on failure or long-running (>=30s), skip internal PROMPT_COMMAND calls
    [[ -z "$cmd" || "$cmd" == _huginn_* ]] && return

    if (( exit_code != 0 || elapsed >= 30 )); then
        python3 "$_huginn_send" bash_event \
            "$exit_code" "$elapsed" "$cmd" &>/dev/null &
    fi
}

trap '_huginn_preexec' DEBUG
# Append to PROMPT_COMMAND safely
if [[ -z "$PROMPT_COMMAND" ]]; then
    PROMPT_COMMAND="_huginn_precmd"
elif [[ "$PROMPT_COMMAND" != *_huginn_precmd* ]]; then
    PROMPT_COMMAND="${PROMPT_COMMAND%;};_huginn_precmd"
fi
