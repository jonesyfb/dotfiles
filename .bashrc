#!/bin/bash

# Dotfiles symlink script
# Usage: ./symlink-dotfiles.sh [dotfiles-directory]
# Default dotfiles directory is ~/dotfiles

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get dotfiles directory from argument or use default
DOTFILES_DIR="${1:-$HOME/dotfiles}"
CONFIG_DIR="$HOME/.config"

# Check if dotfiles directory exists
if [ ! -d "$DOTFILES_DIR" ]; then
    echo -e "${RED}Error: Dotfiles directory '$DOTFILES_DIR' not found!${NC}"
    echo "Usage: $0 [dotfiles-directory]"
    exit 1
fi

echo -e "${GREEN}=== Dotfiles Symlink Script ===${NC}"
echo "Dotfiles directory: $DOTFILES_DIR"
echo "Config directory: $CONFIG_DIR"
echo ""

# Create .config directory if it doesn't exist
mkdir -p "$CONFIG_DIR"

# Function to create symlink
create_symlink() {
    local source="$1"
    local target="$2"
    local name=$(basename "$source")
    
    # Check if target exists
    if [ -e "$target" ] || [ -L "$target" ]; then
        echo -e "${YELLOW}Removing existing: $target${NC}"
        rm -rf "$target"
    fi
    
    # Create symlink
    ln -sf "$source" "$target"
    echo -e "${GREEN}✓ Linked: $target -> $source${NC}"
}

# Special case: vim/vimrc -> ~/.vimrc
if [ -f "$DOTFILES_DIR/vim/vimrc" ]; then
    create_symlink "$DOTFILES_DIR/vim/vimrc" "$HOME/.vimrc"
elif [ -f "$DOTFILES_DIR/vim/.vimrc" ]; then
    create_symlink "$DOTFILES_DIR/vim/.vimrc" "$HOME/.vimrc"
fi

# Link all directories/files in dotfiles to .config
# Exclude common non-config items
for item in "$DOTFILES_DIR"/*; do
    # Skip if not a file or directory
    [ -e "$item" ] || continue
    
    name=$(basename "$item")
    
    # Skip certain items
    case "$name" in
        .git|.gitignore|README*|LICENSE*|*.md|vim)
            echo -e "${YELLOW}Skipping: $name${NC}"
            continue
            ;;
    esac
    
    # Create symlink in .config
    create_symlink "$item" "$CONFIG_DIR/$name"
done

echo ""
echo -e "${GREEN}=== Dotfiles symlinked successfully! ===${NC}"
