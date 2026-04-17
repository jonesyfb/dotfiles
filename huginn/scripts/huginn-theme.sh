#!/usr/bin/env bash
# Switch the Huginn theme across all dotfiles configs.
# Usage: huginn-theme <midnight|obsidian|ember|verdant>
set -e
uv run --project ~/dotfiles/huginn python3 ~/dotfiles/huginn/backend/theme.py "${1:-midnight}"
