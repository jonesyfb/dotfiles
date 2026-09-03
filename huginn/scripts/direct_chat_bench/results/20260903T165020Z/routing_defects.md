# Routing defects observed in this report (not fixed here)

Pulled from `report.md` in this same results directory. Listed only, per
explicit instruction — no `intent.py`/`daemon.py`/`llm.py` changes made
alongside this list.

1. **False action match: "I've got that browser open."**
   Line 115. Classified `tool_or_action` (`tool_action_keyword` — the bare
   keyword "open" fires even though the sentence states a fact, not a
   request), which produced the hollow no-tool-call fallback: `"No command
   ran — tell me exactly what you'd like done."` The sentence should read
   as a casual mention (SOCIAL_DIRECT / ENTITY_OPINION), not an action
   request.

2. **"Night owl, huh?" falls to the same action fallback.**
   Line 104. Classified `ambiguous` (`no_confident_match`), also produced
   `"No command ran — tell me exactly what you'd like done."` — a message
   with zero action content getting an action-flavored non-answer is a
   worse failure mode than a plain "not sure what you mean."

3. **Bare "Docker." overconfidently classified as ENTITY_OPINION.**
   Line 66: `'Docker.'` -> `social_direct`/`entity_opinion`, answered as if
   asked for an opinion ("Docker is like an octopus..."). A single bare
   noun with no verb, question, or opinion cue is being treated with the
   same confidence as "What do you think of Docker?" — there's no signal
   in the input distinguishing a bare mention from a request for a take.

4. **"What's up with Docker?" chose `search_memory` over system inspection.**
   Line 103: `factual_or_reasoning` (`entity_status_question`) correctly
   left the tool-calling path open (per this round's item 8 fix), but the
   model chose `search_memory {'query': 'Docker containers'}` — a
   fact-recall tool — instead of an inspection tool (`shell` running
   `docker ps`/`systemctl status docker`), then asked a non-answer
   follow-up ("How's the container orchestration?"). Compare this same
   input in a clean-history run in `report.md`'s live daemon check, which
   *did* pick `docker ps` — the tool choice is inconsistent across runs,
   not wrong in a single fixed way. The classifier hands off correctly;
   what happens after handoff (tool selection) is unconstrained.

5. **"Close Docker." expanded scope to disable-on-boot.**
   Line 65: `confirm_required` proposed
   `sudo systemctl stop docker && sudo systemctl disable docker` — Nathan
   asked to close/stop Docker, not to disable it from starting on the next
   boot. The confirm gate means nothing destructive ran without approval,
   but the *proposed* action already overshoots the request before Nathan
   even sees the confirm prompt.

6. **Factual responses introducing unapproved productivity criticism.**
   Line 17: `'Why is my computer stuttering?'` (`factual_or_reasoning`) got
   `'[tool_call: system_stats {}]Why did you open that game instead of
   reading the manual?'` — a real tool call followed by a jab at Nathan's
   choices that has nothing to do with disk/CPU diagnosis and no
   authorization. `personality.py`'s procrastination-boundary validators
   only run on the SOCIAL_DIRECT path (`render_direct_social`); the
   ordinary `route_model()`/`stream_chat()` path used for
   FACTUAL_OR_REASONING/TOOL_OR_ACTION has no equivalent check (only the
   general `SYSTEM_PROMPT`'s new one-line instruction from the prior
   commit, which is advisory, not enforced).

Common thread: items 1-3 are intent-classifier precision gaps (routing
too eagerly on weak signals); items 4-6 are gaps in what happens *after*
correct routing (tool selection and the general chat path have no
validators at all, unlike SOCIAL_DIRECT). None of these were touched by
either commit in this session — flagged for a future focused pass.
