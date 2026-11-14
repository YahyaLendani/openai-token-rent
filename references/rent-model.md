# The rent model

The maths behind each rule in SKILL.md. Every script implements these formulas; `scripts/rentlib.py`
holds the shared parts.

## Contents
1. Notation
2. Lifetime cost
3. Prefix validity and traffic splitting
4. Block order: Smith's rule for prompts
5. Cache writes and TTL choice
6. Breakpoint placement
7. Admission control
8. Eviction (compaction)
9. Output and reasoning
10. The effort ladder
11. Assumptions and limits

## 1. Notation

| Symbol | Meaning |
|---|---|
| `p_in`, `p_c`, `p_out` | per-token price: input, cached input, output |
| `r = p_c / p_in` | read multiplier: 0.1 on most models, 0.05 on gpt-6.1-sol and gpt-6.1-terra |
| `W_t` | write multiplier for TTL `t`: 1.25 (30m) or 2.5 (12h) on GPT-5.6+; 1 on earlier models |
| `e` | entry multiplier for a new token that gets cached: `W_30m` on GPT-5.6+, else 1 |
| `R` | requests left before a block leaves the context |
| `L` | current context size, in tokens |
| `λ` | requests per second that use a prompt template |

## 2. Lifetime cost

A block of `X` tokens that enters now and stays for `R` more requests costs

```
C(X) = X · (e · p_in + R · p_c)
```

The two terms trade places over a session. Early, `R` is large and rent dominates. Late, entry
dominates. For gpt-6.1-sol, `e · p_in = 2.5e-6` and `p_c = 1e-7`, so rent overtakes entry once a
block stays for more than 25 requests. In a 60-request Codex session, a file read at request 5
costs 3.2x its entry price.

## 3. Prefix validity and traffic splitting

A cached prefix ending at block `k` is valid only while blocks `1..k` are all unchanged. If block
`i` changes at rate `c_i` (changes per second), the prefix changes at `Λ_k = Σ_{i≤k} c_i`.

Blocks with `P_i` live variants (users, tenants, conversations) split traffic. The prefix through
`k` is shared by `λ_k = λ / P_k` requests per second, where `P_k` is the largest variant count so
far (nested scopes) or the product (independent scopes, `"independent": true` in a spec).

Two numbers follow:

- run length `N_k = λ_k / Λ_k`: requests that share one version of the prefix
- gap `g_k = 1 / λ_k`: seconds between those requests

A write produces `h = N_k − 1` hits if `g_k ≤ TTL` (assuming each hit refreshes the TTL; pass
`--no-refresh` or `"refresh_on_hit": false` to cap hits at `TTL / g_k`), and 0 hits otherwise. The
share of requests that find the prefix cached is `h / (h + 1)`.

This is why a user profile near the top hurts more than its size suggests. With 400 active users,
each user's stream is 1/400 of traffic. At 600 requests/hour, that is one request per 40 minutes,
which is longer than a 30m TTL. Every block after the profile then misses the cache, for everyone.

## 4. Block order: Smith's rule for prompts

Take two adjacent blocks A (`a` tokens, miss contribution `α`) and B (`b`, `β`), where the miss
contribution is the probability per request that the block invalidates what follows (change rate
÷ request rate, plus `1 − 1/P` for traffic splitting). For small miss probabilities, the expected
re-billed tokens are

```
order A,B:  a·α + b·(α + β)
order B,A:  b·β + a·(α + β)
A,B − B,A = b·α − a·β    →   A goes first iff  α / a < β / b
```

So the sort key is **miss contribution per token**, lowest first. This is Smith's rule from
scheduling theory (weighted shortest processing time). A volatile block is acceptable early if it
is big enough. What matters is how much it shields relative to how much it churns.

Worked example (the test `test_size_beats_rate_when_shielding_more`): tools 1,500 tokens changing
twice an hour; corpus 60,000 tokens changing four times an hour; 3,600 requests/hour.
`α_tools / a = (2/3600)/1500 = 3.7e-7` and `α_corpus / b = (4/3600)/60000 = 1.9e-8`. The corpus
goes first, though it changes twice as often.

`strata_plan.py` uses this key to seed the search, then scores orders exactly (it tries every
permutation up to 7 blocks and hill-climbs beyond that) because the linear approximation fails
when miss probabilities are large or TTLs bind.

## 5. Cache writes and TTL choice

A block written once and read `h` times costs, per request, on average

```
m(h) = (W + h · r) / (h + 1)      (as a multiple of p_in)
```

Caching beats sending it uncached (multiplier 1) when `m(h) < 1`, i.e. `h > (W − 1) / (1 − r)`:

| TTL | W | hits needed (r = 0.1) |
|---|---|---|
| 30m | 1.25 | > 0.28, so 1 hit |
| 12h | 2.5 | > 1.67, so 2 hits |

On GPT-5.6+, a write that nobody reads costs 25% (30m) or 150% (12h) extra. Caching per-request
content wastes money, which the older free-write models never did. On earlier models, writes are
free, so the longest allowed retention (`24h` where supported) is always at least as cheap.

Choose 12h over 30m when the gap between reuses exceeds 30 minutes and each version is reused at
least twice. `rent_calc.py ttl` compares the options for a given gap and reuse count.

## 6. Breakpoint placement

GPT-5.6+ allows at most 4 cache writes per request. With breakpoints at `b_1 < … < b_m`, tokens
between `b_{j−1}` and `b_j` cost `v_j · r + (1 − v_j) · W_j` (read when the prefix through `b_j` is
valid, written when not), and tokens after the last breakpoint cost 1. `strata_plan.py` solves
this exactly with dynamic programming over boundaries and TTLs, for each candidate order.

A breakpoint helps when the block before it is reused more often than the blocks after it. Put
one at the end of the shared stable blocks, one after each traffic split you can't avoid, and none
after per-request content.

## 7. Admission control

The choice is a wide read (`X` tokens) or a narrow one (`Y` tokens) that costs an extra round
trip. The extra request re-reads the whole context as rent and emits a short tool call of `c`
output tokens. With probability `q` the narrow read is not enough and the wide read follows anyway:

```
wide   = C(X)
narrow = L · p_c + c · p_out + C(Y) + q · (L · p_c + c · p_out + C(X))
```

With `q = 0`, narrowing wins when `(X − Y) · (e · p_in + R · p_c) > L · p_c + c · p_out`. The saving
grows with `R` and the cost grows with `L`:

- early in a long session (small `L`, large `R`): narrow aggressively
- late in a big session (large `L`, small `R`): batch reads and read wide once

`references/codex-playbook.md` has a table of break-even sizes. `rent_calc.py admit` computes one.

## 8. Eviction (compaction)

Keeping `D` dead tokens costs `D · p_c · R`. Compacting costs one call that reads the context
(`L · p_c`) and emits a summary (`S · p_out`), plus a one-off re-entry: the `K` surviving tokens
form a new prefix, paid at `e · p_in` instead of `p_c` on the next request.

```
compact iff  D · p_c · R  >  L · p_c + S · p_out + K · (e · p_in − p_c)
break-even R* = (L · p_c + S · p_out + K · (e · p_in − p_c)) / (D · p_c)
```

Example (gpt-6.1-sol, `L` = 180k, `K` = 40k, `S` = 4k): `R* ≈ 11`. With 30 requests left, compact.
With 4 left, finish first. A fixed auto-compaction threshold ignores `R`, so it compacts too early
near the end of a task and too late at the start of a long one. Raw context size is the wrong
trigger.

Starting a new session is compaction with `K` = the bootstrap context (AGENTS.md, task note), so
it wins at clean task boundaries.

## 9. Output and reasoning

An output token costs `p_out` (5-8x `p_in` on current models) when it is generated. In a
multi-turn conversation it then stays in context, so its lifetime cost is `p_out + R · p_c`.
Verbose answers cost twice: once as output, then as rent.

Reasoning tokens bill as output. With `reasoning.context = "all_turns"` (the GPT-5.6 default),
persisted reasoning items are carried into later turns, so they can also pay rent. Measure it:
compare `input_tokens` across turns with `current_turn` and with `all_turns` before you choose.

Cheaper output patterns: ask for a diff, an id, an enum or a field instead of rewritten text, and
use structured outputs so the model doesn't restate the input.

## 10. The effort ladder

Direct: one call at high effort, `c_hi = I · p_in + (ρ_hi + O) · p_out`.
Ladder: one call at low effort, then a check costing `v`, and with probability `f` an escalation.
The escalation keeps the prefix, so it pays input at `r` (on GPT-6+, append a
`configuration_update` item instead of changing the top-level effort):

```
ladder = c_lo + v + f · (I · p_c + (ρ_hi + O) · p_out)
ladder wins iff  f < (c_hi − c_lo − v) / c_esc
```

Typical numbers (20k input, 1.5k low vs 12k high reasoning tokens, gpt-6.1-sol): the ladder wins
while low effort fails less than ~80% of the time. A free check (tests, schema validation) is
what makes the ladder work. Without one, use high effort for hard tasks.

**Across models** (`rent_calc.py model-ladder`). The same shape holds with a cheap model `m` and a
strong model `M`, with one difference: caches are per model, so the retry cannot reuse the cheap
model's cached prefix. Split the input into a stable prefix `P` and new tokens `N = I − P`, and let
`h` be the chance the retry finds `M`'s copy of the prefix still cached:

```
c_M(warm) = P · p_c(M) + N · e · p_in(M) + (ρ_M + O) · p_out(M)        always-M traffic keeps M warm
c_m       = P · p_c(m) + N · e · p_in(m) + (ρ_m + O) · p_out(m)
c_retry   = c_M(warm) + (1 − h) · P · (e · p_in(M) − p_c(M))
ladder    = c_m + v + f · c_retry          wins iff  f < (c_M(warm) − c_m − v) / c_retry
```

`e` is the entry multiplier (1.25 on GPT-5.6+, where new tokens are 30m writes). Example: 50k input
with a 45k prefix, 4k reasoning on both, gpt-6.1-sol then gpt-6-astra. Always Astra costs $0.35 a
call. With a cold retry the ladder wins while Sol fails less than ~33% of the time; with a warm
Astra cache, while it fails less than ~81%. The cold prefix, not the model price, decides most of
the gap, so a model ladder pays best on short prompts or when retries keep the strong model warm.

## 11. Assumptions and limits

- Prices are list prices from `data/pricing.json`. Discounts, regional uplift (10% for models
  released on or after 2026-03-05 on regional endpoints) and Fast mode are not modelled.
- Long-context pricing (>272k input) is assumed to cover the whole request.
- On GPT-5.6+, new tokens in a conversation are assumed to be billed as 30m writes (`e = 1.25`).
  Check `cache_write_tokens` in your usage. If writes stay at 0 in implicit mode, pass
  `--entry-mult 1` to `rent_calc.py`.
- Change processes are treated as independent and steady. Deploy spikes and bursty traffic make
  real hit rates lower than the plan.
- Hit refresh on read is assumed unless you say otherwise. The docs don't state it.
- Token estimates without tiktoken use characters ÷ 4. That is accurate to about ±20% for English
  and code, and worse for CJK text and base64.
