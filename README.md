# dotfiles

Personal config for niri, kitty, fuzzel, swaylock, quickshell, zed, huginn, and vim.

## Setup on a new machine

```sh
git clone --recurse-submodules <repo-url> ~/dotfiles
~/dotfiles/make_symlinks.sh
```

`make_symlinks.sh` symlinks `vim/vimrc` to `~/.vimrc`, prompts for desktop vs
laptop to pick the right niri output config, then symlinks everything else
into `~/.config/`.

Submodules: `niri`, `kitty`, `fuzzel`, `swaylock`, `quickshell`, `zed`, `vim`.
Run `git submodule update --init --recursive` if they're empty after clone.

## Vim / LSP setup

`vim/vimrc` sources `options.vim`, `keybinds.vim`, `plugins.vim`, `colors.vim`,
`fzf.vim`, `lsp.vim`. Plugins auto-clone on first launch (see `plugins.vim`)
into `~/vim/plugged` — no plugin manager install step needed, just open vim.

LSP is handled by `yegappan/lsp`, configured in `vim/lsp.vim`. It expects
these binaries on `PATH`:

| Filetype | Server | Package / install |
|---|---|---|
| rust | `rust-analyzer` | via `rustup component add rust-analyzer` |
| rust (lint+format on save) | `rustfmt`, `clippy-driver` (via rust-analyzer, `check.command = clippy`) | via `rustup component add rustfmt clippy` |
| python | `pyright-langserver` (hover/completion/defs; its own diagnostics disabled) | pacman: `pyright` |
| python (lint+format on save) | `ruff` (`ruff server`) | pacman: `ruff` |
| html | `vscode-html-language-server` | npm: `vscode-langservers-extracted` |
| sql | `sqls` | `go install github.com/sqls-server/sqls@latest` (needs `go`, pacman: `go`) |

Install everything in one shot:

```sh
sudo pacman -S --needed pyright ruff go rustup
rustup component add rust-analyzer rustfmt clippy
npm install -g --prefix ~/.local vscode-langservers-extracted
go install github.com/sqls-server/sqls@latest
```

PATH must include `~/.cargo/bin`, `~/.local/bin`, and `~/go/bin` (already set
in `~/.bashrc` on this machine — check that a new machine's `~/.bashrc` has
the same three `export PATH=...` lines).

Note: `sql-language-server` (the npm-based SQL LSP) is broken on Node 22+
(`ERR_PACKAGE_PATH_NOT_EXPORTED`, unmaintained upstream) — use `sqls` instead.

### sqls database config (per machine, not in this repo)

`sqls` does **not** auto-discover a project-local config. It only reads, in
priority order: an explicit `-config` flag, an LSP `workspace/configuration`
push, or a single **global** file at `~/.config/sqls/config.yml`. Add one
connection per project there:

```yaml
lowercaseKeywords: false
connections:
  - alias: some-project
    driver: sqlite3   # or mysql, postgresql, mssql, h2
    dataSourceName: "/absolute/path/to/db.sqlite3"
```

Use an absolute `dataSourceName` — relative paths resolve against whatever
cwd the LSP server was started with, which isn't guaranteed to be the
project root. `sqls` supports multiple connections in that one file and an
LSP `switchConnections`/`switchDatabase` command to flip between them at
runtime.

### Plugin freshness

`plugins.vim` clones each plugin once with `git clone --depth=1` and never
updates it — there's no plugin manager doing that for you. If something in
`yegappan/lsp` looks broken, check upstream first:

```sh
cd ~/vim/plugged/lsp && git fetch origin main && git log HEAD..origin/main --oneline
```

If there are relevant fixes, `git reset --hard origin/main` (the clone has no
local edits, so this is a safe update, same as any plugin manager would do).

Per-filetype indent overrides live in `vim/after/ftplugin/` (2-space for
yaml/json/js/ts/jsx/tsx/html/css/scss/lua/sh/md/toml; global default in
`options.vim` is 4-space, used for rust/python/etc). This only works because
`options.vim` adds `~/dotfiles/vim` and `~/dotfiles/vim/after` to
`runtimepath` — both entries are required, not just the parent dir.
`options.vim` also forces `encoding=utf-8`, since some terminals/locales
default vim to `latin1`, which corrupts the UTF-8 diagnostic sign glyphs.

Format-on-save is wired for `.rs` and `.py` (`autocmd BufWritePre ... LspFormat`
in `lsp.vim`). Manual format: `<leader>f`.
