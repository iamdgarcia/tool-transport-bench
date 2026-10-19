#!/usr/bin/env python3
"""A REAL, runnable MCP server (stdio) + client — the minimal complete pair.

Not a shim: implements the MCP stdio protocol lifecycle honestly enough that you can
point a standard MCP client at it:
  initialize -> (notifications/initialized) -> tools/list -> tools/call
and it answers with protocolVersion 2026-07-28, serverInfo, tool schemas, results.

The server exposes ONE tool: `refund_decision` — a deterministic rules engine that
encodes the same refund policy as 03_skill_disclosure.py's skill body. That's the point:
the SAME capability shipped two ways (knowledge via skill, capability via tool) so you
can feel the difference:

  skill (03): model reads the manual and reasons   -> flexible, can handle novel cases
  tool  (04): model asks, engine answers deterministically -> fast, testable, no judgment

Run the server standalone (it just speaks JSON-RPC on stdio):
  python 04_mcp_server.py

Run the client demo (spawns the server, does the full lifecycle, 3 decisions):
  python 04_mcp_server.py --demo
"""
from __future__ import annotations

import json
import sys

PROTOCOL_VERSION = "2026-07-28"

TOOLS = [
    {
        "name": "refund_decision",
        "description": ("Deterministic refund-eligibility engine. Provide sku, days_since_delivery, "
                        "price_usd, opened, damaged (bool), refund_target (original|gift|other). "
                        "Returns the policy decision and the code to log."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "days_since_delivery": {"type": "integer"},
                "price_usd": {"type": "number"},
                "opened": {"type": "boolean"},
                "damaged": {"type": "boolean"},
                "refund_target": {"type": "string", "enum": ["original", "gift", "other"]},
            },
            "required": ["sku", "days_since_delivery", "price_usd", "opened", "damaged",
                         "refund_target"],
        },
    }
]


def refund_decision(a: dict) -> dict:
    sku = (a.get("sku") or "").upper()
    days = a.get("days_since_delivery", 0)
    price = a.get("price_usd", 0)
    opened = a.get("opened", False)
    damaged = a.get("damaged", False)
    target = a.get("refund_target", "original")

    if damaged:
        return {"decision": "APPROVE", "code": "RF-DAMAGED",
                "note": "damaged-on-arrival overrides every window; full refund",
                "conditions": ["photo evidence attached"]}
    if sku.startswith("CLR-") and days > 7:
        return {"decision": "DENY", "code": "RF-WINDOW", "note": "clearance: 7-day window"}
    if sku.startswith("SEA-") and days > 14:
        return {"decision": "DENY", "code": "RF-WINDOW", "note": "seasonal: 14-day window"}
    if days > 30:
        return {"decision": "DENY", "code": "RF-WINDOW", "note": "30-day window exceeded"}
    if sku.startswith("ELEC") and opened:
        if price > 500:
            return {"decision": "SUPERVISOR", "code": "RF-SUPERVISOR",
                    "note": "opened electronics over 500 USD: supervisor approval first"}
        return {"decision": "REVIEW", "code": "RF-ELIG",
                "note": "opened electronics: 85% restocking check applies"}
    if target == "other":
        return {"decision": "DENY", "code": "RF-ELIG",
                "note": "refunds go to the ORIGINAL payment method only"}
    if target == "gift":
        return {"decision": "APPROVE", "code": "RF-ELIG",
                "note": "gift orders refund to the purchaser"}
    if price > 500:
        return {"decision": "SUPERVISOR", "code": "RF-SUPERVISOR",
                "note": "amount over 500 USD: supervisor approval required"}
    return {"decision": "APPROVE", "code": "RF-ELIG", "note": "within window, standard rules"}


def handle(msg: dict) -> dict:
    method = msg.get("method", "")
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL_VERSION,
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "refund-engine", "version": "1.0.0"}}
    elif method == "notifications/initialized":
        return {}  # notification: no response
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") != "refund_decision":
            result = {"content": [{"type": "text", "text": f"unknown tool {params.get('name')}"}],
                      "isError": True}
        else:
            d = refund_decision(params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(d)}], "isError": False}
    else:
        result = {}
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": result}


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        out = handle(json.loads(line))
        if out:
            print(json.dumps(out), flush=True)


def demo():
    """Spawn self as a subprocess and drive the full lifecycle (what a client does)."""
    import subprocess
    child = subprocess.Popen([sys.executable, __file__], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
    n = 1
    def rpc(method, params=None):
        nonlocal n
        child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n, "method": method,
                                      "params": params or {}}) + "\n")
        child.stdin.flush()
        n += 1
        return json.loads(child.stdout.readline())["result"]

    init = rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}})
    print(f"initialized: {init['serverInfo']} (protocol {init['protocolVersion']})")
    tools = rpc("tools/list")["tools"]
    print(f"tools: {[t['name'] for t in tools]}")

    cases = [
        # (question a real user would ask, arguments)
        ("opened electronics, 12 days, 189 USD", dict(sku="ELEC-771", days_since_delivery=12,
         price_usd=189, opened=True, damaged=False, refund_target="original")),
        ("moldy perishable basket, photo attached", dict(sku="FRSH-2", days_since_delivery=3,
         price_usd=64, opened=False, damaged=True, refund_target="original")),
        ("720 USD electronics, unopened, wants it on a different card", dict(sku="ELEC-990",
         days_since_delivery=9, price_usd=720, opened=False, damaged=False, refund_target="other")),
    ]
    for label, args in cases:
        r = rpc("tools/call", {"name": "refund_decision", "arguments": args})
        d = json.loads(r["content"][0]["text"])
        print(f"- {label}\n    -> {d['decision']} ({d['code']}): {d['note']}")
    child.stdin.close()
    child.wait(timeout=5)
    print("\nSame three cases, answered by 03_skill_disclosure.py's model+manual: "
          "compare stability. The tool is deterministic; the model+manual is flexible.")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    else:
        serve()
