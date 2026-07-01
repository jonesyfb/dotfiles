#!/usr/bin/env zsh
# Source this in ~/.zshrc to enable Huginn shell chimes:
#   source ~/dotfiles/huginn/scripts/huginn-zsh.sh

_huginn_send="python3 /home/nate/dotfiles/huginn/backend/huginn_send.py"

_huginn_preexec() {
    _huginn_cmd="$1"
    _huginn_t0=$SECONDS
}

_huginn_precmd() {
    local exit_code=$?
    local cmd="$_huginn_cmd"
    local elapsed=$(( SECONDS - ${_huginn_t0:-$SECONDS} ))
    _huginn_cmd=""
    _huginn_t0=""

    [[ -z "$cmd" ]] && return

    if (( exit_code != 0 || elapsed >= 30 )); then
        $_huginn_send bash_event "$exit_code" "$elapsed" "$cmd" &>/dev/null &
    fi
}

autoload -Uz add-zsh-hook
add-zsh-hook preexec _huginn_preexec
add-zsh-hook precmd  _huginn_precmd
