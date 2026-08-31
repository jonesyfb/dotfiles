# Launcher entries

`.desktop` files in this directory are symlinked into
`~/.local/share/applications/` by `make_symlinks.sh`, one file at a time.

They live here rather than in a submodule because fuzzel reads the XDG
applications dirs, not `~/.config` — so the repo-wide "link each top-level
dir into `~/.config/<name>`" rule does not apply. `applications` is in that
loop's skip list for the same reason.

## Writing one

`Exec=` does not expand `~`, so it needs an absolute path. Point at the
`~/.config` symlink rather than `~/dotfiles` — both resolve to the same
file, and the former keeps working if the repo is ever cloned elsewhere:

```desktop
[Desktop Entry]
Type=Application
Name=Symbol Picker
Comment=Insert Icelandic / Norse characters
Exec=/home/nate/.config/niri/scripts/symbol-picker.sh
Icon=accessories-character-map
Terminal=false
Categories=Utility;
```

A script launched this way inherits no terminal, so anything it prints goes
nowhere — have it talk to the user through fuzzel, notifications, or the
clipboard.

## Gotcha

`symbol-picker.sh` calls `fuzzel --dmenu` itself, so launching it *from*
fuzzel starts a second, separate fuzzel. That works, but it also means the
entry depends on `fuzzel`, `wl-clipboard` (`wl-copy`) and `ydotool` — and
`ydotool` needs `ydotoold` running or the typing step silently does nothing.
