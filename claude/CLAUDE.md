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

## Keep Codex panes per project

The policy fails on friction, not on rules. Cold-starting a pane mid-task
always looks more expensive than doing the work inline, so the work stays
on Claude and the quota drains. Fix it by having panes ready before they
are needed.

At the start of any session with real work in it, check `herdr agent list`
for a `codex` agent in this `cwd`. If there is none, start one immediately
— before the first delegable task, not when one shows up. Add more panes
whenever a plan has independent tracks; concurrency needs one pane per
concurrent task.

**Name it at start, uniquely.** Names are global across every project, so
`codex` alone is unusable: `agent start codex` fails with
`agent_name_taken` once any pane holds it, and targeting bare `codex`
fails with `agent_target_ambiguous` once two exist. Use
`codex-<project>`, plus `-a`/`-b` suffixes for fan-out panes.

```sh
eval "$(herdr pane current --current | jq -r '.result.pane |
  "TAB=\(.tab_id) WS=\(.workspace_id) CWD=\(.cwd)"')"
herdr agent start codex-<project> --cwd "$CWD" --workspace "$WS" \
  --tab "$TAB" --split right --no-focus -- codex
```

Pin `--workspace`/`--tab` or the pane opens on some other tab than the
Claude that spawned it. `--no-focus` stops it stealing focus mid-task. If
it boots into a hooks/info screen, dismiss it with
`herdr pane send-keys <pane_id> Escape`.

## Plan the split before running anything

Concurrency is a planning decision, not an execution one. By the time a
task is in flight it is too late to parallelise it, so decomposition has
to name the independent tracks up front.

When a plan is drawn up, before any of it runs:

1. Mark every step as Codex-work or Claude-work by the fan-out rule above.
2. Group the Codex steps into tracks that do not depend on each other's
   output. Anything reading different files, or answering different
   questions, is almost always independent.
3. Give each track its own pane and start them all at once.

Steps that only *look* sequential usually are not: "find the callers",
"check how the API behaves", and "see which tests cover this" are three
independent reads, not a chain. A dependency exists only when one task
needs another's actual output as input.

## Run the batch — fan out, then join

Send every independent task first, then collect. Never send → wait →
send → wait; that serialises work that had no reason to be serial, and
total time becomes the sum instead of the longest single task.

```sh
resolve() { herdr agent list | jq -r --arg n "$1" \
  '.result.agents[] | select(.name==$n) | .pane_id'; }

herdr pane run "$(resolve codex-proj-a)" "<task A>"    # fan out
herdr pane run "$(resolve codex-proj-b)" "<task B>"

herdr agent wait codex-proj-a --status working --timeout 20000  # join
herdr agent wait codex-proj-b --status working --timeout 20000
herdr agent wait codex-proj-a --status idle --timeout 180000
herdr agent wait codex-proj-b --status idle --timeout 180000
```

Details that bite:

- **Resolve pane ids by name at send time.** `pane run` needs a pane id,
  but ids are not durable — panes get renumbered and can disappear
  outright. The agent name is the stable handle.
- **Never redirect `pane run` to `/dev/null`.** A dead pane id fails
  silently and the task simply never runs.
- **Wait for `working` before waiting for `idle`.** A pane that has not
  picked the task up yet is still `idle`, so an immediate idle-wait
  returns instantly and the read comes back empty.
- **Do not idle during the join.** Codex running is exactly the window
  for Claude-only work — design decisions, reviewing what an earlier
  track returned. Block only when nothing else is left.
- **Reads stay small.** `herdr agent read <name> --lines 40` is usually
  plenty; `--lines 200` pulls a wall of reasoning back into context and
  undoes the saving.
- Review every result before treating it as fact. Codex's output is an
  input to your judgment, not a substitute for it.

For tracks that *write* to the same files, give each its own checkout
with `herdr worktree create --branch <name>` so the edits cannot collide.
Read-only tracks can share a repo freely.

## Prompting Codex

Self-contained always — Codex has no memory of this conversation. Exact
file paths, function names, the specific question. Never "based on what
we discussed."

Ask for compact answers, or the fan-out saving leaks back out in the read:
- "Answer in under 15 lines. No code blocks."
- "Return `file:line` references only, not the surrounding code."
- "List the failing test names and the assertion that failed. Nothing else."
- "Report what you changed as a one-line summary per file."

Verified working 2026-08-18 against the dotfiles repo: two panes pinned to
the calling tab, both fired with `pane run`, both `working` by t+1s and
both `idle` at t+6s — one task alone had taken 7s, so the batch cost the
longest task, not the sum. Also observed live: a pane vanished mid-session
and its stale id failed silently under `/dev/null`.

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
