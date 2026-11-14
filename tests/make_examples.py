"""Regenerates the synthetic example logs in examples/. Deterministic; run from the repo root:
    python tests/make_examples.py
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, "examples")


def codex_session(path):
    lines = [
        {"timestamp": "2026-11-10T09:00:00Z", "type": "session_meta", "payload": {"id": "demo", "cwd": "/repo"}},
        {"timestamp": "2026-11-10T09:00:01Z", "type": "turn_context", "payload": {"model": "gpt-6.1-sol", "cwd": "/repo"}},
    ]
    # TokenUsage fields as in codex-rs/protocol. On GPT-5.6+ the uncached tail is written to the
    # cache (implicit mode puts a breakpoint at the end), so it shows up as cache_write_input_tokens.
    ctx, total = 14000, {"input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                         "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0}
    for k in range(30):
        cached = int(ctx * 0.97) if k else 0
        if k == 15:                      # something rewrote the prefix
            cached = 3000
        if k == 23:                      # first request after compaction
            cached = 2000
        out, reas = 600, 900
        last = {"input_tokens": ctx, "cached_input_tokens": cached, "cache_write_input_tokens": ctx - cached,
                "output_tokens": out + reas, "reasoning_output_tokens": reas, "total_tokens": ctx + out + reas}
        for key in total:
            total[key] += last[key]
        lines.append({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": dict(total), "last_token_usage": last, "model_context_window": 272000}}})
        if k == 3:
            lines.append({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": dict(total), "last_token_usage": last}}})       # duplicate event
        call_id = f"call_{k}"
        if k == 3:
            cmd, body = "cat package-lock.json", "x" * 100_000          # ~25k tokens
        elif k == 9:
            cmd, body = "pytest -q", "F" * 24_000                        # ~6k tokens
        else:
            cmd, body = "rg -n handler src", "src/a.py:1: def handler()\n" * 20
        lines.append({"type": "response_item", "payload": {
            "type": "function_call", "name": "shell", "call_id": call_id,
            "arguments": json.dumps({"command": ["bash", "-lc", cmd]})}})
        lines.append({"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": call_id,
            "output": json.dumps({"output": body, "metadata": {"exit_code": 0}})}})
        ctx += len(body) // 4 + out + 200
        if k == 21:
            lines.append({"type": "compacted", "payload": {"message": "summary"}})
            ctx = 20000
    with open(path, "w", encoding="utf-8") as fh:
        for i, l in enumerate(lines):           # every real rollout line carries a timestamp
            ts = l.get("timestamp") or f"2026-11-10T09:{i // 60:02d}:{i % 60:02d}.000Z"
            fh.write(json.dumps({"timestamp": ts, **{k: v for k, v in l.items() if k != "timestamp"}}) + "\n")


def api_log(path):
    rows = []
    # a chat feature with a stable layout: high hit rate
    for i in range(12):
        rows.append({"model": "gpt-6.1-sol", "group": "support-answer", "usage": {
            "input_tokens": 36000 + 300 * i,
            "input_tokens_details": {"cached_tokens": 0 if i == 0 else 35000,
                                     "cache_write_tokens": 36000 if i == 0 else 300,
                                     "cache_write_tokens_details": {"ttl_30m_tokens": 36000 if i == 0 else 300, "ttl_12h_tokens": 0}},
            "output_tokens": 700, "output_tokens_details": {"reasoning_tokens": 300}}})
    # a classifier with a timestamp in the system prompt: nothing caches
    for i in range(12):
        rows.append({"object": "response", "model": "gpt-5.4-mini", "prompt_cache_key": "classify", "usage": {
            "input_tokens": 9000, "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 2400, "output_tokens_details": {"reasoning_tokens": 2300}}})
    # chat completions format
    rows.append({"object": "chat.completion", "model": "gpt-5.4", "usage": {
        "prompt_tokens": 5000, "prompt_tokens_details": {"cached_tokens": 4096},
        "completion_tokens": 400, "completion_tokens_details": {"reasoning_tokens": 0}}})
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    codex_session(os.path.join(EX, "codex-rollout.example.jsonl"))
    api_log(os.path.join(EX, "api-usage.example.jsonl"))
    print("wrote examples/codex-rollout.example.jsonl and examples/api-usage.example.jsonl")
