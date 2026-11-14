#!/usr/bin/env python3
"""rent_calc: break-even answers for the four token-rent decisions.

  admit    Should this block (file, tool output, document) enter the context, or a narrower one?
  compact  Is compacting / starting fresh cheaper than carrying dead context to the end?
  ladder   Is "low effort first, escalate on failed check" cheaper than high effort up front?
  model-ladder  Is "cheap model first (e.g. Sol), strong model (e.g. Astra) on a failed check"
           cheaper than the strong model every time? Accounts for per-model caches.
  ttl     Which cache TTL (or none) is cheapest for a prefix with a given reuse pattern?

All answers are in USD from data/pricing.json and are only as good as the inputs you give.
Run `python rent_calc.py <command> -h` for each command's inputs.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rentlib as rl  # noqa: E402


def _rates(a, tokens=0):
    return rl.rates_for(rl.load_pricing(a.pricing), a.model, tokens, a.tier)


def _entry(a, r):
    return rl.entry_mult(r) if a.entry_mult is None else a.entry_mult


def cmd_admit(a) -> dict:
    """Wide read: X tokens enter now and pay rent for R more requests.
    Narrow read: one extra round trip (re-reads the context L as rent, plus a short tool call)
    and only Y tokens enter."""
    r = _rates(a, a.context)
    p = min(1.0, max(0.0, a.p_need_wide))
    trip = a.context * r.p_cached + a.call_tokens * r.p_out

    def costs(turns):
        wide = rl.lifetime_cost(a.tokens, r, turns, _entry(a, r))
        narrow = trip + rl.lifetime_cost(a.narrow, r, turns, _entry(a, r)) + p * (trip + wide)
        return wide, narrow

    wide, narrow = costs(a.turns_left)
    # smallest number of remaining requests at which narrowing wins (search; costs are linear)
    w0, n0 = costs(0)
    w1, n1 = costs(1)
    slope = (w1 - w0) - (n1 - n0)                  # how much narrowing gains per extra request
    gap0 = n0 - w0                                 # narrowing's extra cost with nothing left
    if gap0 <= 0:
        breakeven = 0.0
    elif slope <= 0:
        breakeven = math.inf
    else:
        breakeven = gap0 / slope
    verdict = "narrow" if narrow < wide else "wide"
    if breakeven == 0.0:
        tail = "Narrowing pays even if nothing follows, because it skips the entry price."
    elif math.isfinite(breakeven):
        tail = f"Narrowing pays once more than {breakeven:.1f} requests remain."
    else:
        tail = "Narrowing never pays here."
    return {
        "decision": "admit", "model": r.model, "verdict": verdict,
        "wide_usd": wide, "narrow_usd": narrow, "breakeven_turns_left": breakeven,
        "why": (f"Reading {a.tokens:,} tokens costs {rl.usd(wide)} over the {a.turns_left} requests left; "
                f"an extra round trip to admit only {a.narrow:,} costs {rl.usd(narrow)}"
                + (f" (with a {p:.0%} chance you need the wide read anyway)" if p else "") + ". " + tail),
    }


def cmd_compact(a) -> dict:
    """Keep: dead tokens (L - K) pay rent on every remaining request.
    Compact: one call that reads L (as rent) and emits a summary, then the surviving K tokens
    re-enter as a new prefix (full or write price instead of rent, once)."""
    r = _rates(a, a.context)
    dead = max(0, a.context - a.keep)
    keep_cost = dead * r.p_cached * a.turns_left
    compact_call = a.context * r.p_cached + a.compact_output * r.p_out
    reentry = a.keep * (r.p_in * _entry(a, r) - r.p_cached)
    compact_cost = compact_call + reentry
    per_turn = dead * r.p_cached
    breakeven = math.inf if per_turn <= 0 else compact_cost / per_turn
    verdict = "compact" if compact_cost < keep_cost else "keep"
    return {
        "decision": "compact", "model": r.model, "verdict": verdict,
        "keep_usd": keep_cost, "compact_usd": compact_cost, "breakeven_turns_left": breakeven,
        "why": (f"{dead:,} dead tokens cost {rl.usd(per_turn)} per request in rent; compacting costs "
                f"{rl.usd(compact_cost)} once. Compact only if more than {breakeven:.1f} requests remain "
                f"(you said {a.turns_left})."),
    }


def cmd_ladder(a) -> dict:
    """Direct: one high-effort call. Ladder: low-effort call + a check, and with probability p
    an escalation that re-reads the input as rent (prefix kept, e.g. via configuration_update)."""
    r = _rates(a, a.input)
    def call(inp_mult, reasoning):
        return a.input * r.p_in * inp_mult + (reasoning + a.output) * r.p_out
    direct = call(1.0, a.high)
    esc_in = r.read_mult if a.cached_escalation else 1.0
    low = call(1.0, a.low)
    escalate = call(esc_in, a.high)
    ladder = low + a.check_usd + a.p_fail * escalate
    # largest failure rate at which the ladder still wins
    p_star = (direct - low - a.check_usd) / escalate if escalate > 0 else 0.0
    verdict = "ladder" if ladder < direct else "direct"
    return {
        "decision": "ladder", "model": r.model, "verdict": verdict,
        "direct_usd": direct, "ladder_usd": ladder, "max_p_fail": max(0.0, min(1.0, p_star)),
        "why": (f"High effort up front costs {rl.usd(direct)}; low first then escalate costs "
                f"{rl.usd(ladder)} at a {a.p_fail:.0%} failure rate. The ladder wins while low effort "
                f"fails less than {max(0.0, min(1.0, p_star)):.0%} of the time."),
    }


def cmd_model_ladder(a) -> dict:
    """Direct: every call goes to the strong model. Ladder: the cheap model first, a check, and
    with probability p a retry on the strong model.

    Caches are per model, so the retry may find the strong model's copy of the prefix cold.
    Steady traffic keeps the cache of the model you call every time warm; --strong-cache-hit
    is the chance a retry finds the strong model's prefix still cached (0 = always cold).
    """
    pricing = rl.load_pricing(a.pricing)
    cheap = rl.rates_for(pricing, a.cheap, a.input, a.tier)
    strong = rl.rates_for(pricing, a.strong, a.input, a.tier)
    prefix = max(0, min(a.prefix, a.input))
    fresh = a.input - prefix
    hit = min(1.0, max(0.0, a.strong_cache_hit))

    def entry(r):
        return rl.entry_mult(r) if a.entry_mult is None else a.entry_mult

    def call(r, reasoning, p_warm):
        cold_prefix = prefix * r.p_in * entry(r)
        warm_prefix = prefix * r.p_cached
        inp = p_warm * warm_prefix + (1 - p_warm) * cold_prefix + fresh * r.p_in * entry(r)
        return inp + (reasoning + a.output) * r.p_out

    direct = call(strong, a.reasoning_strong, 1.0)           # always-strong traffic keeps its cache warm
    first = call(cheap, a.reasoning_cheap, 1.0)
    retry = call(strong, a.reasoning_strong, hit)
    ladder = first + a.check_usd + a.p_fail * retry
    p_star = (direct - first - a.check_usd) / retry if retry > 0 else 0.0
    p_star = max(0.0, min(1.0, p_star))
    cold_penalty = call(strong, a.reasoning_strong, 0.0) - call(strong, a.reasoning_strong, 1.0)
    verdict = "ladder" if ladder < direct else "direct"
    note = ""
    if prefix and hit < 1.0 and cold_penalty > 0:
        note = (f" A retry that finds {strong.model}'s cache cold pays {rl.usd(cold_penalty)} extra for the "
                f"{prefix:,}-token prefix; pass --strong-cache-hit if retries are frequent enough to keep it warm.")
    return {
        "decision": "model-ladder", "cheap": cheap.model, "strong": strong.model, "verdict": verdict,
        "direct_usd": direct, "ladder_usd": ladder, "first_try_usd": first, "retry_usd": retry,
        "max_p_fail": p_star, "cold_retry_penalty_usd": cold_penalty,
        "why": (f"Always {strong.model} costs {rl.usd(direct)} per call; {cheap.model} first, then "
                f"{strong.model} on a failed check, costs {rl.usd(ladder)} at a {a.p_fail:.0%} failure rate. "
                f"The ladder wins while {cheap.model} fails less than {p_star:.0%} of the time." + note),
    }


def cmd_ttl(a) -> dict:
    r = _rates(a, a.prefix)
    rows = []
    if r.generation == "explicit":       # earlier models cache automatically; "none" is not a choice
        rows.append({"ttl": "none", "usd_per_request": a.prefix * r.p_in, "hits_per_write": 0.0})
    for ttl, secs in r.ttl_seconds.items():
        hits = rl.ttl_hits(a.gap, a.reuses, secs, not a.no_refresh)
        mult = rl.cached_block_mult(r.write_mult.get(ttl, 1.0), r.read_mult, hits)
        rows.append({"ttl": ttl, "usd_per_request": a.prefix * r.p_in * mult, "hits_per_write": hits})
    if a.prefix < r.min_prefix:
        for row in rows:
            row["usd_per_request"] = a.prefix * r.p_in
            row["hits_per_write"] = 0.0
    best = min(rows, key=lambda x: x["usd_per_request"])
    note = ""
    if a.prefix < r.min_prefix:
        note = f" The prefix is under the {r.min_prefix}-token minimum, so it cannot cache at all."
    elif r.generation == "implicit":
        note = " Earlier models charge nothing for writes, so the longer retention is free when allowed."
    return {
        "decision": "ttl", "model": r.model, "generation": r.generation, "verdict": best["ttl"],
        "options": rows,
        "why": f"Cheapest: {best['ttl']} at {rl.usd(best['usd_per_request'])} per request." + note,
    }


def main(argv=None):
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--tier", default="standard", choices=["standard", "batch", "flex"])
    base.add_argument("--pricing", default=None, help="path to pricing.json (default: bundled)")
    base.add_argument("--json", action="store_true", help="print JSON instead of a sentence")
    base.add_argument("--entry-mult", type=float, default=None,
                      help="price of a new token as a multiple of input (default 1.25 on GPT-5.6+, "
                           "where new tokens are cache writes; 1 elsewhere)")
    common = argparse.ArgumentParser(add_help=False, parents=[base])
    common.add_argument("--model", default="gpt-6.1-sol")

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("admit", parents=[common], help="wide read vs. narrower read")
    s.add_argument("--tokens", type=int, required=True, help="size of the wide block")
    s.add_argument("--narrow", type=int, default=0, help="size of the narrower alternative")
    s.add_argument("--context", type=int, required=True, help="current context size")
    s.add_argument("--turns-left", type=int, required=True, help="requests expected before the block leaves context")
    s.add_argument("--call-tokens", type=int, default=150, help="output tokens of the extra tool call")
    s.add_argument("--p-need-wide", type=float, default=0.0,
                   help="chance the narrow read is not enough and you read the wide block anyway")
    s.set_defaults(fn=cmd_admit)

    s = sub.add_parser("compact", parents=[common], help="carry dead context vs. compact / restart")
    s.add_argument("--context", type=int, required=True, help="current context size")
    s.add_argument("--keep", type=int, required=True, help="tokens that survive compaction (incl. summary)")
    s.add_argument("--turns-left", type=int, required=True)
    s.add_argument("--compact-output", type=int, default=4000, help="tokens the compaction call emits")
    s.set_defaults(fn=cmd_compact)

    s = sub.add_parser("ladder", parents=[common], help="effort ladder vs. high effort up front")
    s.add_argument("--input", type=int, required=True, help="input tokens per call")
    s.add_argument("--output", type=int, default=800, help="visible output tokens")
    s.add_argument("--low", type=int, required=True, help="reasoning tokens at the low effort")
    s.add_argument("--high", type=int, required=True, help="reasoning tokens at the high effort")
    s.add_argument("--p-fail", type=float, required=True, help="share of low-effort answers that fail the check")
    s.add_argument("--check-usd", type=float, default=0.0, help="cost of the check (tests are ~0)")
    s.add_argument("--no-cached-escalation", dest="cached_escalation", action="store_false",
                   help="escalation re-sends input uncached (different model or broken prefix)")
    s.set_defaults(fn=cmd_ladder)

    s = sub.add_parser("model-ladder", parents=[base],
                       help="cheap model first, strong model on a failed check, vs. strong model always")
    s.add_argument("--cheap", default="gpt-6.1-sol", help="model tried first")
    s.add_argument("--strong", default="gpt-6-astra", help="model used on a failed check, or always")
    s.add_argument("--input", type=int, required=True, help="input tokens per call")
    s.add_argument("--prefix", type=int, default=0,
                   help="of those, tokens in a stable shared prefix that steady traffic keeps cached")
    s.add_argument("--output", type=int, default=800, help="visible output tokens")
    s.add_argument("--reasoning-cheap", type=int, default=0, help="reasoning tokens on the cheap model")
    s.add_argument("--reasoning-strong", type=int, default=0, help="reasoning tokens on the strong model")
    s.add_argument("--p-fail", type=float, required=True, help="share of cheap-model answers that fail the check")
    s.add_argument("--check-usd", type=float, default=0.0, help="cost of the check (tests are ~0)")
    s.add_argument("--strong-cache-hit", type=float, default=0.0,
                   help="chance a retry finds the strong model's prefix cached (default 0: cold)")
    s.set_defaults(fn=cmd_model_ladder)

    s = sub.add_parser("ttl", parents=[common], help="cheapest cache TTL for one prefix")
    s.add_argument("--prefix", type=int, required=True, help="prefix tokens")
    s.add_argument("--gap", type=float, required=True, help="mean seconds between requests sharing it")
    s.add_argument("--reuses", type=float, required=True, help="requests that share it before it changes")
    s.add_argument("--no-refresh", action="store_true", help="assume a hit does not extend the TTL")
    s.set_defaults(fn=cmd_ttl)

    a = p.parse_args(argv)
    try:
        out = a.fn(a)
    except KeyError as e:
        print(f"error: {e.args[0]}", file=sys.stderr)
        return 2
    if a.json:
        clean = {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in out.items()}
        print(json.dumps(clean, indent=2))
    else:
        print(f"[{out['decision']}] {out['verdict'].upper()}: {out['why']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
