# Results — E2E tool-calling bench (real models)

Run: 2026-10-19, an OpenAI-compatible gateway serving Gemini/Claude-class models,
`gemini-3.5-flash` + `claude-sonnet-4-6-sa` + `gemini-2.5-flash`, 3 shelf sizes,
question "What is 271 plus 834? Use the add tool and report the result.",
2-turn conversations, native exec vs full MCP-shaped stdio round-trip.
Raw repro: `python e2e_tool_bench.py --full` (needs OPENAI_BASE_URL/OPENAI_API_KEY in .env).

## Per-model medians across shelf sizes (single run per cell, same session)

| Model | tool exec native | tool exec MCP-hop | delta total B−A | child init |
|---|---|---|---|---|
| gemini-3.5-flash | 0.0 ms | 0.2–0.5 ms | −2122 … +144 ms (noisy, ±0) | ~1.15 s |
| claude-sonnet-4-6-sa | 0.0 ms | 0.2–0.3 ms | −946 … +761 ms (noisy) | ~1.14 s |
| gemini-2.5-flash | 0.0 ms | 0.3–0.4 ms | −444 … +111 ms (noisy) | ~1.10 s |

Correctness: 9/9 cells correct on all three models (claude formats as "1,105" —
checker handles both).

## What the real-model run adds over the transport-only bench

1. **MCP-hop tool exec: 0.2–0.5 ms end-to-end through a real child process** — buried in
   LLM variance (first-token LLM latency alone swings ±800 ms between identical runs).
   Any infrastructure choice defended on "MCP adds latency" is defending noise.
2. **The schema tax, with REAL tokenizer counts** (usage.prompt_tokens, first request):
   - gemini-3.5-flash: 144 → 564 → 989 tok at shelf 1 → 11 → 21
   - claude-sonnet-4-6-sa: 682 → 1,476 → 2,273 tok (heavier baseline; schemas cost ~2.3×
     gemini's on the same shelf)
   - gemini-2.5-flash: 114 → 431 → 740 tok
   Tokenizer-dependent, not chars/4: at 21 tools the real counts run 15–40% above the
   crude estimate. The tax is per-request and grows linearly; tokenizer choice changes
   its absolute size, not its shape.
3. **Cold start confirmed at ~1.1–1.2 s per server** — every cell pays it once; in a voice
   agent that's dead air before the greeting if servers connect eagerly.
4. **Model variance > infrastructure variance**: identical A/B cells differ by up to
   ±2.1 s between runs on the same shelf (LLM side). Any bench that wants to compare
   architectures must median over many runs or measure the tool hop in isolation —
   the transport bench (this repo) does the latter.

## Caveats

- Single run per cell (n=1): treat deltas as noise-band evidence, not point estimates.
  The stable signals are the ones consistent across all 18 cells: sub-ms tool hop,
  ~1.1 s child init, linear schema tax.
- gpt-5.6-sol excluded: Azure backend unreachable from this network that day.
- add() is trivially fast to execute; a real tool with I/O will dominate both arms
  identically — which is the point: architecture choice does not change tool I/O cost.


---

# Results — decision bench (06_decision.py): same capability, three architectures

Run 2026-10-19, `gemini-3.5-flash`, 6 refund cases with real policy edge cases, 3 runs of the
full matrix (n=3 per cell). Ground truth = the deterministic engine.

## Latency per final answer (medians across 3 runs)

| Arm | Final-answer latency | Tokens (final request) | Extra turns |
|---|---|---|---|
| A native tool | ~2.1 s | ~500 | 0 (single tool turn) |
| B MCP hop | ~1.9 s | ~470 | 0 |
| C skill + manual | ~4.6 s | ~600 | +1 (the load_skill turn) |

A vs B: statistically indistinguishable (the hop is 0.3 ms; LLM noise is ±800 ms). C pays
one extra LLM turn to load the manual: ~2× wall clock, ~+100 tok.

## Decision match vs engine (across 3 runs)

| Arm | match rate |
|---|---|
| native tool | 100% / 100% / 83% |
| MCP hop | 100% / 100% / 83% |
| skill + manual | 83% / 100% / 83% |

The 83% cells are the model paraphrasing the decision (saying "the request requires a
review process" without the literal word) — the deterministic engine underneath never
varies. Arm C occasionally settles on the right action with different framing.

## What this settles

- **A vs B is NOT a latency or accuracy question** — same model, same tokens, same answers;
  the only real difference is operational: who owns the executor, who can reuse it, who
  gets paged when it breaks.
- **C trades ~2x latency and one turn for reasoning over free-form policy** — plus it is
  the only arm that handles inputs the schema never anticipated (the tool enum DENYs
  "different card"; the manual explains WHY and what to offer instead).
- The production rule the numbers support: **deterministic core in a tool (native or MCP —
  the hop never decides), judgment and changing rules in a skill, and don't ship a tool
  whose schema is a straitjacket for cases that need explanation.**
