# Vision-model gatekeeper audition — recommendation

**Status: audition only. No production change made.** Gatekeeper's configured
model, Ollama's service environment, the Runtime Context Engine, personality
routing, and SYSTEM_PROMPT are all untouched. This document is for review.

Definitive dataset: `scripts/vision_bench/results/20260903T031452Z/`
(`raw.json` + `report.md`). An earlier 4-model run (`20260903T030452Z`,
missing qwen3.8:27b) is also on disk but superseded by this one — kept
only because both are internally consistent (see the version note below),
not because there's any doubt about the newer one.

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

## Reproducibility

```bash
cd huginn
uv run --project . python3 scripts/benchmark-vision-models.py \
  qwen3.8:27b gemma4:31b gemma4:e2b gemma4:e4b llava:7b
```

Refuses to run if `~/.local/share/huginn/game-mode` exists; warns (use
`--force` to override) if GPU busy% is already ≥15% before starting.
Regenerates the exact synthetic corpus each run (`scripts/vision_bench/corpus.py`,
seeded/deterministic layout, no real desktop content) and writes
`scripts/vision_bench/results/<UTC timestamp>/{raw.json,report.md,corpus/*.png}`.
Fixed generation params (`temperature=0.1, seed=7, num_ctx=8192`) for
repeatability; note LLM sampling still isn't bit-identical run to run even
at temperature 0.1, so expect small variation on re-run, not exact
reproduction of every verdict.

## Proposed production deadline and fail-closed UX (proposal only, not implemented)

Current: `GATE_QUEUE_DEADLINE_SECONDS = 120` (config.py), covering queue
wait + run combined via the coordinator.

- With `gemma4:e4b` or `qwen3.8:27b`, a `GATE_QUEUE_DEADLINE_SECONDS` of
  **30–45s** would comfortably cover cold load (9–19s) plus a slow warm
  case, while still failing closed well before it feels broken to someone
  waiting on a Steam launch. This is a meaningful tightening from the
  current 120s, itself only possible because of the faster models tested
  here (the current 120s exists to tolerate the control's worst case).
- Fail-closed message stays as-is (`"Not while you're playing. Ask again
  after."` for game mode; `"Judgment failed (...). Denying by default."`
  for other denials) — both already tested in slice 2/3 and unaffected by
  a model swap.
- If `qwen3.8:27b` is chosen: given its slower warm latency, consider
  whether 45s (rather than 30s) is the safer deadline to avoid raising the
  false-timeout rate relative to e4b.

Not implemented: this is a proposal for you to decide on before any
config or model change lands.
