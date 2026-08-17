# Agent delegation via herdr

Nate manages Claude Code (and other agents) through `herdr`, a terminal
multiplexer with a socket API (`herdr agent ...`, `herdr pane ...`,
`herdr wait ...`). The `codex` integration is already installed
(`herdr integration status` shows `codex: current`), so Codex CLI panes
can be spun up and controlled the same way Claude Code panes are.

Goal: delegate self-contained, low-judgment work to Codex to conserve
Claude session usage, and reserve Claude for architecture, ambiguous
bugs, and final review.

## When to delegate to Codex

Delegate:
- Locating code ("where is X implemented", "find all callers of Y")
- Writing tests for an existing, well-specified function
- First-pass code review / diff review
- Research (library behavior, API docs, comparing approaches)
- Any task whose result is easy for Claude to verify at a glance

Keep on Claude:
- Architecture / design decisions
- Multi-file refactors requiring judgment across the change
- Ambiguous or hard-to-reproduce bugs
- Final review/integration of anything Codex produced

## How to delegate

1. Start (or reuse) a Codex pane:
   `herdr agent start codex --cwd <project_dir> --split right -- codex`
   If it boots into a hooks/info screen instead of the prompt, dismiss it:
   `herdr pane send-keys <pane_id> Escape`
2. Hand it the task. `agent send` only *types* the text — it does not
   submit it — so an explicit Enter is required:
   `herdr agent send codex "<precise, self-contained task description>"`
   `herdr pane send-keys <pane_id> Enter`
3. Confirm it started, then block until done:
   `herdr agent wait codex --status working --timeout 15000`
   `herdr agent wait codex --status idle --timeout 180000`
4. Pull the result:
   `herdr agent read codex --lines 200`
5. Review the output yourself before treating it as fact/final — Codex's
   result is an input to your judgment, not a substitute for it.

Verified working 2026-08-17: full start → send → Enter → wait → read
loop tested against the dotfiles repo. `agent send` not auto-submitting
is the one real gotcha — don't skip the Enter step.

Give Codex prompts that are self-contained (it has no memory of this
conversation) — include exact file paths, function names, and the
specific question, not "based on what we discussed."

## Employer code: cleared

Nate confirmed on 2026-08-17 that delegating FullBoreStudios work to
Codex is allowed. No need to ask again per-repo or per-session — treat
these repos the same as personal ones when deciding what to delegate.

Still don't paste, whatever the repo:

- Secrets — `.env` contents, API tokens, passwords, private keys.
- Customer or employee personal data, and database dumps containing it.

None of that is needed to answer a code question, and a prompt leaves the
machine. Point Codex at file paths and let it read the repo itself.
