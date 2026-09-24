# Delegating Implementation

Use OpenCode as the orchestrator. Delegate architecture-sensitive changes,
difficult implementation, and substantial code reviews in this order:

1. Prefer Claude Code through a project-scoped `herdr` pane that OpenCode starts
   for the current task. Never use or send input to a pre-existing pane that
   OpenCode did not start; it belongs to the user or another ongoing task.
2. If Claude is unavailable, quota-limited, fails, or times out, use OpenCode's
   `general` subagent, which runs `openai/gpt-6-astra` directly.

Do not use Codex through `herdr` by default. Reserve it for explicit user
requests or work that benefits from a persistent, visible, independently
running pane.

Do not use Claude for token-heavy but straightforward work such as broad file
searches, mechanical tracing, or high-volume summarization. Run that work
directly in OpenCode or use an appropriate OpenCode subagent. Reserve Claude
for writing or architecting code and reasoning where its higher cost is
justified.

Do not delegate routine commands such as formatting, linting, `cargo check`,
or simple test runs. Run those directly in OpenCode unless verification has
high input/output fan-out and a subagent can summarize a large result compactly.

Treat delegated output as an input to final engineering judgment. Review edits
and conclusions before integrating or reporting them.
