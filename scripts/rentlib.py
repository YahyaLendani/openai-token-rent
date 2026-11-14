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

UPDATE_URL = "https://raw.githubusercontent.com/YahyaLendani/openai-token-rent/main/data/pricing.json"


def cache_path() -> str:
    """Where refresh_pricing.py stores a newer pricing table (outside the skill folder)."""
    if os.environ.get("TOKEN_RENT_CACHE"):
        return os.path.join(os.environ["TOKEN_RENT_CACHE"], "pricing.json")
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = os.environ["LOCALAPPDATA"]
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "token-rent", "pricing.json")


def _as_of(data: dict) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(str(data.get("as_of", "")))
    except ValueError:
        return None


def validate_pricing(data: Any) -> str | None:
    """Returns None if `data` looks like a usable pricing table, else what is wrong."""
    if not isinstance(data, dict):
        return "not a JSON object"
    if _as_of(data) is None:
        return "missing or bad as_of date"
    gens, models = data.get("cache_generations"), data.get("models")
    if not isinstance(gens, dict) or not isinstance(models, dict) or not models:
        return "missing cache_generations or models"
    for name, m in models.items():
        if not isinstance(m, dict) or m.get("cache") not in gens:
            return f"model {name}: unknown cache generation"
        for k in ("input", "output"):
            if not isinstance(m.get(k), (int, float)) or m[k] < 0:
                return f"model {name}: bad {k} price"
    return None


def _read_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_pricing(path: str | None = None, warn: bool = True) -> dict:
    """Explicit path or TOKEN_RENT_PRICING wins. Otherwise the bundled table, unless a refreshed
    copy in the cache is valid and newer."""
    path = path or os.environ.get("TOKEN_RENT_PRICING")
    if path:
        data = _read_json(path)
    else:
        data = _read_json(DEFAULT_PRICING)
        try:
            cached = _read_json(cache_path())
            if validate_pricing(cached) is None and _as_of(cached) > _as_of(data):
                data = cached
        except (OSError, ValueError):
            pass
    if warn:
        as_of = _as_of(data)
        if as_of and (_dt.date.today() - as_of).days > STALE_DAYS:
            here = os.path.join(HERE, "refresh_pricing.py")
            print(f"[token-rent] prices are {(_dt.date.today() - as_of).days} days old (as_of {as_of}). "
                  f"Run `python3 {here}` to fetch the latest table, then rerun this command.",
                  file=sys.stderr)
    return data


def refresh_pricing(url: str | None = None, timeout: float = 10.0) -> tuple[bool, str]:
    """Downloads the maintained pricing table and stores it in the cache if it is valid and newer.
    Sends nothing but a plain GET for one public file."""
    import urllib.request
    url = url or _read_json(DEFAULT_PRICING).get("update_url") or UPDATE_URL
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "token-rent-pricing-refresh"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            fetched = json.loads(resp.read().decode("utf-8"))
    except Exception as e:                                   # network, sandbox, HTTP or JSON error
        return False, f"could not fetch {url}: {e}"
    problem = validate_pricing(fetched)
    if problem:
        return False, f"downloaded table rejected ({problem}); keeping the current prices"
    current = load_pricing(warn=False)
    if _as_of(fetched) <= _as_of(current):
        return True, f"already up to date (as_of {_as_of(current)})"
    dest = cache_path()
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(fetched, fh, indent=1)
        os.replace(tmp, dest)
    except OSError as e:
        return False, f"fetched prices as_of {_as_of(fetched)} but could not save them to {dest}: {e}"
    return True, f"updated prices to as_of {_as_of(fetched)} ({len(fetched['models'])} models), saved to {dest}"


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
