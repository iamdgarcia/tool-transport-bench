#!/usr/bin/env python3
"""E2E tool-calling bench: REAL model, three tool-delivery architectures.

Architecture A — native tools: tool schemas in the request, model emits tool_calls,
  we execute in-process, loop back the result.
Architecture B — MCP-shaped: identical model+schemas, but the tool executes through a
  real JSON-RPC round-trip to a child process (the MCP stdio transport framing), so we
  measure the hop where it actually lands.
Architecture C — tool-shelf scaling: same single-user-task, but the request carries
  N total schemas (N-1 distractors). Measures selection accuracy + added latency +
  token overhead as N grows (the "residential schema tax", now with real tokenizer
  numbers from usage.prompt_tokens).

Real model via OPENAI_BASE_URL (LiteLLM proxy) + OPENAI_API_KEY from .env.
stdlib + openai lib only.

Usage:
  python e2e_tool_bench.py --fast          # 1 model, small N grid
  python e2e_tool_bench.py --full          # multiple models, full grid
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

from openai import OpenAI

ROOT = Path(__file__).resolve().parent


def load_env() -> dict:
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        env_path = ROOT / ".env"
    if not env_path.exists():
        env_path = ROOT.parent / ".env"
    vals = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"\'')
    return vals


# ---------------- tool definitions ----------------
def make_tools(n_distractors: int = 0) -> tuple[list[dict], list[dict]]:
    """The target tool = add(x, y). Distractors = realistic but harmless others."""
    target_schema = {
        "type": "function",
        "function": {
            "name": "add",
            "description": "Add two integers and return the sum.",
            "parameters": {
                "type": "object",
                "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                "required": ["x", "y"],
            },
        },
    }
    names = [
        "lookup_order_status", "create_ticket", "send_email", "search_kb",
        "get_customer_profile", "schedule_callback", "cancel_order", "refund_charge",
        "transfer_to_human", "log_call_summary", "get_business_hours", "verify_identity",
        "check_inventory", "apply_discount", "get_invoice_pdf", "update_address",
        "list_open_disputes", "get_agent_sla", "escalate_supervisor", "tag_conversation",
    ]
    distractors = []
    for i, name in enumerate(names[:n_distractors]):
        distractors.append({
            "type": "function",
            "function": {
                "name": name,
                "description": f"Internal operation {name.replace('_', ' ')} for the "
                               f"customer-service workspace (operation family {i}).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                    "required": ["account_id"],
                },
            },
        })
    return [target_schema] + distractors, distractors


def exec_native(name: str, args: dict) -> str:
    return str(args.get("x", 0) + args.get("y", 0))


# ---------------- MCP-shaped child (same as transport bench) ----------------
SERVER = r'''
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        out = {"protocolVersion": "2026-07-28", "capabilities": {"tools": {}},
               "serverInfo": {"name": "bench", "version": "0.0.1"}}
    elif method == "tools/call":
        a = (msg.get("params") or {}).get("arguments") or {}
        out = {"content": [{"type": "text", "text": str(a.get("x", 0) + a.get("y", 0))}],
               "isError": False}
    else:
        out = {}
    print(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": out}), flush=True)
'''


class McpStdio:
    def __init__(self):
        self.p = subprocess.Popen(
            [sys.executable, "-c", SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1)
        self.n = 1
        self.t0 = time.perf_counter()
        self._rpc("initialize", {"protocolVersion": "2026-07-28", "capabilities": {}})
        self.init_ms = (time.perf_counter() - self.t0) * 1e3

    def _rpc(self, method, params=None):
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method,
                                       "params": params or {}}) + "\n")
        self.p.stdin.flush()
        resp = json.loads(self.p.stdout.readline())
        self.n += 1
        return resp["result"]

    def call(self, name: str, args: dict) -> str:
        r = self._rpc("tools/call", {"name": name, "arguments": args})
        return r["content"][0]["text"]

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


# ---------------- the bench ----------------
QUESTION = "What is 271 plus 834? Use the add tool and report the result."

def run_one(client, model, tools, arch, mcp, max_turns=3) -> dict:
    """One conversation until the model answers without tool calls."""
    messages = [{"role": "user", "content": QUESTION}]
    tool_ms = []          # execution latency per tool exec (native vs mcp hop)
    turns = 0
    t_start = time.perf_counter()
    answer = None
    for _ in range(max_turns):
        turns += 1
        t0 = time.perf_counter()
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=tools if tools else None)
        llm_ms = (time.perf_counter() - t0) * 1e3
        msg = resp.choices[0].message
        usage0 = resp.usage
        if msg.content and not msg.tool_calls:
            answer = msg.content
            break
        if not msg.tool_calls:
            answer = msg.content or "(empty)"
            break
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            t1 = time.perf_counter()
            if arch == "mcp":
                result = mcp.call(tc.function.name, args)
            else:
                result = exec_native(tc.function.name, args)
            tool_ms.append((time.perf_counter() - t1) * 1e3)
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": result})
    total_ms = (time.perf_counter() - t_start) * 1e3
    correct = answer is not None and ("1105" in (answer or "") or "1,105" in (answer or ""))
    return {"turns": turns, "llm_ms_first": round(llm_ms, 1),
            "tool_exec_ms": [round(m, 1) for m in tool_ms],
            "total_ms": round(total_ms, 1), "correct": correct,
            "prompt_tokens": usage0.prompt_tokens if usage0 else None,
            "answer_head": (answer or "")[:60]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()

    env = load_env()
    cert = env.get("SSL_CERT_FILE", "").strip("\"'")
    if cert and not Path(cert).exists() and (ROOT / "cacert.pem").exists():
        cert = str(ROOT / "cacert.pem")
    os.environ["SSL_CERT_FILE"] = cert or os.environ.get("SSL_CERT_FILE", "")
    os.environ["REQUESTS_CA_BUNDLE"] = os.environ["SSL_CERT_FILE"]
    client = OpenAI(base_url=env["OPENAI_BASE_URL"], api_key=env["OPENAI_API_KEY"], timeout=90)

    models = ["gemini-3.5-flash"] if args.fast else ["gemini-3.5-flash", "claude-sonnet-4-6-sa", "gemini-2.5-flash"]
    grid = [(1,), (11,)] if args.fast else [(1,), (11,), (21,)]

    print(f"question: {QUESTION}")
    print(f"models: {models} | shelf sizes: {[g[0] for g in grid]}\n")

    for model in models:
        for (n_shelf,) in grid:
            n_distractors = n_shelf - 1
            tools, _ = make_tools(n_distractors)

            # A: native
            rA = run_one(client, model, tools, "native", None)

            # B: MCP-shaped hop (same request, tool executes through stdio child)
            mcp = McpStdio()
            rB = run_one(client, model, tools, "mcp", mcp)
            mcp_ms = mcp.init_ms
            mcp.close()

            print(f"[{model} | shelf={n_shelf:2d}]")
            print(f"  A native : total {rA['total_ms']:8.1f} ms | 1st LLM {rA['llm_ms_first']:8.1f} ms | "
                  f"tool exec {rA['tool_exec_ms']} ms | turns {rA['turns']} | "
                  f"prompt_tok {rA['prompt_tokens']} | correct {rA['correct']}")
            print(f"  B mcp    : total {rB['total_ms']:8.1f} ms | 1st LLM {rB['llm_ms_first']:8.1f} ms | "
                  f"tool exec {rB['tool_exec_ms']} ms | turns {rB['turns']} | "
                  f"prompt_tok {rB['prompt_tokens']} | correct {rB['correct']} | "
                  f"child init {mcp_ms:.0f} ms")
            print(f"  delta B-A: total {rB['total_ms']-rA['total_ms']:+8.1f} ms | "
                  f"tokens {rB['prompt_tokens']-rA['prompt_tokens']:+d}\n")

    print("Legend: 'tool exec' = native function call vs full MCP stdio round-trip.")
    print("'prompt_tok' = usage.prompt_tokens of the FIRST request (carries all schemas).")


if __name__ == "__main__":
    main()
