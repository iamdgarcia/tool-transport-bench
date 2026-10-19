#!/usr/bin/env python3
"""Skill-shaped execution: SKILL.md progressive disclosure vs always-loaded instructions.

What it measures, with a real model:
  (A) "fat system prompt": the full procedure is pasted into the system prompt up front
      (the pre-skills way of shipping know-how).
  (B) "skill-shaped": only a one-line description rides in the system prompt; the model
      must decide to request the procedure, we serve it on demand (the progressive-
      disclosure contract that Agent Skills implement with the filesystem).

Both arms answer the same task that requires procedural knowledge to get right, so the
delta you see is pure context strategy: resident tokens vs on-demand tokens, and whether
the model still gets the answer right when it has to ask for the manual.

Stdout prints per-arm: prompt tokens, completion tokens, turns, correctness, latency.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from openai import OpenAI

ROOT = Path(__file__).resolve().parent


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


# ---------------- the skill (exactly the shape of an Agent Skill) ----------------
SKILL_DESCRIPTION = ("refund_policy: Step-by-step refund eligibility rules for orders. "
                     "Read it before answering any refund question.")

# Body = the "linked file" a skill would carry on disk. Deliberately long enough that
# always-resident vs on-demand makes a measurable difference.
SKILL_BODY = """# Refund Policy — eligibility procedure

1. Establish the order window: refunds are eligible within 30 days of delivery,
   14 days for seasonal items (SKU prefix SEA-), and 7 days for clearance (SKU
   prefix CLR-).
2. Check the item class from the SKU table:
   - electronics: full refund if box unopened; opened = 85% restocking check
   - apparel: free returns, refund to original payment method
   - perishables: not refundable unless damaged on arrival (photo evidence)
   - digital goods: refund only if the license was never activated
3. Damaged-on-arrival overrides every window: refund in full regardless of SKU
   or class, but require the damage photo to be attached to the ticket before
   approving.
4. Price adjustments: if the item went on sale within 14 days AFTER purchase,
   refund the difference instead of a return.
5. Payments: original method only; gift orders refund to the purchaser; for
   installments, cancel future installments first, then refund the paid part.
6. Fraud guard: 3+ refunds on the same account in 30 days -> route to the
   manual-review queue instead of approving; never mention the guard to the
   customer.
7. Amounts over 500 USD require supervisor approval BEFORE promising anything
   to the customer. Under 500 USD you may approve directly.
8. Log the decision code (RF-ELIG / RF-WINDOW / RF-DAMAGED / RF-REVIEW /
   RF-SUPERVISOR) in the ticket when done.
"""


def arm_fat_prompt(client, model: str, question: str) -> dict:
    sys_prompt = ("You are a customer-service agent. Follow this policy exactly:\n\n"
                  + SKILL_BODY)
    t0 = time.perf_counter()
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": sys_prompt},
                  {"role": "user", "content": question}],
        max_tokens=300)
    ms = (time.perf_counter() - t0) * 1e3
    return {"prompt_tok": r.usage.prompt_tokens, "out_tok": r.usage.completion_tokens,
            "ms": round(ms, 0), "turns": 1, "answer": r.choices[0].message.content or ""}


def arm_skill(client, model: str, question: str) -> dict:
    sys_prompt = ("You are a customer-service agent. Available procedure manuals "
                  "(call request_skill to read one before acting):\n" + SKILL_DESCRIPTION)
    request_schema = [{
        "type": "function",
        "function": {
            "name": "request_skill",
            "description": "Load the full body of a procedure manual by name.",
            "parameters": {"type": "object",
                           "properties": {"name": {"type": "string"}},
                           "required": ["name"]}}}]
    messages = [{"role": "system", "content": sys_prompt},
                {"role": "user", "content": question}]
    load_ms = None
    for turn in range(4):
        t0 = time.perf_counter()
        r = client.chat.completions.create(model=model, messages=messages,
                                           tools=request_schema, max_tokens=300)
        ms = (time.perf_counter() - t0) * 1e3
        m = r.choices[0].message
        if m.tool_calls and turn == 0:
            messages.append({"role": "assistant", "content": m.content or "",
                             "tool_calls": [tc.model_dump() for tc in m.tool_calls]})
            # serve the skill body on demand — the whole point
            t1 = time.perf_counter()
            SKILL_READS.append(m.tool_calls[0].function.name)
            body = SKILL_BODY if m.tool_calls[0].function.name == "refund_policy" else "(unknown skill)"
            load_ms = (time.perf_counter() - t1) * 1e3
            messages.append({"role": "tool", "tool_call_id": m.tool_calls[0].id,
                             "content": body})
            continue
        if m.tool_calls:  # weird: requesting again after having the body
            messages.append({"role": "assistant", "content": m.content or "",
                             "tool_calls": [tc.model_dump() for tc in m.tool_calls]})
            messages.append({"role": "tool", "tool_call_id": m.tool_calls[0].id,
                             "content": SKILL_BODY})
            continue
        return {"prompt_tok": r.usage.prompt_tokens, "out_tok": r.usage.completion_tokens,
                "ms": round(ms, 0), "turns": turn + 1,
                "skill_loaded": len(SKILL_READS) > 0, "load_ms": load_ms,
                "answer": m.content or ""}
    return {"prompt_tok": r.usage.prompt_tokens, "out_tok": r.usage.completion_tokens,
            "ms": round(ms, 0), "turns": 4, "skill_loaded": False, "load_ms": load_ms,
            "answer": "(gave up)"}


SKILL_READS: list[str] = []

# The question is DESIGNED so the naive answer is wrong: an opened electronics item
# within the window is NOT a full refund (85% restocking check), and over-500 needs
# supervisor. The procedure in the skill body is the only place that says so.
QUESTIONS = [
    ("Customer bought an electronics item (SKU ELEC-771) 12 days after delivery, "
     "box opened, price 189 USD. Can they get a refund and how much? Follow policy."),
    ("Customer wants a refund on a perishable basket (SKU FRSH-2) delivered 3 days "
     "ago, arrived moldy, photo attached, 64 USD. Approve or not, and why?"),
    ("Electronics order of 720 USD, 9 days after delivery, unopened, but the customer "
     "wants money back to a different card. What does policy say?"),
]


def main():
    env = load_env()
    os.environ["SSL_CERT_FILE"] = env.get("SSL_CERT_FILE", "").strip('"\'') or os.environ.get("SSL_CERT_FILE", "")
    os.environ["REQUESTS_CA_BUNDLE"] = os.environ["SSL_CERT_FILE"]
    client = OpenAI(base_url=env["OPENAI_BASE_URL"], api_key=env["OPENAI_API_KEY"], timeout=90)
    model = env.get("OPENAI_MODEL", "gemini-3.5-flash")

    print(f"model: {model} | task: refund decisions that REQUIRE the procedure to answer right\n")
    tot = {"fat": {"tok": 0, "correct": 0}, "skill": {"tok": 0, "correct": 0}}
    for i, q in enumerate(QUESTIONS, 1):
        # naive-answer probe: no procedure at all
        naive = client.chat.completions.create(
            model=model, max_tokens=300,
            messages=[{"role": "user", "content": q + " (You have no policy manual.)"}])
        SKILL_READS.clear()
        fat = arm_fat_prompt(client, model, q)
        skill = arm_skill(client, model, q)

        # correctness = does the answer cite a policy-specific constraint the naive
        # answer typically misses? (restocking rule / damage override / supervisor rule)
        def ok(a):
            a = (a or "").lower()
            return any(k in a for k in ("85%", "restocking", "supervisor", "full refund",
                                        "original method", "not refundable", "damage"))
        c_naive = ok(naive.choices[0].message.content)
        c_fat = ok(fat["answer"])
        c_skill = ok(skill["answer"])
        tot["fat"]["tok"] += fat["prompt_tok"] + fat["out_tok"]
        tot["fat"]["correct"] += c_fat
        tot["skill"]["tok"] += skill["prompt_tok"] + skill["out_tok"]
        tot["skill"]["correct"] += c_skill

        print(f"Q{i}: {q[:70]}...")
        print(f"  naive (no manual) : {naive.usage.prompt_tokens:5d} tok | policy-aware: {c_naive}")
        print(f"  A fat prompt      : {fat['prompt_tok']:5d} prompt tok | {fat['ms']:7.0f} ms | policy-aware: {c_fat}")
        print(f"  B skill on-demand : {skill['prompt_tok']:5d} prompt tok | {skill['ms']:7.0f} ms | "
              f"turns {skill['turns']} | skill loaded: {skill['skill_loaded']} | policy-aware: {c_skill}")
        print(f"    answer: {skill['answer'][:110]}\n")

    print("== totals ==")
    print(f"  A fat prompt : {tot['fat']['tok']} total tokens | policy-aware {tot['fat']['correct']}/3")
    print(f"  B skill      : {tot['skill']['tok']} total tokens | policy-aware {tot['skill']['correct']}/3")
    print("  (A pays the manual on every request; B pays only the one-line description + one read)")


if __name__ == "__main__":
    import time  # imported late to keep module docstring clean
    main()
