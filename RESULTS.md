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


---

# Results — harness configurations (07_harness_configs.py): N tools, N MCP servers, real logic

The question: when you BUILD an agentic system, how should the harness expose capabilities?
Same backend world (11 tools with real logic: orders, inventory, ledger, tickets, escalations —
tools MUTATE state), same 6 multi-step tasks (lookup → policy decision → money movement →
ticket/escalation closure), 4 configurations, 2 clean runs per config, oracle = final WORLD
state (not text matching).

| Config | schemas resident | boot cost | oracle pass | median turns/case | tool-exec Σ |
|---|---|---|---|---|---|
| A flat-native (11 schemas, in-process) | 11 | 0 | **12/12** | 7/9/15/8/6/12 | 0.6 ms |
| B grouped-mcp (same 11 schemas, 3 real MCP servers) | 11 | ~3.5 s | **12/12** | 8/9/16/10/6/11 | ~48 ms |
| C lazy-mcp (2 meta-tools, connect-on-demand) | 2 | 0 | 2/12 | 13/12/16/11/7/15 | — |
| D skill-core (5 tools + procedure manual on demand) | 5 | 0 | 11/12 | **6/6/7/5/5/7** | 0.25 ms |

## What the numbers say

1. **A vs B: identical outcomes, the hop is noise.** 12/12 both; tool-exec 0.6 ms vs 48 ms —
   both invisible next to ±800 ms LLM variance. B pays ~3.5 s boot (3 servers) + ops surface.
   The hop never decides; ownership does.
2. **C (lazy-MCP) loses everywhere it shouldn't.** 2/12 oracle with +60% turns: the model
   must chain connect_domain → read tool list → execute_tool blind (schemas not in context),
   and it fumbles arg shapes and forgets to connect domains. Meta-tools trade context for
   REASONING TURNS — a bad trade for mid-tier models on multi-step work.
3. **D (skill + core tools) is the efficiency winner**: fewest turns by far (5-7 vs 6-16),
   fewest calls, ~86-84k LLM ms vs ~110-130k, 11-12/12 oracle. The manual is paid once
   (a load turn) and then GUIDES the whole procedure — the model stops wandering.
4. **The A/B case-6 wander is the schema-bloat signal**: with 11 schemas resident the model
   loops search_kb (6-12 extra turns on the digital-good case in early runs); with D's
   5 schemas + a manual it finishes in 5-7 turns every time. Fewer schemas + better
   instructions beat more schemas + more tools.
5. **Contract hardening mattered as much as configuration**: the model executed a DENY code
   as a refund (RF-LICENSE with money movement) until the executor started rejecting
   non-approval codes. Tools that move money must validate decision codes — model-side
   discipline is not a control.

## Config guidance (evidence-backed)

- Default harness: **core tools resident + procedures as skills** (D): fewest turns, best
  token economics, full oracle pass.
- Add MCP servers when tools belong to other teams/domains (B works, 12/12, costs boot + ops).
- Lazy/meta-tool indirection (C) costs more than it saves: schemas are cheap; turns are not.
