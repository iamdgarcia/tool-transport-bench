# tool-transport-bench

Which extension architecture should your agentic system use — native tools, an MCP server,
or skills with associated scripts? This repo answers that question with runnable evidence
instead of opinions: four benches + a complete MCP server + a complete Agent Skill, all
shipped the way you would actually build them.

Companion artifact to the essay *"Tools, MCP, or Skills? What Each One Actually Costs."*

```
tool-transport-bench/
├── 01_transport_bench.py    # micro: in-process vs MCP-shaped stdio round-trip (no LLM needed)
├── 02_native_vs_mcp.py      # E2E: real model, real conversations, native vs MCP hop
├── 03_skill_disclosure.py   # E2E: fat system prompt vs skill loaded on demand
├── 04_mcp_server.py         # a REAL, runnable MCP server (stdio, protocol 2026-07-28) + demo
├── 05_skill_demo.py         # a REAL, runnable Agent Skill (SKILL.md) + progressive disclosure live
├── 06_decision.py           # the decision: same capability shipped all three ways, one matrix
├── skills/refund-policy/    # the skill directory (SKILL.md + linked policy_table.md)
├── RESULTS.md               # all measured numbers
└── .env.example             # what you need to run the LLM-backed benches
```

## Setup (2 minutes)

```bash
cp .env.example .env        # fill in any OpenAI-compatible endpoint + key
pip install openai          # the only dependency
python 01_transport_bench.py    # works with NO .env at all (transport math only)
```

`.env` needs three values (any provider that speaks the OpenAI API: self-hosted gateways,
OpenRouter, a local vLLM/Ollama layer, etc. — nothing in this repo cares which):

```
OPENAI_BASE_URL=https://api.openai.com/v1   # your endpoint
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gemini-3.5-flash               # any chat model your endpoint serves
```

## Run order (each ~15s-4min)

```bash
python 01_transport_bench.py        # 1. transport cost: the wire is free (no LLM needed)
python 04_mcp_server.py --demo      # 2. a complete MCP server: lifecycle + 3 decisions
python 05_skill_demo.py             # 3. a complete Agent Skill: progressive disclosure live
python 02_native_vs_mcp.py --fast   # 4. E2E conversations: native vs MCP (real model)
python 03_skill_disclosure.py       # 5. E2E: always-resident manual vs on-demand skill
python 06_decision.py               # 6. THE DECISION MATRIX: same capability, 3 architectures
```

## The answer, in one paragraph

**A vs B (native vs MCP) is not a latency or accuracy question** — the hop costs 0.2–0.5 ms,
model noise is ±800 ms, tokens and answers are identical. It is an ownership question: pick
MCP when the tool belongs to another team/domain and many clients will reuse it; pick native
when you ship and test it with your own deploy. **Skills solve a different problem** — they
move knowledge out of the schema tax: a hundred procedures cost tens of tokens each, and the
manual is paid once (on the turn it's loaded), not on every request. The measured trade:
skills are ~2× slower end-to-end (one extra load turn) but they reason over free-form rules,
explain *why*, and handle inputs a tool schema never anticipated. Production shape: **deterministic
core in a tool, judgment and fast-changing rules in a skill, and never a tool whose schema is
a straitjacket for cases that need explanation.** Full numbers: [RESULTS.md](RESULTS.md).

## Reference results

| Measurement | Value |
|---|---|
| In-process function call | 0.00005 ms |
| MCP-shaped stdio round-trip | 0.04 ms (transport) / 0.2–0.5 ms (E2E, real models) |
| Child cold start | ~1.1–1.2 s per server |
| Schema tax @ 21 tools (real tokenizers) | 989 tok (gemini-3.5-flash) … 2,273 tok (claude-sonnet-4-6) |
| Skill load turn cost | +1 turn, ~2× wall clock vs tool call |
| Decision match (6 cases × 3 runs) | tool arms 83–100%, skill arm 83–100% |

## Honest scope

- `02/03/06` use single runs per cell unless noted (n=1..3): deltas are noise-band evidence;
  the stable signals are the ones consistent across every run (sub-ms hop, ~1.1 s init,
  linear schema tax, skill's +1 turn).
- The MCP server implements the stdio lifecycle honestly (initialize → tools/list →
  tools/call, protocolVersion 2026-07-28) but is not a drop-in SDK replacement; it exists so
  you can read every hop. Point a standard MCP client at it with
  `python 04_mcp_server.py` as the command.
- Token counts come from your endpoint's `usage.prompt_tokens` — real tokenizer, not an estimate.
- One machine, Python, n small. PRs with per-platform results, HTTP-transport variants, or
  bigger case grids are exactly what this poc wants.

## Attribution

Built by [iamdgarcia](https://github.com/iamdgarcia) — realtime voice / conversational agent
engineering. Related: the [event-driven-voice-orchestrator conformance kit](https://github.com/iamdgarcia/event-driven-voice-orchestrator)
(measure runtime contracts, not vibes).
