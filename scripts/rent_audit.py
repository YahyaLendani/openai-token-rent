#!/usr/bin/env python3
"""rent_audit: split real OpenAI spend into entry, rent, rent hikes, reasoning and output.

Reads JSONL. Each line may be:
  - a Responses API response or Chat Completions response (anything with "usage" + "model"),
  - a line from the usage meter in references/api-playbook.md: {"model", "usage", "group"},
  - a Codex CLI session rollout line (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl).
The format is detected per file. Codex files are one conversation each, so the audit also finds
rent hikes (cache misses where the previous prefix should have been reused) and the biggest
tenants (tool outputs that sat in context, paying rent, for the rest of the session).

  python rent_audit.py usage.jsonl [more.jsonl ...] [--model gpt-6.1-sol] [--json]
  python rent_audit.py --codex-latest          # newest Codex session under ~/.codex/sessions
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rentlib as rl  # noqa: E402

TOOL_OUTPUT_TYPES = {"function_call_output", "custom_tool_call_output", "local_shell_call_output",
                     "tool_call_output", "mcp_tool_call_output", "tool_search_output"}
TOOL_CALL_TYPES = {"function_call", "custom_tool_call", "local_shell_call", "mcp_tool_call",
                   "tool_search_call"}
IMAGE_TOKENS = 1000      # rough flat estimate for an image content item; base64 length is not tokens


# --------------------------------------------------------------------------- parsing

def _is_codex(obj) -> bool:
    return isinstance(obj, dict) and "payload" in obj and "type" in obj


def _text_of(output) -> str:
    if isinstance(output, str):
        try:                                    # Codex wraps shell output as a JSON string
            inner = json.loads(output)
            if isinstance(inner, dict) and isinstance(inner.get("output"), str):
                return inner["output"]
        except (ValueError, TypeError):
            pass
        return output
    if isinstance(output, dict):
        t = output.get("type")
        if t in ("input_image", "input_audio", "encrypted_content"):
            return ""                           # counted separately or opaque; see _media_tokens
        for k in ("output", "content", "text"):
            if k in output:
                return _text_of(output[k])
        return json.dumps(output)
    if isinstance(output, list):
        return "\n".join(_text_of(x) for x in output)
    return ""


def _media_tokens(output) -> int:
    """Flat estimate for image items in a tool output (Codex stores them as content items)."""
    if isinstance(output, dict):
        return IMAGE_TOKENS if output.get("type") == "input_image" else sum(
            _media_tokens(output[k]) for k in ("output", "content") if k in output)
    if isinstance(output, list):
        return sum(_media_tokens(x) for x in output)
    return 0


def _label(call) -> str:
    if not call:
        return "tool output"
    name = call.get("name") or call.get("type", "tool")
    args = call.get("arguments") or call.get("input") or call.get("action") or ""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            pass
    if isinstance(args, dict):
        cmd = args.get("command") or args.get("cmd") or args.get("path") or args
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd[-1:]) if len(cmd) > 2 and cmd[0] in ("bash", "sh", "zsh", "pwsh", "powershell") else " ".join(map(str, cmd))
        args = cmd
    s = f"{name}: {args}" if args else str(name)
    return " ".join(s.split())[:90]


def parse_codex(lines, default_model):
    """Returns (model, requests: [Usage], tenants: [dict], compactions: [request_index])."""
    model = default_model
    reqs, tenants, compactions = [], [], []
    calls = {}
    last_total = None
    for obj in lines:
        typ, p = obj.get("type"), obj.get("payload") or {}
        ptype = p.get("type") if isinstance(p, dict) else None
        if typ in ("turn_context", "session_meta") and isinstance(p, dict) and isinstance(p.get("model"), str):
            model = p["model"]
        if typ == "compacted" or (ptype and "compact" in str(ptype)):
            compactions.append(len(reqs))
        if typ == "response_item" and isinstance(p, dict):
            if ptype in TOOL_CALL_TYPES and p.get("call_id"):
                calls[p["call_id"]] = p
            elif ptype in TOOL_OUTPUT_TYPES:
                text = _text_of(p.get("output"))
                tenants.append({"admitted_at": len(reqs),
                                "tokens": rl.est_tokens(text) + _media_tokens(p.get("output")),
                                "label": _label(calls.get(p.get("call_id")))})
        if typ == "event_msg" and ptype == "token_count":
            info = p.get("info") or {}
            last, total = info.get("last_token_usage"), info.get("total_token_usage")
            tkey = json.dumps(total, sort_keys=True) if total else None
            if tkey and tkey == last_total:
                continue                        # duplicate event (e.g. a rate-limit refresh)
            last_total = tkey or last_total
            u = rl.normalize_usage(last or {}, model)
            if u and u.input:
                u.index = len(reqs)
                reqs.append(u)
    for u in reqs:
        u.model = model if not u.model or u.model == default_model else u.model
    return model, reqs, tenants, compactions


def parse_api(objs, default_model):
    groups = OrderedDict()
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        body = obj.get("response") if isinstance(obj.get("response"), dict) else obj
        usage = body.get("usage") or obj.get("usage")
        model = obj.get("model") or body.get("model") or default_model
        u = rl.normalize_usage(usage, model)
        if not u:
            continue
        g = obj.get("group") or body.get("prompt_cache_key") or obj.get("prompt_cache_key") or "all"
        u.group = str(g)
        u.meta["chained"] = bool(obj.get("group") or body.get("previous_response_id"))
        u.index = len(groups.setdefault(u.group, []))
        groups[u.group].append(u)
    return groups


# --------------------------------------------------------------------------- analysis

def find_hikes(reqs, pricing, compactions=()):
    hikes = []
    comp = set(compactions)
    for k in range(1, len(reqs)):
        prev, cur = reqs[k - 1], reqs[k]
        expected = min(prev.input, cur.input)
        if expected < 1024 or cur.cached >= 0.5 * expected:
            continue
        r = rl.rates_for(pricing, cur.model, cur.input)
        lost = (expected - cur.cached) * (r.p_in * rl.entry_mult(r) - r.p_cached)
        hikes.append({"request": k, "expected_cached": expected, "cached": cur.cached,
                      "usd_lost": max(0.0, lost), "after_compaction": k in comp or (k - 1) in comp})
    return hikes


def price_tenants(tenants, reqs, pricing, compactions):
    if not reqs:
        return []
    out = []
    comps = sorted(compactions)
    for t in tenants:
        start = t["admitted_at"]
        end = next((c for c in comps if c > start), len(reqs))
        turns = max(0, end - start - 1)
        u = reqs[min(start, len(reqs) - 1)]
        r = rl.rates_for(pricing, u.model, u.input)
        out.append({**t, "turns_in_context": turns, "usd": rl.lifetime_cost(t["tokens"], r, turns)})
    out.sort(key=lambda x: -x["usd"])
    return out


def summarize(reqs, pricing, tier):
    c = rl.Cost()
    tot = {"input": 0, "cached": 0, "written": 0, "output": 0, "reasoning": 0}
    for u in reqs:
        c += rl.cost_of(u, pricing, tier=tier)
        tot["input"] += u.input; tot["cached"] += u.cached; tot["written"] += u.written
        tot["output"] += u.output; tot["reasoning"] += u.reasoning
    return c, tot


def advice(cost, tot, hikes, tenants, is_session, n_req):
    tips = []
    T = cost.total or 1e-12
    share = lambda x: x / T
    hike_usd = sum(h["usd_lost"] for h in hikes if not h["after_compaction"])
    hit = tot["cached"] / tot["input"] if tot["input"] else 0
    avg_in = tot["input"] / max(1, n_req)
    if share(cost.reasoning) > 0.4:
        tips.append(f"Reasoning is {share(cost.reasoning):.0%} of spend. Run the effort ladder: default to a "
                    f"lower effort and escalate only when a check fails (rent_calc.py ladder).")
    if hike_usd > 0.05 * T:
        tips.append(f"Rent hikes (broken prefixes) cost {rl.usd(hike_usd)} ({hike_usd / T:.0%}). Something early in "
                    f"the prompt changed between requests: run strata_lint.py on the code that builds it.")
    if not is_session and hit < 0.5 and avg_in >= 2048:
        tips.append(f"Cache hit rate is {hit:.0%} on {avg_in:,.0f}-token prompts. Lay the prompt out by lifetime "
                    f"(strata_plan.py) and set prompt_cache_key per template.")
    if is_session and tenants and (share(cost.rent) > 0.35 or share(tenants[0]["usd"]) > 0.1):
        big = tenants[0]
        tips.append(f"The biggest tenant, '{big['label']}' (~{big['tokens']:,} tokens), cost {rl.usd(big['usd'])} "
                    f"({share(big['usd']):.0%} of the session) over {big['turns_in_context']} requests. Early in a "
                    f"long session, read narrowly (rent_calc.py admit); at task boundaries, compact or start a "
                    f"new session (rent_calc.py compact).")
    if share(cost.visible) > 0.25:
        tips.append(f"Visible output is {share(cost.visible):.0%} of spend. Set text.verbosity low, ask for diffs "
                    f"or ids instead of rewritten text, and cap max_output_tokens.")
    if share(cost.fresh + cost.write) > 0.5 and hit < 0.3 and n_req > 3:
        tips.append("Most input is paid at entry price. If these calls are not user-facing, Batch or Flex "
                    "halves every line of this report.")
    return tips


def audit_group(name, reqs, pricing, tier, tenants=None, compactions=(), session=False, chained=False):
    cost, tot = summarize(reqs, pricing, tier)
    hikes = find_hikes(reqs, pricing, compactions) if (session or chained) else []
    priced = price_tenants(tenants or [], reqs, pricing, compactions) if session else []
    return {
        "group": name, "requests": len(reqs), "models": sorted({u.model for u in reqs if u.model}),
        "tokens": tot,
        "cache_hit_rate": (tot["cached"] / tot["input"]) if tot["input"] else 0.0,
        "usd": {"entry": cost.fresh, "cache_writes": cost.write, "rent": cost.rent,
                "reasoning": cost.reasoning, "visible_output": cost.visible, "total": cost.total},
        "hikes": hikes, "tenants": priced[:8], "compactions": list(compactions),
        "advice": advice(cost, tot, hikes, priced, session, len(reqs)),
    }


# --------------------------------------------------------------------------- output

def render(rep):
    U = rep["usd"]
    T = U["total"] or 1e-12
    out = [f"== {rep['group']}  ({rep['requests']} requests, {', '.join(rep['models']) or 'model ?'})",
           f"   total {rl.usd(U['total'])}   cache hit rate {rep['cache_hit_rate']:.0%}"]
    for k, lab in (("entry", "entry (new tokens)"), ("cache_writes", "cache writes"), ("rent", "rent (cached re-reads)"),
                   ("reasoning", "reasoning"), ("visible_output", "visible output")):
        if U[k] or k != "cache_writes":
            bar = "#" * int(round(30 * U[k] / T))
            out.append(f"   {lab:<24}{rl.usd(U[k]):>11}  {U[k] / T:>4.0%}  {bar}")
    real = [h for h in rep["hikes"] if not h["after_compaction"]]
    if real:
        lost = sum(h["usd_lost"] for h in real)
        where = ", ".join(str(h["request"]) for h in real[:10])
        out.append(f"   rent hikes: {len(real)} (requests {where}{'...' if len(real) > 10 else ''}) lost {rl.usd(lost)}")
    if rep["compactions"]:
        out.append(f"   compactions before requests: {', '.join(map(str, rep['compactions']))}")
    if rep["tenants"]:
        out.append("   biggest tenants (lifetime cost = entry + rent until compaction or end):")
        for t in rep["tenants"][:5]:
            out.append(f"     {rl.usd(t['usd']):>9}  ~{t['tokens']:>7,} tok x {t['turns_in_context']:>3} req  {t['label']}")
    for tip in rep["advice"]:
        out.append(f"   -> {tip}")
    return "\n".join(out)


def latest_codex():
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    files = glob.glob(os.path.join(home, "sessions", "**", "rollout-*.jsonl"), recursive=True)
    return max(files, key=os.path.getmtime) if files else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*")
    ap.add_argument("--codex-latest", action="store_true", help="audit the newest Codex session")
    ap.add_argument("--model", help="price every request as this model")
    ap.add_argument("--default-model", default="gpt-6.1-sol", help="used when a line names no model")
    ap.add_argument("--tier", default="standard", choices=["standard", "batch", "flex"])
    ap.add_argument("--pricing", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    files = list(a.files)
    if a.codex_latest:
        f = latest_codex()
        if not f:
            print("no Codex sessions found under ~/.codex/sessions (set CODEX_HOME if elsewhere)", file=sys.stderr)
            return 2
        files.append(f)
    if not files:
        ap.print_help()
        return 2

    pricing = rl.load_pricing(a.pricing)
    reports = []
    for path in files:
        objs = [o for _, _, o in rl.iter_jsonl([path])]
        if not objs:
            continue
        try:
            if any(_is_codex(o) for o in objs[:50]):
                model, reqs, tenants, comps = parse_codex(objs, a.default_model)
                if a.model:
                    for u in reqs:
                        u.model = a.model
                if reqs:
                    reports.append(audit_group(os.path.basename(path), reqs, pricing, a.tier,
                                               tenants, comps, session=True))
            else:
                for g, reqs in parse_api(objs, a.default_model).items():
                    if a.model:
                        for u in reqs:
                            u.model = a.model
                    chained = any(u.meta.get("chained") for u in reqs)
                    name = os.path.basename(path) + ("" if g == "all" else f" [{g}]")
                    reports.append(audit_group(name, reqs, pricing, a.tier, chained=chained))
        except KeyError as e:
            print(f"error in {path}: {e.args[0]}", file=sys.stderr)
            return 2

    if not reports:
        print("no usage records found", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(reports, indent=2))
    else:
        print("\n\n".join(render(r) for r in reports))
    return 0


if __name__ == "__main__":
    sys.exit(main())
