#!/usr/bin/env python3
"""A runnable Agent Skill (SKILL.md shape) + the model using it — minimal complete pair.

The skill directory lives at skills/refund-policy/ exactly the way the open Agent Skills
standard defines it:

  skills/refund-policy/
    SKILL.md          <- YAML frontmatter (name, description) + body
    policy_table.md   <- linked reference, read on demand

The demo asks the model a refund question WITHOUT the manual, lets it decide to load the
skill (progressive disclosure: description first, body on request, linked file only if
needed), and prints every hop so you can watch the mechanism work.

Run: python 05_skill_demo.py          (needs .env, same as the other benches)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from openai import OpenAI

ROOT = Path(__file__).resolve().parent
SKILL_DIR = ROOT / "skills" / "refund-policy"


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


# ---- the two on-disk files a real skill carries ----
def skill_metadata() -> str:
    """Level 1: what rides in the system prompt (~tens of tokens)."""
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    # frontmatter description only — exactly what a skill runtime pre-loads
    lines = text.splitlines()
    i = lines.index("---", 1)
    for line in lines[1:i]:
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError("SKILL.md missing description")


def skill_body() -> str:
    """Level 2: the SKILL.md body, served when the model asks for the skill."""
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    i = lines.index("---", 1)
    return "\n".join(lines[i + 1:]).strip()


def skill_reference(name: str) -> str | None:
    """Level 3: linked files, read only if the model asks."""
    p = SKILL_DIR / name
    if p.exists():
        return p.read_text(encoding="utf-8")
    return None


REQUEST_SCHEMA = [{
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": ("Load a skill's full body (and optionally one of its linked files) "
                        "into the conversation. Available skills: refund-policy."),
        "parameters": {
            "type": "object",
            "properties": {
                "skill": {"type": "string"},
                "file": {"type": "string",
                         "description": "optional linked file inside the skill"}}},
        "required": ["skill"]}}]


def main():
    env = load_env()
    os.environ["SSL_CERT_FILE"] = env.get("SSL_CERT_FILE", "").strip('"\'') or os.environ.get("SSL_CERT_FILE", "")
    os.environ["REQUESTS_CA_BUNDLE"] = os.environ["SSL_CERT_FILE"]
    client = OpenAI(base_url=env["OPENAI_BASE_URL"], api_key=env["OPENAI_API_KEY"], timeout=90)
    model = env.get("OPENAI_MODEL", "gemini-3.5-flash")

    desc = skill_metadata()
    print(f"skill dir: {SKILL_DIR}")
    print(f"level-1 metadata (resident, {len(desc)//4 if False else len(desc.split())} words): {desc}\n")

    question = ("Customer bought an electronics item (SKU ELEC-771) 12 days after delivery, "
                "box opened, price 189 USD, and wants the money back to a different card "
                "because 'the original card was cancelled'. What do we do? Be exact.")
    messages = [
        {"role": "system", "content":
            "You are a customer-service agent. Installed skills (load one with load_skill "
            "before answering if relevant):\n" + desc},
        {"role": "user", "content": question},
    ]

    for turn in range(5):
        t0 = time.perf_counter()
        r = client.chat.completions.create(model=model, messages=messages,
                                           tools=REQUEST_SCHEMA, max_tokens=1200)
        ms = (time.perf_counter() - t0) * 1e3
        m = r.choices[0].message
        print(f"turn {turn+1}: {r.usage.prompt_tokens} prompt tok | {ms:.0f} ms | "
              f"tool_call: {bool(m.tool_calls)} | finish: {r.choices[0].finish_reason}")
        if not m.tool_calls:
            print("\nFINAL ANSWER:\n" + (m.content or "")[:800])
            print(f"\n(total context cost: {sum(1 for _ in [])}{r.usage.prompt_tokens} tok on the last request; "
                  "the manual was paid ONCE, on the turn that loaded it)")
            return
        call = m.tool_calls[0]
        args = json.loads(call.function.arguments or "{}")
        messages.append({"role": "assistant", "content": m.content or "",
                         "tool_calls": [tc.model_dump() for tc in m.tool_calls]})
        if args.get("file"):
            body = skill_reference(args["file"]) or "(file not found)"
            print(f"  -> load_skill({args['skill']!r}, file={args['file']!r}): {len(body)} chars served")
        else:
            body = skill_body() if args.get("skill") == "refund-policy" else "(unknown skill)"
            print(f"  -> load_skill({args.get('skill')!r}): {len(body)} chars served")
        messages.append({"role": "tool", "tool_call_id": call.id, "content": body})
    print("model never converged; increase max turns")


if __name__ == "__main__":
    main()
