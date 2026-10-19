#!/usr/bin/env python3
"""Transport-cost micro-benchmark: in-process call vs MCP-shaped JSON-RPC over stdio.

Measures what a tool call costs at each layer:
  (a) in-process function call
  (b) newline-delimited JSON-RPC 2.0 round-trip over stdio to a child process
      (the MCP stdio transport framing: initialize -> tools/list -> tools/call)

Also measures tools/list payload size (the schema tokens every MCP/native tool puts in
context) for N tools.

stdlib-only, deterministic-ish: medians over N runs. Not a full MCP server — the JSON-RPC
framing and message shapes follow the MCP stdio transport, but we do not claim SDK parity.
"""
import json
import statistics
import subprocess
import sys
import time

N_CALLS = 200          # round-trips through the child process
N_INPROC = 100_000     # in-process calls
SCHEMA_CHARS = 350     # chars of a realistic tool schema (name+desc+params json)


# ---------- (a) in-process ----------
def target_tool(x: int, y: int) -> int:
    return x + y


def bench_inprocess() -> float:
    xs = range(N_INPROC)
    t0 = time.perf_counter()
    for i in xs:
        target_tool(i, i)
    t1 = time.perf_counter()
    return (t1 - t0) / N_INPROC * 1e6  # us per call


# ---------- (b) MCP-shaped stdio server ----------
SERVER = r'''
import json, sys
TOOLS = [
    {"name": f"tool_{i:03d}", "description": "Adds two integers and returns the sum.",
     "inputSchema": {"type": "object", "properties": {"x": {"type": "integer"},
                     "y": {"type": "integer"}}, "required": ["x", "y"]}}
    for i in range(int(sys.argv[1]))
]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        out = {"protocolVersion": "2026-07-28", "capabilities": {"tools": {}},
               "serverInfo": {"name": "bench", "version": "0.0.1"}}
    elif method == "tools/list":
        out = {"tools": TOOLS}
    elif method == "tools/call":
        out = {"content": [{"type": "text", "text": "42"}], "isError": False}
    else:
        out = {}
    print(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": out}), flush=True)
'''


def bench_stdio(n_tools: int) -> dict:
    p = subprocess.Popen([sys.executable, "-c", SERVER, str(n_tools)],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, encoding="utf-8", bufsize=1)
    def rpc(method, params=None):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rpc.n, "method": method,
                                  "params": params or {}}) + "\n")
        p.stdin.flush()
        resp = json.loads(p.stdout.readline())
        rpc.n += 1
        return resp["result"]
    rpc.n = 1

    t0 = time.perf_counter()
    rpc("initialize", {"protocolVersion": "2026-07-28", "capabilities": {}})
    init_ms = (time.perf_counter() - t0) * 1e3

    # tools/list payload size
    t0 = time.perf_counter()
    listing = rpc("tools/list")
    list_ms = (time.perf_counter() - t0) * 1e3
    payload = json.dumps(listing)

    # warm one call, then measure
    rpc("tools/call", {"name": "tool_000", "arguments": {"x": 1, "y": 2}})
    lat = []
    for i in range(N_CALLS):
        t0 = time.perf_counter()
        rpc("tools/call", {"name": f"tool_{i % n_tools:03d}",
                           "arguments": {"x": i, "y": i}})
        lat.append((time.perf_counter() - t0) * 1e3)

    p.stdin.close()
    p.wait(timeout=5)
    return {"init_ms": init_ms, "list_ms": list_ms,
            "list_bytes": len(payload), "call_ms_median": statistics.median(lat)}


def main():
    inproc_us = bench_inprocess()
    print(f"in-process function call : {inproc_us:8.2f} us   (median-free, {N_INPROC} calls)")

    rows = []
    for n in (10, 50, 200):
        r = bench_stdio(n)
        rows.append((n, r))
        print(f"stdio JSON-RPC round-trip: {r['call_ms_median']:7.3f} ms median "
              f"({N_CALLS} calls) | init {r['init_ms']:6.1f} ms | "
              f"tools/list {r['list_bytes']:7d} bytes (~{r['list_bytes']//4:6d} tok est) "
              f"for {n:3d} tools | list {r['list_ms']:5.1f} ms")
    print()
    print("context cost of schemas (every request, every tool):")
    for n, r in rows:
        print(f"  {n:3d} tools -> ~{r['list_bytes']//4:5d} tokens resident "
              f"= ~{r['list_bytes']//4 * 200 / 1000:.0f}k tokens per 200-call session (est, chars/4)")


if __name__ == "__main__":
    main()
