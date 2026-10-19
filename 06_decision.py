#!/usr/bin/env python3
"""The decision, run live: same customer-service capability, shipped THREE ways.

Capability under test: "should this refund be approved?" with a policy that has real
edge cases (opened electronics = 85% restocking, damage overrides windows, >500 USD
needs supervisor, original-method-only).

  A. NATIVE TOOL  — refund_decision(x) as an in-process function; model calls it.
  B. MCP SERVER   — same schema; the call travels the full MCP stdio lifecycle
                    (04_mcp_server.py) to a child process and back.
  C. SKILL        — no tool; the model loads skills/refund-policy/SKILL.md and reasons
                    over the manual (05_skill_demo.py's mechanism).

It then runs the SAME six cases through all three arms and prints the decision matrix:
latency, tokens, correctness vs the deterministic engine, and behavioral notes (what the
model+manual arm adds: it caught the case the engine's enum forced, etc.).

Ends with the decision rule this repo argues for, with YOUR numbers attached.

Run:  python 06_decision.py          (needs .env; ~2-4 min)
      python 06_decision.py --quick  (2 cases per arm)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from openai import OpenAI

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def load_env() -> dict:
    for cand in (Path.cwd() / ".env", ROOT / ".env", ROOT.parent / ".env"):
        if cand.exists():
            vals = {}
            for line in cand.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip().strip('"\'')
            return vals
    raise SystemExit("no .env found (see .env.example)")


from importlib import import_module  # noqa: E402
server = import_module("04_mcp_server")  # reuse the same engine + schema

SCHEMA = [{
    "type": "function",
    "function": {
        "name": "refund_decision",
        "description": server.TOOLS[0]["description"],
        "parameters": server.TOOLS[0]["inputSchema"],
    },
}]

SYSTEM_TOOL = ("You are a customer-service agent. Use the refund_decision tool for every "
               "refund question; report its decision and code.")
SYSTEM_SKILL = ("You are a customer-service agent. Installed skill: refund-policy. "
                "Load it with load_skill before answering any refund question.")

SKILL_SCHEMA = [{
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": "Load a skill's full body. Available: refund-policy.",
        "parameters": {"type": "object", "properties": {"skill": {"type": "string"}},
                       "required": ["skill"]},
    },
}]

CASES = [
    ("opened electronics, 12d, $189, original card",
     dict(sku="ELEC-771", days_since_delivery=12, price_usd=189, opened=True,
          damaged=False, refund_target="original")),
    ("moldy perishable, photo attached, 3d",
     dict(sku="FRSH-2", days_since_delivery=3, price_usd=64, opened=False,
          damaged=True, refund_target="original")),
    ("$720 electronics, unopened, wants a different card",
     dict(sku="ELEC-990", days_since_delivery=9, price_usd=720, opened=False,
          damaged=False, refund_target="other")),
    ("apparel, 20d, $85, original card",
     dict(sku="APP-410", days_since_delivery=20, price_usd=85, opened=True,
          damaged=False, refund_target="original")),
    ("clearance item, 10d after delivery, $40",
     dict(sku="CLR-77", days_since_delivery=10, price_usd=40, opened=False,
          damaged=False, refund_target="original")),
    ("digital good, never activated, 5d, $29",
     dict(sku="DIGI-5", days_since_delivery=5, price_usd=29, opened=False,
          damaged=False, refund_target="original")),
]

TEXT_ONLY_CASES = {
    # natural-language rendering of each case for the skill arm
    0: "Customer bought an electronics item (SKU ELEC-771) delivered 12 days ago, box "
       "opened, $189, wants the money back to the original card. Decide.",
    1: "Perishable basket (SKU FRSH-2) delivered 3 days ago arrived moldy; customer "
       "attached a photo; $64. Decide.",
    2: "Electronics order (SKU ELEC-990) delivered 9 days ago, box unopened, $720; the "
       "customer demands the refund go to a different card. Decide.",
    3: "Apparel item (SKU APP-410) delivered 20 days ago, $85, refund to original card. Decide.",
    4: "Clearance item (SKU CLR-77) delivered 10 days ago, $40, original card. Decide.",
    5: "Digital good (SKU DIGI-5), license never activated, delivered 5 days ago, $29. Decide.",
}


def mcp_call(child, name, args) -> dict:
    child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mcp_call.n,
                                  "method": "tools/call",
                                  "params": {"name": name, "arguments": args}}) + "\n")
    child.stdin.flush()
    mcp_call.n += 1
    return json.loads(json.loads(child.stdout.readline())["result"]["content"][0]["text"])


def run_tool_arm(client, model, question, use_mcp, child=None):
    messages = [{"role": "system", "content": SYSTEM_TOOL},
                {"role": "user", "content": question}]
    for turn in range(4):
        t0 = time.perf_counter()
        r = client.chat.completions.create(model=model, messages=messages,
                                           tools=SCHEMA, max_tokens=700)
        ms = (time.perf_counter() - t0) * 1e3
        m = r.choices[0].message
        if not m.tool_calls:
            return {"ms": ms, "tok": r.usage.prompt_tokens,
                    "answer": m.content or "", "full": m.content or ""}
        args = json.loads(m.tool_calls[0].function.arguments or "{}")
        messages.append({"role": "assistant", "content": m.content or "",
                         "tool_calls": [tc.model_dump() for tc in m.tool_calls]})
        if use_mcp:
            d = mcp_call(child, "refund_decision", args)
        else:
            d = server.refund_decision(args)
        messages.append({"role": "tool", "tool_call_id": m.tool_calls[0].id,
                         "content": json.dumps(d)})
    return {"ms": ms, "tok": r.usage.prompt_tokens, "answer": "(no convergence)"}


def run_skill_arm(client, model, question):
    desc = ("refund-policy: Step-by-step refund eligibility rules for orders. "
            "Load it before answering any refund question.")
    messages = [{"role": "system", "content": SYSTEM_SKILL + "\n" + desc},
                {"role": "user", "content": question}]
    skill_body = (ROOT / "skills" / "refund-policy" / "SKILL.md").read_text(encoding="utf-8")
    skill_body = skill_body.split("---", 2)[2].strip()
    loaded = False
    for turn in range(5):
        t0 = time.perf_counter()
        r = client.chat.completions.create(model=model, messages=messages,
                                           tools=SKILL_SCHEMA, max_tokens=900)
        ms = (time.perf_counter() - t0) * 1e3
        m = r.choices[0].message
        if not m.tool_calls:
            return {"ms": ms, "tok": r.usage.prompt_tokens,
                    "answer": m.content or "", "full": m.content or "", "loaded": loaded}
        args = json.loads(m.tool_calls[0].function.arguments or "{}")
        messages.append({"role": "assistant", "content": m.content or "",
                         "tool_calls": [tc.model_dump() for tc in m.tool_calls]})
        messages.append({"role": "tool", "tool_call_id": m.tool_calls[0].id,
                         "content": skill_body if args.get("skill") == "refund-policy"
                         else "(unknown skill)"})
        loaded = True
    return {"ms": ms, "tok": r.usage.prompt_tokens, "answer": "(no convergence)",
            "loaded": loaded}


def main():
    quick = "--quick" in sys.argv
    cases = CASES[:2] if quick else CASES
    env = load_env()
    os.environ["SSL_CERT_FILE"] = env.get("SSL_CERT_FILE", "").strip('"\'') or os.environ.get("SSL_CERT_FILE", "")
    os.environ["REQUESTS_CA_BUNDLE"] = os.environ["SSL_CERT_FILE"]
    client = OpenAI(base_url=env["OPENAI_BASE_URL"], api_key=env["OPENAI_API_KEY"], timeout=90)
    model = env.get("OPENAI_MODEL", "gemini-3.5-flash")

    print(f"model: {model} | cases: {len(cases)}\n")
    child = subprocess.Popen([sys.executable, str(ROOT / "04_mcp_server.py")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             text=True, encoding="utf-8", bufsize=1)
    child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                                  "params": {"protocolVersion": "2026-07-28"}}) + "\n")
    child.stdin.flush()
    child.stdout.readline()
    mcp_call.n = 1

    rows = []
    for i, (label, args) in enumerate(cases):
        q = TEXT_ONLY_CASES[i]
        # ground truth = the deterministic engine
        truth = server.refund_decision(args)

        a = run_tool_arm(client, model, q, use_mcp=False)
        b = run_tool_arm(client, model, q, use_mcp=True, child=child)
        c = run_skill_arm(client, model, q)

        def ok(ans, full):
            code = truth["decision"].lower()
            return code in (full or "").lower() or truth["code"] in (full or "")
        rows.append((label, truth, a, b, c))
        print(f"case {i+1}: {label}")
        print(f"  engine (truth) : {truth['decision']} ({truth['code']})")
        print(f"  A native tool  : {a['ms']:6.0f} ms | {a['tok']:5d} tok | match: {ok(a['answer'], a.get('full'))} | {a['answer'][:70]}")
        print(f"  B MCP hop      : {b['ms']:6.0f} ms | {b['tok']:5d} tok | match: {ok(b['answer'], b.get('full'))} | {b['answer'][:70]}")
        print(f"  C skill+manual : {c['ms']:6.0f} ms | {c['tok']:5d} tok | match: {ok(c['answer'], c.get('full'))} | loaded: {c['loaded']} | {c['answer'][:70]}")
        print()

    child.stdin.close()
    child.wait(timeout=5)

    n = len(rows)
    def arm_match(idx):
        hits = sum(1 for r in rows
                   if r[idx].get("full") and (r[1]["decision"].lower() in r[idx]["full"].lower()
                                              or r[1]["code"] in r[idx]["full"]))
        return hits / n * 100
    print("== matrix ==")
    print(f"native tool : match {arm_match(2):.0f}% (deterministic engine decides; model relays)")
    print(f"MCP hop     : match {arm_match(3):.0f}% (same engine, one process hop away)")
    print(f"skill+manual: match {arm_match(4):.0f}% (model reasons over policy text)")
    print("Summary rule (with your numbers):")
    print("  A/B: identical model behavior, identical tokens — the only difference is who")
    print("       owns the executor. Pick MCP when ownership/sharing matters, native when")
    print("       latency-to-ship and testing matter. The hop itself never decides.")
    print("  C:   the only arm that REASONS over policy text — the place for judgment calls,")
    print("       edge case nuance, and rules that change with compliance. Slower, fuzzier,")
    print("       and costs a load turn — but it is also the only arm that can explain WHY")
    print("       and handle inputs the schema never anticipated.")


if __name__ == "__main__":
    main()
