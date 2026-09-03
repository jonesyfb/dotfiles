# Direct-conversation model audition — performance report (letter-keyed)

Same candidate letters as report_blind.md and mapping.json. Judge prose quality in report_blind.md BEFORE reading this file.

| Candidate | Total calls | Errors | Cold load (s) | Warm p50 (s) | Warm p95 (s) | Avg tok/s | VRAM resident (GB) |
|---|---|---|---|---|---|---|---|
| A | 183 | 0 | 1.482 | 0.66 | 1.07 | 89.4 | 5.34 |
| B | 183 | 0 | 10.555 | 1.56 | 4.64 | 32.5 | 16.24 |
| C | 183 | 0 | 1.486 | 0.4 | 0.71 | 125.4 | 3.11 |
| D | 183 | 0 | 17.353 | 1.13 | 1.66 | 33.3 | 18.88 |

Cold load = load_duration on each candidate's first call this session (model was explicitly unloaded beforehand, so this is a true cold load). Warm p50/p95 exclude that first call. Avg tok/s = eval_count / eval_duration averaged over all successful calls. VRAM resident is read once via /api/ps; note OLLAMA_MAX_LOADED_MODELS=1 means only one candidate is ever resident at a time even during this benchmark.