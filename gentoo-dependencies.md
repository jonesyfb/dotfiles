# Runtime dependencies these dotfiles assume

Things the configs/scripts call by name that won't exist on a fresh Gentoo
install until emerged. `make_symlinks.sh` only symlinks config files — it
doesn't install any of this.

## Binaries niri/fuzzel/huginn spawn or shell out to

- `brave`, `kitty`, `fuzzel` — launched directly from niri keybinds
- `brightnessctl`, `playerctl` — niri media/brightness binds
- `grim` — huginn's screenshot capture (`v2/capture.py`)
- `ollama` — huginn's LLM backend, must be running with these models pulled:
  `qwen3.5:9b`, `qwen3.5:4b`, `qwen3.5:27b`, `qwen3.8:27b`
- `uv` — runs huginn itself (`huginn.service` ExecStart is `uv run --project ...`)
- `curl`, `pgrep` — used by `huginn-gamemode.sh`/`huginn-gamewatch.sh`

## Fonts / themes

- `JetBrainsMono Nerd Font Mono` — fuzzel's configured font
- `Papirus-Dark` icon theme — fuzzel's `icon-theme=`

## Not handled by huginn/install.sh

`install.sh` only sets up the core `huginn.service`. These systemd user
units need manual symlink+enable, same pattern as install.sh's core one:
`huginn-gamemode.service`, `huginn-gamewatch.service`, `huginn-morning.service`
(all in `huginn/systemd/`).

## vim

Plugin manager (`vim/plugins.vim`) shells out to `git clone` directly — no
plugin manager package needed, just `git` on PATH.

## Quickshell

Needs Qt6 + the USE flags: wayland, layer-shell, session-lock,
toplevel-management, screencopy, tray, mpris, notifications (see the
2026-09-24 Gentoo package research — GURU overlay, `gui-apps/quickshell`).

## Package lists

Full explicit-install and AUR package lists snapshotted from the Arch
install are on the backup drive (`gentoo-prep/pkglist-explicit.txt`,
`gentoo-prep/pkglist-aur.txt`), not in this repo — AUR packages especially
don't map 1:1 to Gentoo/GURU package names and need manual translation.
