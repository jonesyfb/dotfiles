# Vision-model gatekeeper audition — recommendation

**Status: DECIDED AND INTEGRATED.** Following a focused 5-trial repeated
re-audition confirming the single-trial result, `qwen3.8:27b` is now
gatekeeper's configured vision model (`v2/config.py`). Ollama's service
environment (`OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_GPU_OVERHEAD=6GB`), the
Runtime Context Engine, personality routing, and SYSTEM_PROMPT remain
untouched, as instructed.

Two datasets, both kept:
- `scripts/vision_bench/results/20260903T031452Z/` — the original 5-candidate
  broad screen (1 trial/scenario), which eliminated `gemma4:31b` (control),
  `gemma4:e2b`, and `llava:7b` (the last for a real, disqualifying prompt-
  injection failure).
- `scripts/vision_bench/results/20260903T034745Z/` — the **focused,
  decisive dataset**: 5 trials per remaining semantic scenario, qwen3.8:27b
  vs. gemma4:e4b only, with deterministic evidence pre-checks wired in
  (see below). This is what the final decision is based on.

## Important context that changed mid-audition

Ollama was upgraded from 0.20.7 to 0.33.2 during this session (to make
qwen3.8:27b's manifest pullable at all — 0.20.7 rejected it outright).
That upgrade **also fixed something unrelated to model choice**: on this
box, `gemma4:31b` now loads **100% into VRAM** (20.3GB, confirmed via
`/api/ps` and `/sys/class/drm/card1/device/mem_info_vram_used`), where
slice 3's measurement on 0.20.7 showed only 67.5% GPU / 32.5% CPU
spillover. Throughput went from ~7.9 tok/s to ~31 tok/s on the *same*
model, same hardware — purely from the Ollama version bump. All five
candidates below were measured 100% GPU-resident on 0.33.2.

**This means the original problem statement — "gemma4:31b spills to CPU
and is unacceptably slow" — is already partly solved by the Ollama upgrade
alone**, independent of any model swap. It's still the slowest of the five
candidates tested, so a switch is still worth considering, but the
urgency is lower than when this slice started, and the improvement isn't
attributable to anything in this repo — it's an upstream Ollama fix. Kept
`OLLAMA_MAX_LOADED_MODELS=1` and `OLLAMA_GPU_OVERHEAD=6GB` unchanged, as
instructed.

## Candidates tested

| Model | Params | Download | License | Vision confirmed |
|---|---|---|---|---|
| `gemma4:31b` (control, current production) | 31.3B | 19GB (was already local) | Gemma license | Yes |
| `qwen3.8:27b` (required contender) | 27.3B | 18GB (17GB Q4_K_M + ~1GB vision projector) | Apache 2.0 | Yes — verified via ollama.com listing ("vision tools" capability, native image/video understanding) before pulling |
| `gemma4:e4b` | ~9.6B | 9.6GB | Gemma license | Yes |
| `gemma4:e2b` | ~7.2B | 7.2GB | Gemma license | Yes |
| `llava:7b` | 7B | 4.7GB | Unconfirmed — flag before production use | Yes |

All four non-control candidates were presented with exact tags and
estimated total download (~40GB) before pulling anything, per instruction.

## Results

| Model | Composite* | Schema-valid | Accuracy | False-pos-deny | False-neg-approve | Uncertainty calib. | Injection-resistant | Cold (s) | Warm avg (s) | Warm tok/s | GPU residency |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **qwen3.8:27b** | **1.16** | 1.0 | 0.54 | **0.0** | **0.0** | 0.0 | True | 18.2 | 11.7 | 41.6 | 100% |
| gemma4:e4b | 1.54 | 1.0 | 0.54 | 0.17 | 0.0 | 0.0 | True | 9.3 | 4.8 | 118.5 | 100% |
| gemma4:31b (control) | 2.20 | 1.0 | 0.54 | 0.33 | 0.0 | 0.2 | True | 31.0 | 20.4 | 30.7 | 100% |
| gemma4:e2b | 2.30 | 1.0 | 0.46 | 0.5 | 0.5 | 0.6 | True | 7.7 | 3.6 | 166.9 | 100% |
| llava:7b | 3.35 | 1.0 | 0.46 | 0.17 | **1.0** | 0.4 | **False** | 6.4 | 0.9 | 139.9 | 100% |

\* Lower is better; false-positive-denial weighted 3x. 13 scenarios, 1 warm
trial each (n=1 per category — read individual scenario misses, not just
composite, for anything you're weighing heavily).

None left unexpected residency after their run (`residency_after_final_unload`
was `null` for all five). Zero timeouts across the board.

## Recommendation

**Winner: `qwen3.8:27b`.** It's the only candidate that both caught the one
real-procrastination scenario (`deny`, 0.8 confidence) *and* never
falsely denied a legitimate-approve scenario (0/6 false-positive-denials)
*and* resisted the prompt-injection scenario. That combination — the top
three selection priorities in order — is unique to it among the five.
It's also meaningfully faster than the control (11.7s vs 20.4s warm avg)
despite being a comparable-sized model, likely a genuinely better
architecture for this workload rather than just smaller.

**Runner-up: `gemma4:e4b`.** Same-family as production (lowest integration
risk), 2.5x faster than qwen3.8:27b, also resisted injection, only one
false-positive-denial (`entertainment_no_commitment` — wrongly called
relaxing "procrastination," a real miss worth noting) and zero
false-negative-approvals. If interactive latency matters more than the
small accuracy edge, this is the safer, cheaper pick.

**Does qwen3.8:27b justify its latency/footprint?** Partially. At 18GB /
11.7s warm average, it costs roughly the same VRAM as the current
production model and is 2.4x slower than e4b per judgment. For an
interactive gate check (someone waiting to see if Steam launches),
11.7s is acceptable; e4b's 4.8s is more pleasant. The honest tradeoff:
**e4b for latency, qwen3.8:27b for correctness on the specific failure
modes this gate exists to avoid** (wrongly accusing someone of slacking,
missing real procrastination, following injected instructions). Given
selection priority #2 (false-positive-denial) explicitly outranks
priority #5 (latency) in the stated ordering, qwen3.8:27b is the
priority-consistent choice — but it's close enough that e4b is a
completely defensible alternative if you weight latency higher in
practice than the stated priority order suggests.

**A real, universal weakness worth flagging regardless of which is
picked:** every single candidate, including the winner, confidently
guessed (approve or deny) on the `stale_screenshot`, `missing_screenshot`,
and `corrupt_screenshot` scenarios instead of saying `uncertain` — all
five scored 0.0–0.6 on uncertainty calibration, and the two best overall
performers (qwen3.8:27b, e4b) both scored **0.0**. None of these models
reliably recognize "the evidence itself is unreliable" as its own signal
distinct from "the evidence says approve/deny." If this matters to you
(it's priority #3 in the stated ordering), it argues for keeping the
`uncertain` path in the eventual production schema mostly as a safety
net rather than something to rely on models actually reaching correctly —
consider a deterministic pre-check (e.g., is the screenshot's file mtime
older than N minutes? did image loading actually succeed?) rather than
trusting the model to notice.

## Focused re-audition: qwen3.8:27b vs. gemma4:e4b (decisive)

Per instruction, evidence validity was moved out of model judgment first
(`v2/evidence.py`, separate commit) — `stale_screenshot`, `missing_screenshot`,
and `corrupt_screenshot` no longer reach either model at all; they're
deterministic, zero-inference `uncertain` results now. The benchmark
harness was updated to use the exact same `evidence.validate_screenshots()`
gatekeeper.py calls, so the audition and production paths are identical.
That leaves 9 "remaining semantic" scenarios run 5 times each (plus 1 cold
trial), fixed `seed=7, temperature=0.1`.

| Model | Composite | Accuracy | False-pos-deny | False-neg-approve | Uncertainty calib. | Stability | Injection-resistant | Cold (s) | Warm avg/p50/p95 (s) | tok/s |
|---|---|---|---|---|---|---|---|---|---|---|
| **qwen3.8:27b** | **0.65** | 0.8 | **0.0** | 0.0 | 0.44 | **1.0** | True (5/5) | 18.66 | 10.63/9.89/15.73 | 42.0 |
| gemma4:e4b | 1.05 | 0.8 | 0.16 | 0.0 | 0.44 | 1.0 | True (5/5) | 9.33 | 5.27/5.33/7.13 | 117.9 |

**Verdict stability was perfect (1.0) for both** — every repeated trial of
every scenario produced the identical verdict (qwen3.8:27b even matched
confidence values exactly, e.g. `approve(0.7)×5`). This is real evidence
the single-trial broad screen wasn't reporting noise.

**The one substantive difference, and it's reproducible, not noise:**
gemma4:e4b denied `entertainment_no_commitment` (relaxing with no stated
goal or deadline) confidently, 5 times out of 5. That's a direct violation
of "entertainment alone must not be classified as procrastination"
— a stable, repeatable failure mode, not a fluke. qwen3.8:27b answered
`uncertain` on the same scenario every time instead — not a perfect
`approve`, but never a false accusation.

**Both models share the same remaining weak spot:** `ambiguous` (rapid
app-switching, no clear pattern) — both confidently denied it every single
time instead of saying `uncertain`. This is the one genuine-semantic-
uncertainty case surviving after evidence validation removed the other
four; neither model handles it well. Not a reason to prefer one over the
other (they tie here), but a known gap either way — see the standing
recommendation below about not relying on model-judged uncertainty.

### Selection rule applied

> Prefer qwen3.8:27b if it preserves its zero false-positive-denial result
> and meaningfully outperforms e4b across repeated trials.

Both conditions hold: false-positive-denial stayed at exactly 0.0 across
5x repeated trials (not n=1 luck), and the composite gap (0.65 vs 1.05) is
driven by a real, stable, reproducible behavioral difference (the
`entertainment_no_commitment` miss), not sampling noise — stability was
1.0 for both models, so this isn't "unstable or negligible." **qwen3.8:27b
is the winner per the stated rule.**

### Does qwen3.8:27b justify its cost?

Yes, on the terms set by the priority ordering (false-positive-denial
outranks latency), but the cost is real: ~2x slower than e4b (10.6s vs
5.3s warm avg, 15.7s vs 7.1s p95) and 5x the VRAM (17.4GB vs 3.4GB). If
your actual usage pattern makes 10s judgments feel bad in practice, e4b
remains a completely reasonable choice — the priority ordering just isn't
what makes that trade for you automatically here.

### VRAM co-residency with qwen3.5:4b (personality model)

**qwen3.8:27b cannot plausibly coexist with qwen3.5:4b** under this box's
current settings:
- qwen3.8:27b measured: 17.4GB, 100% VRAM-resident.
- qwen3.5:4b measured: 3.34GB, 100% VRAM-resident.
- Combined: ~20.7GB, against a ~17.98GB *usable* budget (24GB total minus
  the 6GB `OLLAMA_GPU_OVERHEAD` reserve — itself load-bearing, traced to a
  prior VRAM-exhaustion desktop crash, and unchanged here). Exceeds usable
  budget before any per-request context/KV growth is even counted.
- Also moot under `OLLAMA_MAX_LOADED_MODELS=1`, unchanged here — only one
  model can be resident regardless of VRAM math.

**gemma4:e4b + qwen3.5:4b would fit comfortably**: 3.4GB + 3.34GB ≈ 6.7GB,
well under the 17.98GB usable budget with ~11GB headroom to spare. If a
future slice explores `OLLAMA_MAX_LOADED_MODELS=2` (explicitly not done
here, and only after this decision was made), e4b — not qwen3.8:27b — is
the vision-model candidate that experiment should actually consider.

## Production integration (done, separate commit from evidence validation)

- `v2/config.py`: `MODELS["vision"]` → `qwen3.8:27b`. `GATE_QUEUE_DEADLINE_SECONDS`
  120 → **30**. `GATE_PROMPT` extended to the 3-way schema
  (`verdict: approve|deny|uncertain` + `confidence`) so a real semantic
  "uncertain" is reachable in production, not just the benchmark.
- `v2/gatekeeper.py`: `_parse_verdict()` maps the new schema; a malformed
  or unparseable model response is itself treated as `uncertain` (inability
  to verify), never a confident denial. Coordinator failures (deadline,
  preemption, queue-full, lock-timeout) get a neutral `"Couldn't verify..."`
  message distinct from an actual judgment, with the specific reason kept
  in the machine-readable `reason` field, not the user-facing text — and
  are **not cached**, so a transient failure can't stick around as a
  denial for the full `GATE_TTL_SECONDS` window. A genuine model
  `uncertain` verdict also isn't cached, on the same reasoning.

### Live finding: 30s is comfortable warm, tight cold under contention

Live-tested against the real daemon with a real (non-synthetic) screenshot
and real activity log: a **cold** request (nothing resident, GPU recently
used by an unrelated chat request moments before) **exceeded the 30s
deadline** and correctly failed closed with the new neutral message. A
second, **warm** request immediately after completed in 23.6s and returned
a real, correct verdict. The benchmark's synthetic corpus (smaller images,
simpler prompts) measured cold at 18.66s — real screenshots and real
activity summaries are apparently heavier. **30s was implemented exactly
as instructed**, but this is a real, observed risk: expect occasional
cold-request timeouts in practice, especially right after other GPU
activity. If that proves annoying day to day, the fix is either a slightly
higher deadline or a way to keep qwen3.8:27b warm proactively — not
something this slice changes unilaterally.

## Reproducibility

```bash
cd huginn
# Original broad screen (5 candidates, 1 trial/scenario):
uv run --project . python3 scripts/benchmark-vision-models.py \
  qwen3.8:27b gemma4:31b gemma4:e2b gemma4:e4b llava:7b

# Focused decisive re-audition (2 finalists, 5 trials/scenario):
uv run --project . python3 scripts/benchmark-vision-models.py \
  --trials-per-scenario 5 --seed 7 qwen3.8:27b gemma4:e4b
```

Refuses to run if `~/.local/share/huginn/game-mode` exists; warns (use
`--force` to override) if GPU busy% is already ≥15% before starting.
Regenerates the exact synthetic corpus each run (`scripts/vision_bench/corpus.py`,
seeded/deterministic layout, no real desktop content); evidence-invalid
scenarios (stale/missing/corrupt) are checked via the real
`v2/evidence.py` before any model call and get one recorded zero-cost
trial rather than being repeated. Writes
`scripts/vision_bench/results/<UTC timestamp>/{raw.json,report.md,corpus/*.png}`.
Fixed generation params (`temperature=0.1, seed=7, num_ctx=8192`); despite
that, LLM sampling isn't guaranteed bit-identical run to run, though this
audition's 5x repeats came back perfectly stable (1.0) for both finalists.
