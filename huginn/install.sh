#!/usr/bin/env bash
# Huginn Phase 0 install script

set -e

echo "Installing Huginn..."

# Sync Python deps via uv
uv sync --project "$(dirname "$0")"

# Create data dir
mkdir -p ~/.local/share/huginn

# Symlink scripts
mkdir -p ~/.local/bin
ln -sf ~/dotfiles/huginn/scripts/huginn-toggle.sh ~/.local/bin/huginn-toggle
ln -sf ~/dotfiles/huginn/scripts/huginn-theme.sh  ~/.local/bin/huginn-theme

# Install systemd user service
mkdir -p ~/.config/systemd/user
ln -sf ~/dotfiles/huginn/systemd/huginn.service ~/.config/systemd/user/huginn.service
systemctl --user daemon-reload
systemctl --user enable --now huginn.service

# Apply default theme (generates kitty + vim files)
uv run --project ~/dotfiles/huginn python3 ~/dotfiles/huginn/backend/theme.py midnight

echo ""
echo "Done. Add this keybind to your niri config:"
echo ""
echo '  Mod+Space { spawn "huginn-toggle"; }'
echo ""
echo "Huginn status: $(systemctl --user is-active huginn.service)"
