"""Support bot answer endpoint."""
import json
import random
import uuid
from datetime import datetime

from openai import OpenAI

client = OpenAI()
EXAMPLES = ["Q: reset password? A: ...", "Q: change plan? A: ...", "Q: refund? A: ..."]
ALL_TOOLS = [{"type": "function", "name": "lookup_order"}, {"type": "function", "name": "refund"}]


def answer(user, question, kb, effort):
    random.shuffle(EXAMPLES)
    system = f"You are a support agent for {user['name']} on plan {user['plan']}. Today is {datetime.now()}."
    tools = [t for t in ALL_TOOLS if t["name"] in user["allowed"]]
    resp = client.responses.create(
        model="gpt-6.1-sol",
        instructions=system,
        input=[
            {"role": "developer", "content": "Request " + str(uuid.uuid4())},
            {"role": "developer", "content": json.dumps(kb)},
            {"role": "developer", "content": "\n".join(EXAMPLES)},
            {"role": "user", "content": question},
        ],
        tools=tools,
        reasoning={"effort": effort},
        prompt_cache_retention="24h",
    )
    return resp.output_text
