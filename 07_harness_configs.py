#!/usr/bin/env python3
"""The harness-configuration experiment: N real tools, N real MCP servers, real logic.

Same backend world (harness_world.py: orders, inventory, ledger, tickets, escalations -
tools MUTATE state, correctness is judged on the final world state by an oracle), same six
multi-step tasks, four harness configurations:

  A flat-native : all 11 tool schemas resident, execution in-process.
  B grouped-mcp : the same 11 tools split across 3 REAL MCP servers (orders/payments/support);
                  all 11 schemas still resident, every call pays the stdio hop.
  C lazy-mcp    : only 2 meta-schemas resident (connect_domain, list_domain_tools);
                  the model connects one domain server at a time under demand.
  D skill-core  : only the 4-step core flow resident as 3 tools (get_order, refund_decision,
                  apply_refund); the FULL procedure lives in a skill manual loaded on demand.

Each cell answers: how many schemas does the model carry, how many LLM turns does the task
take, what does it cost, and does the world end in the oracle's state?

Run:  python 07_harness_configs.py           # full: 6 cases x 4 configs
      python 07_harness_configs.py --quick   # 3 cases
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

import harness_world as W  # noqa: E402


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


# ---------------- real MCP servers, one per domain ----------------
MCP_SERVER = r'''
import json, sys, importlib.util
# load harness_world.py from the same directory this server file lives in
spec = importlib.util.spec_from_file_location("harness_world", "harness_world.py")
W = importlib.util.module_from_spec(spec); spec.loader.exec_module(W)
PROTOCOL_VERSION = "2026-07-28"
domain = sys.argv[1]
catalog = W.catalog()
impl = W.IMPL
tools = [{"name": n, "description": catalog[n][0]["function"]["description"],
          "inputSchema": catalog[n][0]["function"]["parameters"]}
         for n in W.DOMAINS[domain]]
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    msg = json.loads(line)
    method = msg.get("method", "")
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                  "serverInfo": {"name": f"csa-{domain}", "version": "1.0.0"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "state/export":
        result = {"world": {"LEDGER": W.LEDGER, "TICKETS": W.TICKETS,
                            "ESCALATIONS": W.ESCALATIONS, "RESERVATIONS": W.RESERVATIONS,
                            "INVENTORY": W.INVENTORY,
                            "ORDER_STATUS": {k: v["status"] for k, v in W.ORDERS.items()},
                            "REFUNDS_30D": {k: v["refunds_30d"] for k, v in W.CUSTOMERS.items()}}}
    elif method == "tools/call":
        p = msg.get("params") or {}
        name = p.get("name")
        args = p.get("arguments") or {}
        fn = impl.get(name)
        if fn is None:
            result = {"content": [{"type": "text", "text": json.dumps({"ok": False, "error": "unknown tool"})}], "isError": True}
        else:
            try:
                out = fn(**args) if not isinstance(fn, tuple) else fn(**args)
            except TypeError as e:
                out = {"ok": False, "error": f"bad arguments: {e}"}
            result = {"content": [{"type": "text", "text": json.dumps(out)}], "isError": not out.get("ok", True)}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": result}), flush=True)
'''


class McpDomain:
    """One real MCP-shaped server process for one domain."""

    def __init__(self, domain: str, root: Path):
        self.domain = domain
        self.p = subprocess.Popen([sys.executable, "-c", MCP_SERVER, domain],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, encoding="utf-8", bufsize=1,
                                  cwd=str(root))
        self.n = 1
        self.init_ms = 0.0
        t0 = time.perf_counter()
        self.rpc("initialize", {"protocolVersion": "2026-07-28", "capabilities": {}})
        self.init_ms = (time.perf_counter() - t0) * 1e3

    def rpc(self, method, params=None):
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method,
                                       "params": params or {}}) + "\n")
        self.p.stdin.flush()
        self.n += 1
        return json.loads(self.p.stdout.readline())["result"]

    def call(self, name, args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})
        return json.loads(r["content"][0]["text"])

    def close(self):
        try:
            self.p.stdin.close(); self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


# ---------------- harness configurations ----------------
class Config:
    def __init__(self, key, label, root):
        self.key, self.label = key, label
        self.catalog = W.catalog()
        self.servers: dict[str, McpDomain] = {}
        self.root = root

    # -- schema surface the model sees on EVERY request --
    def resident_schemas(self):
        raise NotImplementedError

    # -- connection cost paid before the first request --
    def boot(self):
        return 0.0

    # -- execute one tool call the way this config routes it --
    def execute(self, name, args):
        return W.IMPL[name](**args)

    # -- a hook the model can use to reach non-resident tools (C only) --
    def extra_schemas(self):
        return []

    def handle_extra(self, name, args):
        return None

    def shutdown(self):
        for s in self.servers.values():
            s.close()
        self.servers = {}


class FlatNative(Config):
    def resident_schemas(self):
        return [self.catalog[n][0] for n in self.catalog]

    def extra_schemas(self):
        return []

    def handle_extra(self, name, args):
        return None


class GroupedMcp(Config):
    def resident_schemas(self):
        return [self.catalog[n][0] for n in self.catalog]

    def boot(self):
        t0 = time.perf_counter()
        for d in W.DOMAINS:
            self.servers[d] = McpDomain(d, self.root)
        return (time.perf_counter() - t0) * 1e3

    def execute(self, name, args):
        for d, names in W.DOMAINS.items():
            if name in names:
                out = self.servers[d].call(name, args)
                self._sync()
                return out
        out = W.IMPL[name](**args)
        return out

    OWNED = {  # each server owns a partition of the world (like real services)
        "payments": ["LEDGER", "REFUNDS_30D"],
        "support": ["TICKETS", "ESCALATIONS"],
        "orders": ["INVENTORY", "RESERVATIONS", "ORDER_STATUS"],
    }

    def _sync(self):
        """Merge each server's owned partition into the parent oracle.

        Every child forks the whole world at spawn; a naive full-state merge is
        last-child-wins (fresh children wipe mutated siblings). Real microservices
        avoid this by owning a partition — we do the same: each server only
        contributes the fields its domain's tools actually mutate.
        """
        rank = {"delivered": 0, "replacement-reserved": 1, "refunded": 2}
        for d, fields in self.OWNED.items():
            w = self.servers[d].rpc("state/export")["world"]
            # any child may have advanced an order's status (cross-domain mutations);
            # take the most-advanced status seen across children
            for oid, st in w.get("ORDER_STATUS", {}).items():
                if rank.get(st, 0) > rank.get(W.ORDERS[oid]["status"], 0):
                    W.ORDERS[oid]["status"] = st
            if "LEDGER" in fields:
                W.LEDGER[:] = w["LEDGER"]
            if "TICKETS" in fields:
                W.TICKETS[:] = w["TICKETS"]
            if "ESCALATIONS" in fields:
                W.ESCALATIONS[:] = w["ESCALATIONS"]
            if "RESERVATIONS" in fields:
                W.RESERVATIONS[:] = w["RESERVATIONS"]
            if "INVENTORY" in fields:
                W.INVENTORY.update(w["INVENTORY"])
            if "REFUNDS_30D" in fields:
                for cid, n in w["REFUNDS_30D"].items():
                    W.CUSTOMERS[cid]["refunds_30d"] = n


LAZY_META = [
    {"type": "function", "function": {
        "name": "connect_domain",
        "description": ("Connect a tool domain and get its tools loaded into this session: "
                        "'orders' (orders, delivery windows, payments method, inventory, "
                        "replacements), 'payments' (refund engine, refund history, executing "
                        "refunds), 'support' (knowledge base, tickets, supervisor escalations). "
                        "Call this before using any tool of that domain."),
        "parameters": {"type": "object",
                       "properties": {"domain": {"type": "string",
                                                 "enum": list(W.DOMAINS)}},
                       "required": ["domain"]}}},
    {"type": "function", "function": {
        "name": "execute_tool",
        "description": ("Run a tool of a connected domain by name with JSON arguments "
                        "(inspect the connect_domain result for the exact names/schemas)."),
        "parameters": {"type": "object",
                       "properties": {"name": {"type": "string"},
                                      "arguments": {"type": "object"}},
                       "required": ["name", "arguments"]}}},
]


class LazyMcp(Config):
    def __init__(self, *a):
        super().__init__(*a)
        self.connected: dict[str, list] = {}

    def resident_schemas(self):
        return LAZY_META

    def boot(self):
        return 0.0  # servers spawn on first connect (measured in-turn)

    def extra_schemas(self):
        return []  # meta-tools already resident

    def handle_extra(self, name, args):
        if name == "connect_domain":
            d = args["domain"]
            if d not in self.connected:
                t0 = time.perf_counter()
                srv = McpDomain(d, self.root)
                spawn_ms = (time.perf_counter() - t0) * 1e3
                self.servers[d] = srv
                tools = srv.rpc("tools/list")["tools"]
                self.connected[d] = tools
                return {"ok": True, "spawn_ms": round(spawn_ms, 0),
                        "tools": [{t["name"]: t["description"]} for t in tools],
                        "note": "domain connected; call execute_tool with these names"}
            return {"ok": True, "tools": [{t["name"]: t["description"]}
                                          for t in self.connected[d]],
                    "note": "already connected"}
        if name == "execute_tool":
            for d, tools in self.connected.items():
                if args["name"] in [t["name"] for t in tools]:
                    return self.servers[d].call(args["name"], args.get("arguments") or {})
            return {"ok": False, "error": f"tool {args['name']} in no connected domain; "
                                          "connect its domain first"}
        return None

    def shutdown(self):
        super().shutdown()
        self.connected = {}


SKILL_MANUAL = """# Multi-step refund procedure (full)

For any refund request, walk this procedure in order; every step names the tool to call.

1. LOOKUP. `get_order(order_id)`. If it errors, stop and tell the customer.
2. DAMAGE SHORT-CIRCUIT. If the order record says damaged=true:
   `reserve_replacement(order_id)` - if it fails (no stock), use the refund path in step 3
   with FULL amount and code RF-DAMAGED. If it succeeds, `create_ticket(order_id, notes)`
   mentioning the replacement, and STOP: done.
3. ELIGIBILITY. `refund_decision(sku=<sku>, days_since_delivery=<days>, price_usd=<price>,
   opened=<opened>, damaged=<damaged>, refund_target="original", customer_id=<customer_id>)`.
   Apply its decision:
   - DENY → `create_ticket(order_id, notes=note)` and STOP (tell the customer why).
   - ESCALATE → `escalate_supervisor(order_id, reason=note)` and STOP (promise nothing).
   - REVIEW → `create_ticket(order_id, notes="manual review queue")` and STOP.
   - APPROVE → continue.
4. EXECUTE. If full_amount is false, refund decision.amount_usd; otherwise the order price.
   `get_payment_method(order_id)` first - refunds go to the ORIGINAL method, always refuse
   other targets. Then `apply_refund(order_id, amount_usd=<amount>, code=<code>)` and
   `create_ticket(order_id, notes=<code + note>)`. Done.
"""


class SkillCore(Config):
    """5 tools resident (core flow + closure actions) + the procedure as a skill manual."""

    CORE = ["get_order", "refund_decision", "apply_refund", "create_ticket",
            "escalate_supervisor"]

    def __init__(self, *a):
        super().__init__(*a)
        self.loaded = False

    def resident_schemas(self):
        return [self.catalog[n][0] for n in self.CORE]

    def extra_schemas(self):
        return [{"type": "function", "function": {
            "name": "load_skill",
            "description": "Load the step-by-step procedure manual for multi-step refund handling.",
            "parameters": {"type": "object", "properties": {"skill": {"type": "string"}},
                           "required": ["skill"]}}}]

    def handle_extra(self, name, args):
        if name == "load_skill":
            self.loaded = True
            return {"manual": SKILL_MANUAL}
        return None

    def execute(self, name, args):
        return W.IMPL[name](**args)


CONFIGS = [
    ("A flat-native", FlatNative),
    ("B grouped-mcp", GroupedMcp),
    ("C lazy-mcp", LazyMcp),
    ("D skill-core", SkillCore),
]

SYSTEM = ("You are a customer-service agent with real backend tools that MUTATE state. "
          "Work each request to completion (including executing refunds/escalations/tickets "
          "when the procedure says so). Be exact with amounts and codes.")


def run_case(client, model, cfg, ask, max_turns=16):
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": ask}]
    schemas = cfg.resident_schemas() + cfg.extra_schemas()
    llm_ms, hop_ms, extra_ms = [], [], []
    n_tool_calls = 0
    for turn in range(max_turns):
        t0 = time.perf_counter()
        r = client.chat.completions.create(model=model, messages=msgs,
                                           tools=schemas or None, max_tokens=900)
        llm_ms.append((time.perf_counter() - t0) * 1e3)
        m = r.choices[0].message
        if not m.tool_calls:
            usage = r.usage
            return {"turns": turn + 1, "llm_ms": llm_ms, "tool_ms": hop_ms,
                    "extra_ms": extra_ms,
                    "tool_calls": n_tool_calls, "answer": m.content or "",
                    "prompt_tok": usage.prompt_tokens, "out_tok": usage.completion_tokens,
                    "finished": True}
        msgs.append({"role": "assistant", "content": m.content or "",
                     "tool_calls": [tc.model_dump() for tc in m.tool_calls]})
        for tc in m.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            t1 = time.perf_counter()
            extra = cfg.handle_extra(tc.function.name, args)
            if extra is not None:
                out, ms = extra, (time.perf_counter() - t1) * 1e3
                extra_ms.append(ms)
            else:
                out = cfg.execute(tc.function.name, args)
                ms = (time.perf_counter() - t1) * 1e3
                hop_ms.append(ms)
            n_tool_calls += 1
            msgs.append({"role": "tool", "tool_call_id": tc.id,
                         "content": json.dumps(out)})
    return {"turns": max_turns, "llm_ms": llm_ms, "tool_ms": hop_ms,
            "extra_ms": extra_ms,
            "tool_calls": n_tool_calls, "answer": "", "prompt_tok": None,
            "out_tok": None, "finished": False}


def grade(snapshot, oracle) -> dict:
    checks = {}
    code, status, extra = oracle
    # the relevant order's status + the extra predicate, computed from what the case touched
    return checks


def main():
    quick = "--quick" in sys.argv
    env = load_env()
    os.environ["SSL_CERT_FILE"] = env.get("SSL_CERT_FILE", "").strip('"\'') or os.environ.get("SSL_CERT_FILE", "")
    os.environ["REQUESTS_CA_BUNDLE"] = os.environ["SSL_CERT_FILE"]
    client = OpenAI(base_url=env["OPENAI_BASE_URL"], api_key=env["OPENAI_API_KEY"], timeout=120)
    model = env.get("OPENAI_MODEL", "gemini-3.5-flash")
    cases = W.CASES[:3] if quick else W.CASES

    print(f"model: {model} | cases: {len(cases)} | tools in catalog: {len(W.catalog())}\n")
    results = {}
    for key, cls in CONFIGS:
        per_case = []
        boot_ms = None
        n_resident = None
        for i, (ask, order_id, oracle) in enumerate(cases, 1):
            # fresh config per case: servers fork the world at spawn; resetting only the
            # parent would leak previous cases' state into the oracle (the whole point of
            # state partitioning is that children hold it).
            W.reset_world()
            W.TOOL_ERRORS.clear()
            cfg = cls(key, key, ROOT)
            boot_ms = cfg.boot()
            n_resident = len(cfg.resident_schemas())
            r = run_case(client, model, cfg, ask)
            snap = W.oracle_snapshot()
            ok_status = snap["ORDER_STATUS"].get(order_id) == oracle[1]
            ok_extra = oracle[2](snap) if ok_status else False
            per_case.append({
                "case": i, "finished": r["finished"], "turns": r["turns"],
                "tool_calls": r["tool_calls"],
                "llm_ms_median": round(statistics.median(r["llm_ms"]), 0) if r["llm_ms"] else None,
                "total_llm_ms": round(sum(r["llm_ms"]), 0),
                "tool_ms_total": round(sum(r["tool_ms"]), 2),
                "extra_ms": round(sum(r["extra_ms"]), 0),
                "prompt_tok": r["prompt_tok"], "out_tok": r["out_tok"],
                "status_ok": ok_status, "extra_ok": ok_extra,
                "answer_head": r["answer"][:90],
            })
            print(f"[{key}] case {i}: turns {r['turns']:2d} | calls {r['tool_calls']:2d} | "
                  f"llm sum  {per_case[-1]['total_llm_ms']:7.0f} ms | tools sum  {per_case[-1]['tool_ms_total']:6.2f} ms | "
                  f"tok {r['prompt_tok']}+{r['out_tok']} | "
                  f"oracle: status {ok_status} extra {ok_extra}")
            if not r["finished"]:
                print(f"    ! no convergence: {r['answer'][:80]}")
            cfg.shutdown()
        results[key] = {"boot_ms": round(boot_ms, 0), "resident_schemas": n_resident,
                        "cases": per_case}
        print()

    print("=" * 76)
    print(f"{'config':16s} {'schemas':>7s} {'boot ms':>8s} {'sum  turns':>8s} {'sum  calls':>8s} "
          f"{'sum  llm ms':>9s} {'sum  tool ms':>9s} {'oracle':>7s}")
    for key, r in results.items():
        t = r["cases"]
        print(f"{key:16s} {r['resident_schemas']:7d} {r['boot_ms']:8.0f} "
              f"{sum(c['turns'] for c in t):8d} {sum(c['tool_calls'] for c in t):8d} "
              f"{sum(c['total_llm_ms'] or 0 for c in t):9.0f} "
              f"{sum(c['tool_ms_total'] or 0 for c in t):9.2f} "
              f"{sum(1 for c in t if c['status_ok'] and c['extra_ok'])}/{len(t)}")
    json.dump(results, open(ROOT / "harness_results.json", "w", encoding="utf-8"),
              indent=1, ensure_ascii=False)
    print("\nfull per-case data: harness_results.json")


import statistics  # noqa: E402

if __name__ == "__main__":
    main()
