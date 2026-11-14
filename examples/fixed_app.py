"""The leaky_app.py example rearranged by lifetime cost: stable, widely shared blocks first,
per-user and per-request facts last, one fixed tools array, deterministic serialisation."""
import json
from datetime import date

from openai import OpenAI

client = OpenAI()
SYSTEM = "You are a support agent. Follow the policy below. Answer in at most 5 sentences."
EXAMPLES = "\n".join(sorted(["Q: change plan? A: ...", "Q: refund? A: ...", "Q: reset password? A: ..."]))
TOOLS = sorted([{"type": "function", "name": "lookup_order"}, {"type": "function", "name": "refund"}],
               key=lambda t: t["name"])


def answer(user, question, kb):
    kb_text = json.dumps(kb, sort_keys=True, separators=(",", ":"))
    facts = f"Customer plan: {user['plan']}. Allowed tools: {', '.join(sorted(user['allowed']))}. Date: {date.today()}"
    resp = client.responses.create(
        model="gpt-6.1-sol",
        instructions=SYSTEM,                                   # identical for all traffic
        input=[
            {"role": "developer", "content": EXAMPLES},       # changes on deploy
            {"role": "developer", "content": kb_text},        # changes a few times a day
            {"role": "user", "content": facts + "\n\n" + question},   # per request: last
        ],
        tools=TOOLS,                                           # same array every time
        reasoning={"effort": "low"},
        text={"verbosity": "low"},
        prompt_cache_options={"ttl": "30m"},
        prompt_cache_key="support-answer-v3",
    )
    return resp.output_text
