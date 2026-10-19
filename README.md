# tool-transport-bench

Micro-benchmark for the transport cost of tool-calling layers: in-process function call vs
MCP-shaped JSON-RPC over stdio. Companion artifact to the essay
"Tools, MCP, or Skills? What Each One Actually Costs" (iamdgarcia).

**▶ Replicate:** `python tools-vs-mcp-bench.py` — Python 3.10+, stdlib only, no dependencies,
no network. Runs in ~15 seconds.

## What it measures

Three numbers that decide how far an agent's tool shelf can scale:

1. **In-process function call** — the floor. 100,000 calls, mean.
2. **stdio JSON-RPC 2.0 round-trip** — newline-delimited request/response to a child process
   using the exact message shapes of the MCP stdio transport (`initialize`, `tools/list`,
   `tools/call`). Median over 200 round-trips. This isolates *transport cost* from SDK cost.
3. **tools/list payload size** — the schema bytes every tool (native or MCP) parks in model
   context on every request. Estimated tokens at chars/4.

Plus cold start: spawn + initialize, paid once per session per server.

## Results on the reference machine

| Layer | Cost |
|---|---|
| In-process function | 0.00005 ms |
| stdio JSON-RPC round-trip | 0.04 ms median |
| Cold start (spawn + initialize) | ~1.2 s |
| Schemas resident at 10 / 50 / 200 tools | ~520 / ~2,600 / ~10,300 tokens |

Headline: the wire is free. The bill is cold start (seconds, per server, before the first
tool responds — a voice-agent problem) and the residential schema tax (identical for native
tools and MCP, because the model-side contract is the same), which compounds with known
tool-selection degradation in many-tool shelves (RAG-MCP, arXiv:2505.03275: 43.13% vs 13.62%
selection accuracy; >50% prompt-token reduction via retrieval).

## Honest scope

- This is not an MCP-SDK-parity benchmark: it is a transport-framing benchmark. Real MCP
  servers add SDK overhead, capability negotiation, and your auth. The claim tested is
  "the wire adds latency to every call" — measured answer: no (tens of microseconds).
- Token estimates are chars/4, the standard crude estimator. Real tokenizers vary ±20%.
- One machine, one language runtime. The point of shipping it is that you run it on yours:
  differences across machines/OSes are exactly what this poc is for. PRs with per-platform
  results or an HTTP-transport variant welcome.

## Attribution

Built by [iamdgarcia](https://github.com/iamdgarcia) — realtime voice / conversational agent
engineering. Related: the [event-driven-voice-orchestrator conformance kit](https://github.com/iamdgarcia/event-driven-voice-orchestrator)
(measure runtime contracts, not vibes) and the essay this bench backs up.
