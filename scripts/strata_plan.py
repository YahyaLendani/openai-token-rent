#!/usr/bin/env python3
"""strata_plan: order prompt segments and place cache breakpoints for the lowest cost per request.

A cached prefix is only valid while *every* segment in it is unchanged, and a prefix that
holds a per-user or per-conversation segment is shared only by that slice of traffic.
So a segment placed early taxes everything after it. This planner scores each order with the
cache maths in rentlib, picks breakpoints (max 4 writes per request on GPT-5.6+) and a TTL for
each, and compares the result with the order you have now.

Input: a JSON file (see examples/strata.example.json):
{
  "model": "gpt-6.1-sol",
  "requests_per_hour": 600,
  "refresh_on_hit": true,
  "segments": [
    {"name": "system",   "tokens": 1500,  "changes_per_day": 0.05, "pin": "first"},
    {"name": "tools",    "tokens": 4000,  "changes_per_day": 0.2},
    {"name": "profile",  "tokens": 700,   "changes_per_day": 1, "partitions": 2000},
    {"name": "question", "tokens": 150,   "per_request": true, "pin": "last"}
  ]
}
Change rate: one of changes_per_day / changes_per_hour / per_request. `partitions`: how many
distinct variants of the segment are live at once (active users, tenants, open conversations).
Default 1. Nested scopes (a conversation belongs to a user) do not multiply: the prefix takes the
largest count so far. Mark a segment "independent": true when its variants cross with earlier
ones (locale x A/B arm), and its count multiplies.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rentlib as rl  # noqa: E402

EXHAUSTIVE_MAX = 7


class Seg:
    def __init__(self, d: dict, lam: float):
        self.name = d["name"]
        self.tokens = int(d["tokens"])
        self.partitions = max(1.0, float(d.get("partitions", 1)))
        self.independent = bool(d.get("independent"))
        self.pin = d.get("pin")
        self.per_request = bool(d.get("per_request"))
        if "changes_per_hour" in d:
            self.rate = float(d["changes_per_hour"]) / 3600.0
        elif "changes_per_day" in d:
            self.rate = float(d["changes_per_day"]) / 86400.0
        else:
            self.rate = 0.0
        self.lam = lam

    def smith_key(self) -> float:
        """Changes per request it causes, per token it shields. Lower goes first."""
        if self.per_request:
            return math.inf
        churn = self.rate / self.lam + (1 - 1 / self.partitions)
        return churn / max(self.tokens, 1)


def prefix_stats(order: list[Seg], lam: float):
    """For each boundary k: cumulative tokens, request gap and run length of the prefix 0..k."""
    out, tok, rate, parts, broken = [], 0, 0.0, 1.0, False
    for s in order:
        tok += s.tokens
        parts = parts * s.partitions if s.independent else max(parts, s.partitions)
        broken = broken or s.per_request
        stream = lam / parts                      # requests/sec that share this exact prefix
        rate += s.rate
        if broken:
            run = 1.0
        elif rate == 0:
            run = math.inf
        else:
            run = max(1.0, stream / rate)
        gap = math.inf if stream == 0 else 1.0 / stream
        out.append((tok, gap, run))
    return out


def p_valid(gap, run, ttl_s, refresh) -> float:
    h = rl.ttl_hits(gap, run, ttl_s, refresh)
    if math.isinf(h):
        return 1.0
    return h / (h + 1.0)


def plan_explicit(order, stats, r, refresh, max_bp):
    """DP over boundaries. Tokens between two breakpoints are billed by the validity of the
    later one: read when valid, written when not. Tokens after the last breakpoint pay input."""
    n = len(order)
    ttls = list(r.ttl_seconds.items())
    best_ttl = []
    for k in range(n):
        tok, gap, run = stats[k]
        opts = []
        for name, secs in ttls:
            pv = 0.0 if tok < r.min_prefix else p_valid(gap, run, secs, refresh)
            wm = r.write_mult.get(name, 1.0)
            opts.append((pv * r.read_mult + (1 - pv) * wm, name, pv))
        best_ttl.append(opts)

    cum = [0]
    for s in order:
        cum.append(cum[-1] + s.tokens)

    def interval_cost(i, j, opt):          # segments i..j (inclusive), breakpoint at j
        return (cum[j + 1] - cum[i]) * r.p_in * opt[0]

    INF = math.inf
    # f[m][j]: min cost of segments 0..j with the m-th breakpoint at j
    f = [[INF] * n for _ in range(max_bp + 1)]
    back = [[None] * n for _ in range(max_bp + 1)]
    for j in range(n):
        for opt in best_ttl[j]:
            c = interval_cost(0, j, opt)
            if c < f[1][j]:
                f[1][j], back[1][j] = c, (None, opt)
    for m in range(2, max_bp + 1):
        for j in range(n):
            for i in range(j):
                if f[m - 1][i] == INF:
                    continue
                for opt in best_ttl[j]:
                    c = f[m - 1][i] + interval_cost(i + 1, j, opt)
                    if c < f[m][j]:
                        f[m][j], back[m][j] = c, (i, opt)
    best = (cum[n] * r.p_in, 0, None)              # no breakpoints at all
    for m in range(1, max_bp + 1):
        for j in range(n):
            if f[m][j] == INF:
                continue
            tail = (cum[n] - cum[j + 1]) * r.p_in
            if f[m][j] + tail < best[0]:
                best = (f[m][j] + tail, m, j)
    bps = {}
    cost, m, j = best
    while m and j is not None:
        i, opt = back[m][j]
        bps[j] = (opt[1], opt[2])
        m, j = m - 1, i
    return cost, bps


def plan_implicit(order, stats, r, refresh, retention):
    best = None
    for name in retention:
        secs = r.ttl_seconds.get(name)
        if secs is None:
            continue
        cost = 0.0
        pvs = []
        for k, s in enumerate(order):
            tok, gap, run = stats[k]
            pv = 0.0 if tok < r.min_prefix else p_valid(gap, run, secs, refresh)
            pvs.append(pv)
            cost += s.tokens * r.p_in * (pv * r.read_mult + (1 - pv))
        if best is None or cost < best[0]:
            best = (cost, {k: (name, pv) for k, pv in enumerate(pvs)})
    return best


def evaluate(order, lam, r, refresh, retention):
    stats = prefix_stats(order, lam)
    if r.generation == "explicit":
        return plan_explicit(order, stats, r, refresh, r.max_writes or 4)
    return plan_implicit(order, stats, r, refresh, retention)


def valid_order(order) -> bool:
    for idx, s in enumerate(order):
        if s.pin == "first" and any(o.pin != "first" for o in order[:idx]):
            return False
        if s.pin == "last" and any(o.pin != "last" for o in order[idx + 1:]):
            return False
    return True


def search(segs, lam, r, refresh, retention):
    if len(segs) <= EXHAUSTIVE_MAX:
        cands = (list(p) for p in itertools.permutations(segs))
    else:
        start = sorted(segs, key=lambda s: (s.pin != "first", s.pin == "last", s.smith_key()))
        cands = [start]
    best = None
    for order in cands:
        if not valid_order(order):
            continue
        cost, bps = evaluate(order, lam, r, refresh, retention)
        if best is None or cost < best[0] - 1e-15:
            best = (cost, order, bps)
    if len(segs) > EXHAUSTIVE_MAX:                       # adjacent-swap hill climb
        improved = True
        while improved:
            improved = False
            cost0, order0, _ = best
            for i in range(len(order0) - 1):
                o = order0[:]
                o[i], o[i + 1] = o[i + 1], o[i]
                if not valid_order(o):
                    continue
                c, b = evaluate(o, lam, r, refresh, retention)
                if c < cost0 - 1e-15:
                    best, improved = (c, o, b), True
                    break
    return best


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", help="segments JSON file")
    ap.add_argument("--model", help="override the model in the spec")
    ap.add_argument("--pricing", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    with open(a.spec, encoding="utf-8") as fh:
        spec = json.load(fh)
    pricing = rl.load_pricing(a.pricing)
    model = a.model or spec.get("model", "gpt-6.1-sol")
    lam = float(spec["requests_per_hour"]) / 3600.0
    refresh = bool(spec.get("refresh_on_hit", True))
    segs = [Seg(d, lam) for d in spec["segments"]]
    total = sum(s.tokens for s in segs)
    try:
        r = rl.rates_for(pricing, model, total)
    except KeyError as e:
        print(f"error: {e.args[0]}", file=sys.stderr)
        return 2
    _, entry = rl.resolve_model(pricing, model)
    retention = entry.get("retention") or list(r.ttl_seconds)

    cur_cost, cur_bps = evaluate(segs, lam, r, refresh, retention)
    best_cost, best_order, best_bps = search(segs, lam, r, refresh, retention)
    stats = prefix_stats(best_order, lam)
    no_cache = total * r.p_in
    per_day = lam * 86400

    rows = []
    for k, s in enumerate(best_order):
        bp = best_bps.get(k)
        rows.append({"segment": s.name, "tokens": s.tokens, "prefix_tokens": stats[k][0],
                     "breakpoint": bp[0] if (bp and r.generation == "explicit") else None,
                     "prefix_hit_rate": round(bp[1], 4) if bp else None})
    result = {
        "model": r.model, "cache_generation": r.generation,
        "recommended_order": [s.name for s in best_order],
        "current_order": [s.name for s in segs],
        "rows": rows,
        "usd_per_request": {"recommended": best_cost, "current": cur_cost, "no_cache": no_cache},
        "usd_per_day": {"recommended": best_cost * per_day, "current": cur_cost * per_day},
        "saving_vs_current_pct": 0.0 if cur_cost == 0 else 100 * (cur_cost - best_cost) / cur_cost,
    }
    if r.generation == "implicit" and best_bps:
        result["prompt_cache_retention"] = next(iter(best_bps.values()))[0]

    if a.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"model {r.model} ({r.generation} caching), {spec['requests_per_hour']} req/h, {total:,} input tokens/request\n")
    print(f"{'#':>2}  {'segment':<20}{'tokens':>9}{'prefix':>10}  {'breakpoint':<11}{'hit rate':>9}")
    for i, row in enumerate(rows, 1):
        bp = row["breakpoint"] or ("(implicit)" if r.generation == "implicit" else "")
        hr = "" if row["prefix_hit_rate"] is None else f"{row['prefix_hit_rate']:.0%}"
        print(f"{i:>2}  {row['segment']:<20}{row['tokens']:>9,}{row['prefix_tokens']:>10,}  {bp:<11}{hr:>9}")
    print()
    print(f"input cost/request  recommended {rl.usd(best_cost)}   current {rl.usd(cur_cost)}   no cache {rl.usd(no_cache)}")
    print(f"input cost/day      recommended {rl.usd(best_cost * per_day)}   current {rl.usd(cur_cost * per_day)}")
    if best_order != segs:
        print(f"reordering saves {result['saving_vs_current_pct']:.1f}% of input spend")
    else:
        print("your current order is already the cheapest found")
    if "prompt_cache_retention" in result:
        print(f'set prompt_cache_retention="{result["prompt_cache_retention"]}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
