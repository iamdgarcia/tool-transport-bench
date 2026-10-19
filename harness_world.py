"""Shared world state + real tool logic for the harness-configuration benches.

A small customer-service backend with REAL state, mutations, and cross-checks:
orders, customers, inventory, refund ledger, tickets, escalations. Tools execute against
this world; correctness of a case = the world's final state vs an oracle, not text match.

Every tool function returns (ok, payload) and never raises — errors are data.
"""
from __future__ import annotations

import copy

# ---------------- the world (deterministic) ----------------
CUSTOMERS = {
    "C-100": {"tier": "gold", "refunds_30d": 0},
    "C-200": {"tier": "standard", "refunds_30d": 3},   # fraud-guard territory
    "C-300": {"tier": "standard", "refunds_30d": 0},
}
ORDERS = {
    "O-9001": {"customer_id": "C-100", "sku": "ELEC-771", "price_usd": 189.0,
               "days_since_delivery": 12, "opened": True, "damaged": False,
               "status": "delivered", "payment": "visa-1111"},
    "O-9002": {"customer_id": "C-300", "sku": "FRSH-2", "price_usd": 64.0,
               "days_since_delivery": 3, "opened": False, "damaged": True,
               "status": "delivered", "payment": "visa-2222"},
    "O-9003": {"customer_id": "C-300", "sku": "ELEC-990", "price_usd": 720.0,
               "days_since_delivery": 9, "opened": False, "damaged": False,
               "status": "delivered", "payment": "visa-3333"},
    "O-9004": {"customer_id": "C-200", "sku": "APP-410", "price_usd": 85.0,
               "days_since_delivery": 20, "opened": True, "damaged": False,
               "status": "delivered", "payment": "visa-4444"},
    "O-9005": {"customer_id": "C-100", "sku": "CLR-77", "price_usd": 40.0,
               "days_since_delivery": 10, "opened": False, "damaged": False,
               "status": "delivered", "payment": "visa-5555"},
    "O-9006": {"customer_id": "C-300", "sku": "DIGI-5", "price_usd": 29.0,
               "days_since_delivery": 5, "opened": False, "damaged": False,
               "status": "delivered", "payment": "visa-6666", "license_activated": False},
}
INVENTORY = {"ELEC-771": 4, "FRSH-2": 0, "ELEC-990": 2, "APP-410": 9, "CLR-77": 1, "DIGI-5": 100}
LEDGER: list[dict] = []      # refund ledger
TICKETS: list[dict] = []     # created tickets
ESCALATIONS: list[dict] = [] # supervisor escalations
RESERVATIONS: list[dict] = []# replacement reservations


def reset_world():
    global LEDGER, TICKETS, ESCALATIONS, RESERVATIONS
    for o in ORDERS.values():
        o["status"] = "delivered"
    LEDGER, TICKETS, ESCALATIONS, RESERVATIONS = [], [], [], []


# ---------------- the engines (real logic, shared by every config) ----------------
def _order_for_sku(sku: str) -> str:
    for oid, o in ORDERS.items():
        if o["sku"].upper() == sku.upper():
            return oid
    return ""


def refund_decision(sku: str, days_since_delivery: int, price_usd: float, opened: bool,
                    damaged: bool, refund_target: str, customer_id: str = "") -> dict:
    """The policy engine: windows, classes, fraud guard, supervisor rule."""
    sku = (sku or "").upper()
    guard = CUSTOMERS.get(customer_id, {}).get("refunds_30d", 0)
    if damaged:
        return {"decision": "APPROVE", "code": "RF-DAMAGED", "full_amount": True,
                "note": "damaged-on-arrival overrides every window; photo evidence required",
                "conditions": ["attach damage photo to ticket", "reserve replacement if stock > 0"]}
    if target := refund_target:
        if target == "other":
            return {"decision": "DENY", "code": "RF-ORIGINAL-METHOD",
                    "note": "refunds go to the ORIGINAL payment method only; offer original-method refund"}
        if target == "gift":
            return {"decision": "APPROVE", "code": "RF-ELIG",
                    "note": "gift orders refund to the purchaser", "full_amount": True}
    if guard >= 3:
        return {"decision": "REVIEW", "code": "RF-REVIEW",
                "note": "3+ refunds in 30 days: route to manual-review queue (do not mention the guard)"}
    if sku.startswith("CLR-") and days_since_delivery > 7:
        return {"decision": "DENY", "code": "RF-WINDOW", "note": "clearance: 7-day window"}
    if sku.startswith("SEA-") and days_since_delivery > 14:
        return {"decision": "DENY", "code": "RF-WINDOW", "note": "seasonal: 14-day window"}
    if sku.startswith("FRSH-"):
        return {"decision": "DENY", "code": "RF-WINDOW", "note": "perishables: not refundable unless damaged"}
    if days_since_delivery > 30:
        return {"decision": "DENY", "code": "RF-WINDOW", "note": "30-day window exceeded"}
    if sku.startswith("ELEC"):
        if opened and price_usd > 500:
            return {"decision": "ESCALATE", "code": "RF-SUPERVISOR",
                    "note": "opened electronics over 500 USD: supervisor approval before promising anything"}
        if opened:
            return {"decision": "APPROVE", "code": "RF-ELIG", "full_amount": False,
                    "amount_usd": round(price_usd * 0.85, 2),
                    "note": "opened electronics: 85% of price (15% restocking)"}
    if price_usd > 500:
        return {"decision": "ESCALATE", "code": "RF-SUPERVISOR",
                "note": "amount over 500 USD: supervisor approval required"}
    if sku.startswith("DIGI-"):
        activated = ORDERS.get(_order_for_sku(sku), {}).get("license_activated", None)
        if activated is False:
            return {"decision": "APPROVE", "code": "RF-ELIG", "full_amount": True,
                    "amount_usd": price_usd,
                    "note": "unactivated digital license: standard refund"}
        return {"decision": "DENY", "code": "RF-LICENSE",
                "note": ("DENY: digital goods are refundable ONLY if the license was never "
                         "activated; this order's activation status was not provided. "
                         "Do not refund; check the license first.")}
    return {"decision": "APPROVE", "code": "RF-ELIG", "full_amount": True,
            "amount_usd": price_usd, "note": "within window, standard rules"}


TOOL_ERRORS = []  # every tool records its errors here for the oracle


def _err(msg):  # errors are data, not exceptions
    TOOL_ERRORS.append(msg)
    return {"ok": False, "error": msg}


# ---- orders domain ----
def get_order(order_id: str) -> dict:
    o = ORDERS.get(order_id)
    if not o:
        return _err(f"order {order_id} not found")
    return {"ok": True, **o}


def check_delivery_window(order_id: str) -> dict:
    o = ORDERS.get(order_id)
    if not o:
        return _err(f"order {order_id} not found")
    sku = o["sku"]
    limit = 7 if sku.startswith("CLR-") else 14 if sku.startswith("SEA-") else 30
    return {"ok": True, "days": o["days_since_delivery"], "window_days": limit,
            "within_window": o["days_since_delivery"] <= limit}


def get_payment_method(order_id: str) -> dict:
    o = ORDERS.get(order_id)
    if not o:
        return _err(f"order {order_id} not found")
    return {"ok": True, "original_method": o["payment"],
            "note": "refunds must go to this method"}


# ---- inventory domain ----
def check_inventory(sku: str) -> dict:
    return {"ok": True, "sku": sku, "stock": INVENTORY.get(sku.upper(), 0)}


def reserve_replacement(order_id: str) -> dict:
    o = ORDERS.get(order_id)
    if not o:
        return _err(f"order {order_id} not found")
    sku = o["sku"]
    if INVENTORY.get(sku, 0) <= 0:
        return {"ok": False, "error": f"no stock for {sku}; offer refund instead"}
    INVENTORY[sku] -= 1
    RESERVATIONS.append({"order_id": order_id, "sku": sku})
    o["status"] = "replacement-reserved"
    return {"ok": True, "reserved": True, "sku": sku, "remaining_stock": INVENTORY[sku]}


# ---- payments domain ----
def get_refund_history(customer_id: str) -> dict:
    c = CUSTOMERS.get(customer_id)
    if not c:
        return _err(f"customer {customer_id} not found")
    return {"ok": True, "refunds_30d": c["refunds_30d"],
            "fraud_guard": c["refunds_30d"] >= 3}


REFUNDABLE_CODES = {"RF-ELIG", "RF-DAMAGED"}  # only these may move money


def apply_refund(order_id: str, amount_usd: float, code: str) -> dict:
    o = ORDERS.get(order_id)
    if not o:
        return _err(f"order {order_id} not found")
    if code not in REFUNDABLE_CODES:
        return {"ok": False,
                "error": (f"code {code} is not an approval code ({sorted(REFUNDABLE_CODES)}); "
                          f"a DENY/REVIEW decision must not be executed as a refund")}
    if o["status"] not in ("delivered", "replacement-reserved"):
        return {"ok": False, "error": f"order status {o['status']}: refund already in flight"}
    LEDGER.append({"order_id": order_id, "amount_usd": amount_usd, "code": code,
                   "customer_id": o["customer_id"]})
    c = CUSTOMERS.get(o["customer_id"])
    if c:
        c["refunds_30d"] += 1
    o["status"] = "refunded"
    return {"ok": True, "refunded_usd": amount_usd, "code": code,
            "method": o["payment"]}


# ---- kb / support domain ----
KB = {
    "refund": "Refunds: 30-day window (14 seasonal, 7 clearance). Opened electronics = 85% of price. "
              "Damaged-on-arrival overrides everything (full refund + photo). Over 500 USD = supervisor. "
              "Original payment method only. Digital: unactivated license only.",
    "fraud": "Accounts with 3+ refunds in 30 days go to the manual-review queue. Never mention the guard.",
    "replacement": "Damaged items get a replacement reserved when stock > 0; otherwise full refund.",
    "escalation": "Escalate to supervisor BEFORE promising anything on amounts over 500 USD.",
    "hours": "Support hours 9-19 CET.",
}
def search_kb(query: str) -> dict:
    q = (query or "").lower()
    best = max(KB, key=lambda k: sum(w in q for w in k.split()) + (1 if k in q else 0))
    return {"ok": True, "topic": best, "text": KB[best]}


def create_ticket(order_id: str, notes: str) -> dict:
    if order_id not in ORDERS:
        return _err(f"order {order_id} not found")
    tid = f"T-{1000 + len(TICKETS)}"
    TICKETS.append({"id": tid, "order_id": order_id, "notes": notes})
    return {"ok": True, "ticket_id": tid}


def escalate_supervisor(order_id: str, reason: str) -> dict:
    if order_id not in ORDERS:
        return _err(f"order {order_id} not found")
    eid = f"E-{100 + len(ESCALATIONS)}"
    ESCALATIONS.append({"id": eid, "order_id": order_id, "reason": reason})
    return {"ok": True, "escalation_id": eid,
            "note": "supervisor will review; do NOT promise the outcome to the customer"}


# ---------------- tool catalog: name -> (schema, impl, domain) ----------------
def catalog() -> dict:
    P = lambda props, req: {"type": "object", "properties": props, "required": req}
    S = lambda name, desc, params: ({"type": "function", "function": {
        "name": name, "description": desc, "parameters": params}}, name.split("_")[0])
    c = {}
    c["get_order"] = S("get_order", "Fetch one order's full record (sku, price, days since delivery, opened, damaged, status, payment).", P({"order_id": {"type": "string"}}, ["order_id"]))
    c["check_delivery_window"] = S("check_delivery_window", "Compute whether an order is inside its refund window (window depends on SKU class).", P({"order_id": {"type": "string"}}, ["order_id"]))
    c["get_payment_method"] = S("get_payment_method", "Get the ORIGINAL payment method of an order (refunds must go there).", P({"order_id": {"type": "string"}}, ["order_id"]))
    c["check_inventory"] = S("check_inventory", "Current stock count for a SKU.", P({"sku": {"type": "string"}}, ["sku"]))
    c["reserve_replacement"] = S("reserve_replacement", "Reserve a replacement unit for a damaged item (fails if no stock). Mutates inventory.", P({"order_id": {"type": "string"}}, ["order_id"]))
    c["get_refund_history"] = S("get_refund_history", "Customer's refund count in the last 30 days + whether the fraud guard triggers (3+).", P({"customer_id": {"type": "string"}}, ["customer_id"]))
    c["apply_refund"] = S("apply_refund", "Execute an approved refund: records it in the ledger, increments the customer's refund count, marks the order refunded. Mutates state.", P({"order_id": {"type": "string"}, "amount_usd": {"type": "number"}, "code": {"type": "string"}}, ["order_id", "amount_usd", "code"]))
    c["search_kb"] = S("search_kb", "Search the internal knowledge base (policy paragraphs by topic).", P({"query": {"type": "string"}}, ["query"]))
    c["create_ticket"] = S("create_ticket", "Create a support ticket for an order with decision notes. Mutates tickets.", P({"order_id": {"type": "string"}, "notes": {"type": "string"}}, ["order_id", "notes"]))
    c["escalate_supervisor"] = S("escalate_supervisor", "Open a supervisor escalation for an order. Use BEFORE promising outcomes over 500 USD. Mutates escalations.", P({"order_id": {"type": "string"}, "reason": {"type": "string"}}, ["order_id", "reason"]))
    c["refund_decision"] = S("refund_decision", "Deterministic refund-eligibility engine: windows, SKU classes, fraud guard, supervisor rule. Provide sku, days_since_delivery, price_usd, opened, damaged, refund_target (original|gift|other), customer_id.", P({"sku": {"type": "string"}, "days_since_delivery": {"type": "integer"}, "price_usd": {"type": "number"}, "opened": {"type": "boolean"}, "damaged": {"type": "boolean"}, "refund_target": {"type": "string", "enum": ["original", "gift", "other"]}, "customer_id": {"type": "string"}}, ["sku", "days_since_delivery", "price_usd", "opened", "damaged", "refund_target", "customer_id"]))
    return c


IMPL = {
    "get_order": get_order, "check_delivery_window": check_delivery_window,
    "get_payment_method": get_payment_method, "check_inventory": check_inventory,
    "reserve_replacement": reserve_replacement, "get_refund_history": get_refund_history,
    "apply_refund": apply_refund, "search_kb": search_kb, "create_ticket": create_ticket,
    "escalate_supervisor": escalate_supervisor,
    "refund_decision": lambda **a: refund_decision(**a),
}

DOMAINS = {
    "orders": ["get_order", "check_delivery_window", "get_payment_method",
               "reserve_replacement", "check_inventory"],
    "payments": ["refund_decision", "get_refund_history", "apply_refund"],
    "support": ["search_kb", "create_ticket", "escalate_supervisor"],
}

# ---------------- cases + oracle ----------------
CASES = [
    # (user ask, order_id, oracle: (decision_code, order_status, extra checks))
    ("What's the status of order O-9001, and can its customer get a refund? "
     "If eligible, apply it and open a ticket with the decision.",
     "O-9001", ("RF-ELIG", "refunded",
                lambda w: len(w["LEDGER"]) == 1 and w["LEDGER"][0]["amount_usd"] == 189.0 * 0.85)),
    # NOTE: FRSH-2 stock is 0 on purpose: the reserve fails, so correct behavior =
    # full refund with RF-DAMAGED (the damage override), never a replacement.
    ("Order O-9002 arrived moldy and the customer attached a photo. Handle it end to end: "
     "reserve a replacement if possible (check stock), otherwise refund in full, and log a ticket.",
     "O-9002", ("RF-DAMAGED", "refunded",
                lambda w: len(w["LEDGER"]) == 1 and w["LEDGER"][0]["amount_usd"] == 64.0
                and len(w["RESERVATIONS"]) == 0)),
    ("Order O-9003: customer wants their 720 USD back to a different card. Handle per policy.",
     "O-9003", ("RF-ORIGINAL-METHOD", "delivered",
                lambda w: len(w["LEDGER"]) == 0)),
    ("Order O-9004: customer wants a refund. Check their history first and follow policy "
     "exactly. If a guard applies, create a ticket instead of refunding.",
     "O-9004", ("RF-REVIEW", "delivered",
                lambda w: len(w["TICKETS"]) >= 1 and len(w["LEDGER"]) == 0)),
    ("Order O-9005 (clearance). Can they still return it? Follow policy; if not eligible, "
     "just open a ticket explaining why.",
     "O-9005", ("RF-WINDOW", "delivered",
                lambda w: len(w["LEDGER"]) == 0 and len(w["TICKETS"]) >= 1)),
    ("Order O-9006 is a digital good. The customer says they never activated it. Handle it.",
     "O-9006", ("RF-ELIG", "refunded",
                lambda w: len(w["LEDGER"]) == 1 and w["LEDGER"][0]["amount_usd"] == 29.0)),
]


def oracle_snapshot() -> dict:
    return {"LEDGER": copy.deepcopy(LEDGER), "TICKETS": copy.deepcopy(TICKETS),
            "ESCALATIONS": copy.deepcopy(ESCALATIONS),
            "RESERVATIONS": copy.deepcopy(RESERVATIONS),
            "INVENTORY": dict(INVENTORY),
            "ORDER_STATUS": {k: v["status"] for k, v in ORDERS.items()}}
