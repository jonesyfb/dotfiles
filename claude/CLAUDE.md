# Agent delegation via herdr

Nate manages Claude Code (and other agents) through `herdr`, a terminal
multiplexer with a socket API (`herdr agent ...`, `herdr pane ...`,
`herdr wait ...`). The `codex` integration is already installed
(`herdr integration status` shows `codex: current`), so Codex CLI panes
can be spun up and controlled the same way Claude Code panes are.

Goal: Claude session quota is the scarce resource; Codex is not. Spend
Claude on judgment and push everything else to Codex. Nate runs Claude in
many projects at once, all drawing the same quota — assume the budget is
already under pressure before this session started.

## The criterion that actually matters

Not task type — **fan-out ratio**. How much material must enter context
to produce the answer, versus how big the answer is?

Delegate anything where a lot goes in and a little comes out. Codex can
read forty files and hand back six lines; those forty files never touch
Claude's context. That is the whole saving. A task is worth delegating
even when Claude could do it easily, and even when explaining it to Codex
takes a couple of minutes.

High fan-out — delegate by default:
- Locating code, tracing callers, "where does X get set"
- Reading any file over ~300 lines to answer a narrow question
- Any repo-wide `grep`/`find` whose raw output is more than ~30 lines
- Test/build/lint runs — delegate the run, get back the failure list
- Stack traces and log triage — get back the suspect frames, not the log
- Research (library behavior, API docs, comparing approaches)
- Writing tests for an existing, well-specified function
- First-pass code review / diff review
- Mechanical multi-file edits once the pattern is pinned down (same field
  rename across N templates, same import swap across N call sites) — hand
  over the exact mapping + file list as one batch, review the diff after

Keep on Claude:
- Architecture / design decisions
- Multi-file changes where the *pattern itself* still needs judgment (how
  to split a checkout flow, a guard rule's semantics, an FK migration's
  shape) — deciding the pattern, not applying it
- Ambiguous or hard-to-reproduce bugs
- Final review/integration of anything Codex produced

Rule of thumb: writing a third near-identical edit for one fully-specified
pattern means stop and batch the rest to Codex. Same for a second big file
read chasing one fact.

## Keep a Codex pane per project

The policy fails on friction, not on rules. Cold-starting a pane mid-task
always looks more expensive than just doing the work inline, so the work
stays on Claude and the quota drains. Fix it by having the pane ready
before it is needed.

At the start of any session with real work in it, check `herdr agent list`
for a `codex` agent in this `cwd`. If there is none, start one immediately
— before the first delegable task, not when one shows up.

Reuse an idle Codex pane in the right `cwd` rather than starting another.

## How to delegate

1. Find or start the pane:
   `herdr agent list` — check for a `codex` agent with a matching `cwd`

   **Pin the placement, or it lands somewhere else.** With no `--tab`,
   `agent start` picks its own spot and the pane can open on a different
   tab or workspace than the Claude that spawned it. Read the current
   ids first and pass them through:

   ```sh
   eval "$(herdr pane current --current | jq -r '.result.pane |
     "TAB=\(.tab_id) WS=\(.workspace_id) CWD=\(.cwd)"')"
   herdr agent start codex --cwd "$CWD" --workspace "$WS" --tab "$TAB" \
     --split right --no-focus -- codex
   ```

   `--no-focus` keeps the split from stealing focus mid-task. If it boots
   into a hooks/info screen instead of the prompt, dismiss it:
   `herdr pane send-keys <pane_id> Escape`

   **Then rename it, always** — not just when running several at once:

   ```sh
   herdr agent rename <pane_id> codex-<project>
   ```

   With two or more Codex panes alive, the bare target `codex` stops
   resolving and every command fails with `agent_target_ambiguous`.
   Since the rule above is one pane per project, that state is the norm,
   not the exception. Target the unique name (or the `pane_id`) from here
   on — never bare `codex`.
2. Hand it the task. `herdr pane run <pane_id> "<task>"` sends the text
   *and* Enter in one call — prefer it. The `agent send` path types
   without submitting, so it needs an explicit Enter and is easy to get
   wrong:
   `herdr agent send codex-<project> "<task>"` + `herdr pane send-keys <pane_id> Enter`
3. Confirm it started, then block until done:
   `herdr agent wait codex-<project> --status working --timeout 15000`
   `herdr agent wait codex-<project> --status idle --timeout 180000`
4. Pull the result — **ask for the smallest read that answers it**.
   `herdr agent read codex-<project> --lines 40` is usually plenty; `--lines 200`
   pulls a wall of reasoning into context and undoes the saving.
5. Review the output before treating it as fact — Codex's result is an
   input to your judgment, not a substitute for it.

Long or independent jobs can run in parallel: start several panes, fire
them all off, then `wait` on each in turn — the per-pane names from step 1
are what keep them addressable. For work touching the same files,
`herdr worktree create --branch <name>` gives a pane its own checkout so
the edits cannot collide.

## Prompting Codex

Self-contained always — Codex has no memory of this conversation. Exact
file paths, function names, the specific question. Never "based on what
we discussed."

Ask for compact answers, or the fan-out saving leaks back out in the read:
- "Answer in under 15 lines. No code blocks."
- "Return `file:line` references only, not the surrounding code."
- "List the failing test names and the assertion that failed. Nothing else."
- "Report what you changed as a one-line summary per file."

Verified working 2026-08-18 against the dotfiles repo: pinned `agent start`
→ `agent rename` → `pane run` → `agent wait` → `agent read --lines 40`.
The pane opened in the calling tab, `pane run` submitted with no separate
Enter, and a compact-output prompt came back as a single line.

Retrospective 2026-08-17 (cts storefront-API migration): hand-edited one
field rename across 6 templates and one import swap across 8 call sites,
one Edit at a time, instead of batching either to Codex. Design work in
that same session was correctly kept on Claude.

Observed 2026-08-18: 7 Claude panes live across separate projects against
1 Codex pane total. That ratio is the quota problem — invert it. That lone
Codex pane also sat on a different tab from its Claude (`w3:t2` vs
`w3:t1`), from an `agent start` with no `--tab` — hence the pinning above.

## Employer code: cleared

Nate confirmed on 2026-08-17 that delegating FullBoreStudios work to
Codex is allowed. No need to ask again per-repo or per-session — treat
these repos the same as personal ones when deciding what to delegate.

Still don't paste, whatever the repo:

- Secrets — `.env` contents, API tokens, passwords, private keys.
- Customer or employee personal data, and database dumps containing it.

None of that is needed to answer a code question, and a prompt leaves the
machine. Point Codex at file paths and let it read the repo itself.
