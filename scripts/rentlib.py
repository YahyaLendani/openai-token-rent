"""Shared helpers for openai-token-rent: pricing lookup, usage normalisation, cost split.

Standard library only. Every other script imports this module from its own folder.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PRICING = os.path.join(HERE, "..", "data", "pricing.json")
STALE_DAYS = 60
MTOK = 1_000_000


# --------------------------------------------------------------------------- pricing

def load_pricing(path: str | None = None, warn: bool = True) -> dict:
    path = path or os.environ.get("TOKEN_RENT_PRICING") or DEFAULT_PRICING
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if warn:
        try:
            as_of = _dt.date.fromisoformat(data.get("as_of", ""))
            age = (_dt.date.today() - as_of).days
            if age > STALE_DAYS:
                print(f"[token-rent] pricing.json is {age} days old (as_of {as_of}). "
                      f"Refresh it from {data.get('source')} before trusting dollar figures.",
                      file=sys.stderr)
        except ValueError:
            pass
    return data


def resolve_model(pricing: dict, name: str | None) -> tuple[str | None, dict | None]:
    """Exact match, else the longest known name that prefixes `name` at a '-' boundary
    (so dated snapshots like gpt-6.1-sol-2026-09-01 resolve to gpt-6.1-sol)."""
    if not name:
        return None, None
    models = pricing["models"]
    if name in models:
        return name, models[name]
    best = None
    for key in models:
        if name.startswith(key + "-") and (best is None or len(key) > len(best)):
            best = key
    return (best, models[best]) if best else (None, None)


@dataclass
class Rates:
    """Per-token USD rates for one model at one context size."""
    model: str
    p_in: float
    p_cached: float
    p_out: float
    generation: str          # "explicit" | "implicit"
    write_mult: dict          # ttl -> multiplier of p_in
    ttl_seconds: dict
    min_prefix: int
    max_writes: int | None

    @property
    def read_mult(self) -> float:
        return self.p_cached / self.p_in if self.p_in else 1.0

    @property
    def out_ratio(self) -> float:
        """How many input tokens one output token costs."""
        return self.p_out / self.p_in if self.p_in else 0.0


def rates_for(pricing: dict, model: str, input_tokens: int = 0, tier: str = "standard") -> Rates:
    key, entry = resolve_model(pricing, model)
    if entry is None:
        raise KeyError(f"unknown model '{model}'. Known: {', '.join(sorted(pricing['models']))}. "
                       f"Add it to data/pricing.json.")
    src = entry
    lc = entry.get("long_context")
    if lc and input_tokens > lc.get("threshold", 10**12):
        src = {**entry, **{k: v for k, v in lc.items() if k != "threshold"}}
    tier_mult = pricing.get("tiers", {}).get(tier, 1.0)
    p_in = src["input"] * tier_mult / MTOK
    cached = src.get("cached_input")
    p_cached = (cached if cached is not None else src["input"]) * tier_mult / MTOK
    p_out = src["output"] * tier_mult / MTOK
    gen_name = entry.get("cache", "implicit")
    gen = pricing["cache_generations"][gen_name]
    return Rates(model=key, p_in=p_in, p_cached=p_cached, p_out=p_out, generation=gen_name,
                 write_mult=dict(gen["write_multipliers"]), ttl_seconds=dict(gen["ttl_seconds"]),
                 min_prefix=gen.get("min_prefix_tokens", 1024),
                 max_writes=gen.get("max_writes_per_request"))


# --------------------------------------------------------------------------- usage

@dataclass
class Usage:
    model: str | None
    input: int = 0            # all input tokens, cached ones included
    cached: int = 0           # read from cache ("rent")
    writes: dict = field(default_factory=dict)   # ttl -> tokens written to cache
    output: int = 0           # all output tokens, reasoning included
    reasoning: int = 0
    group: str = "default"
    index: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def written(self) -> int:
        return sum(self.writes.values())

    @property
    def fresh(self) -> int:
        return max(0, self.input - self.cached - self.written)

    @property
    def visible(self) -> int:
        return max(0, self.output - self.reasoning)


def _get(d: Any, *path, default=0):
    for p in path:
        if not isinstance(d, dict) or p not in d or d[p] is None:
            return default
        d = d[p]
    return d


def normalize_usage(u: dict, model: str | None = None) -> Usage | None:
    """Accepts a Responses `usage`, a Chat Completions `usage`, or a Codex TokenUsage."""
    if not isinstance(u, dict):
        return None
    if "input_tokens" in u and "cached_input_tokens" in u:            # Codex CLI TokenUsage
        # Codex records cache writes as one total with no TTL split: assume the 30m default.
        cw = int(u.get("cache_write_input_tokens") or 0)
        return Usage(model=model, input=int(u.get("input_tokens") or 0),
                     cached=int(u.get("cached_input_tokens") or 0),
                     writes={"30m": cw} if cw else {},
                     output=int(u.get("output_tokens") or 0),
                     reasoning=int(u.get("reasoning_output_tokens") or 0))
    if "input_tokens" in u:                                            # Responses API
        det = u.get("input_tokens_details") or {}
        writes = {}
        wdet = det.get("cache_write_tokens_details") or u.get("cache_write_tokens_details") or {}
        for k, v in (wdet or {}).items():
            if v:
                ttl = k.replace("ttl_", "").replace("_tokens", "")
                writes[ttl] = int(v)
        total_w = int(det.get("cache_write_tokens") or u.get("cache_write_tokens") or 0)
        if total_w and not writes:
            writes["30m"] = total_w                                    # TTL unknown: assume default
        return Usage(model=model, input=int(u.get("input_tokens") or 0),
                     cached=int(det.get("cached_tokens") or 0), writes=writes,
                     output=int(u.get("output_tokens") or 0),
                     reasoning=int(_get(u, "output_tokens_details", "reasoning_tokens")))
    if "prompt_tokens" in u:                                           # Chat Completions
        return Usage(model=model, input=int(u.get("prompt_tokens") or 0),
                     cached=int(_get(u, "prompt_tokens_details", "cached_tokens")),
                     output=int(u.get("completion_tokens") or 0),
                     reasoning=int(_get(u, "completion_tokens_details", "reasoning_tokens")))
    return None


@dataclass
class Cost:
    fresh: float = 0.0       # entry price of new tokens
    write: float = 0.0       # cache-write premium included (whole write price)
    rent: float = 0.0        # cached re-reads
    reasoning: float = 0.0
    visible: float = 0.0

    @property
    def total(self) -> float:
        return self.fresh + self.write + self.rent + self.reasoning + self.visible

    def __iadd__(self, o: "Cost"):
        self.fresh += o.fresh; self.write += o.write; self.rent += o.rent
        self.reasoning += o.reasoning; self.visible += o.visible
        return self


def cost_of(u: Usage, pricing: dict, model: str | None = None, tier: str = "standard") -> Cost:
    r = rates_for(pricing, model or u.model, u.input, tier)
    write = 0.0
    for ttl, n in u.writes.items():
        write += n * r.p_in * r.write_mult.get(ttl, 1.25 if r.generation == "explicit" else 1.0)
    return Cost(fresh=u.fresh * r.p_in, write=write, rent=u.cached * r.p_cached,
                reasoning=u.reasoning * r.p_out, visible=u.visible * r.p_out)


# --------------------------------------------------------------------------- rent maths

def entry_mult(r: Rates) -> float:
    """Price multiplier (of p_in) for a brand-new token that will be cached.
    Explicit-generation models bill it as a 30m cache write; earlier models bill plain input."""
    return r.write_mult.get("30m", 1.0) if r.generation == "explicit" else 1.0


def lifetime_cost(tokens: int, r: Rates, turns_left: int, entry: float | None = None) -> float:
    """USD a token block costs from now until it leaves context: entry once, then rent
    (a cached re-read) on every later request."""
    e = entry_mult(r) if entry is None else entry
    return tokens * (r.p_in * e + turns_left * r.p_cached)


def ttl_hits(gap_s: float, run_len: float, ttl_s: float, refresh_on_hit: bool = True) -> float:
    """Expected cache hits per write.
    gap_s:   mean seconds between requests that share this prefix
    run_len: mean requests that share the prefix before it changes (>= 1)
    """
    if run_len <= 1 or gap_s > ttl_s:
        return 0.0
    hits = run_len - 1
    if not refresh_on_hit:
        hits = min(hits, ttl_s // max(gap_s, 1e-9))
    return float(hits)


def cached_block_mult(write_mult: float, read_mult: float, hits: float) -> float:
    """Average price multiplier (of p_in) per request for a block that is written once and
    then read `hits` times before it changes or expires."""
    return (write_mult + hits * read_mult) / (hits + 1.0)


# --------------------------------------------------------------------------- misc

def est_tokens(text: str) -> int:
    """Cheap estimate (~4 chars/token for English and code). Uses tiktoken when installed."""
    try:
        import tiktoken  # type: ignore
        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception:
        return max(1, (len(text) + 3) // 4) if text else 0


def iter_jsonl(paths: Iterable[str]):
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield p, n, json.loads(line)
                except json.JSONDecodeError:
                    continue


def usd(x: float) -> str:
    if abs(x) >= 100:
        return f"${x:,.0f}"
    if abs(x) >= 1:
        return f"${x:,.2f}"
    return f"${x:.4f}"
