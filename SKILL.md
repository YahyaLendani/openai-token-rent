---
name: openai-token-rent
description: Lifetime-cost token optimization for OpenAI models (GPT-5.x, GPT-6, Codex). Treats every token as a tenant that pays an entry price, then rent on every later request, plus a penalty when its cached prefix breaks, and uses that to plan prompt layout, cache TTLs, reasoning effort, context reads and compaction. Use it whenever code calls the OpenAI API (Responses, Chat Completions, Agents SDK, openai-python, openai-node) and cost, tokens, latency, prompt caching, cache hit rate, prompt_cache_key, prompt_cache_options, reasoning effort, verbosity, Batch, Flex or model choice comes up; when auditing OpenAI usage logs or a bill; and inside Codex CLI during long or expensive sessions, before large file reads, or near compaction. Use it even when the user only says "make this cheaper" about OpenAI code. Not for Anthropic/Claude API work.
---

# Token Rent

Most token advice counts tokens. This skill prices them over their **lifetime**:

```
lifetime cost of a block = entry            (input price; a cache write on GPT-5.6+)
                         + rent × requests  (cached re-read on every later request it stays in)
                         + hikes            (full price again each time an earlier token changes)
output tokens            = output price, then rent like any other block if they stay in context
```

A 25k-token file read at request 3 of a 40-request agent session is not 25k tokens: it is 25k
at entry plus 37 cached re-reads. A timestamp at the top of a system prompt costs 20 tokens and
also turns every token after it from rent (0.1x or 0.05x) back into entry price, on every request.
Counting tokens misses both. Pricing their lifetime catches both, and it tells you when the usual
advice ("always grep first", "always cache", "always compact") is wrong.

## The five rules

1. **Order by churn per token shielded, not by "static first".** A prefix is valid only while
   every block in it is unchanged, so each block taxes everything after it. Sort blocks by
   (changes per request + traffic split) ÷ tokens, lowest first (Smith's rule). A 60k corpus that
   changes four times an hour can belong *before* a 1.5k tool list that changes twice an hour.
   Per-user and per-conversation blocks split traffic into thin streams, so they go late.
2. **Price the write.** On GPT-5.6+ and GPT-6, cache writes cost 1.25x input (30m TTL) or 2.5x
   (12h). A 30m entry pays for itself after one hit, a 12h entry after two. A block that is
   written but never re-read costs *more* than no caching. Earlier models charge nothing for
   writes, so there the longest allowed retention is free.
3. **Admission control.** Before a big block enters a context, compare
   `(tokens saved) × (entry + rent × requests left)` with the cost of one extra round trip
   (`context × cached rate`). Early in a long session, narrow reads win. Late, in a large
   context, one wide read beats two narrow ones.
4. **Evict on a break-even, not a threshold.** Compact or start fresh when
   `dead tokens × cached rate × requests left` exceeds compaction cost plus the one-off re-entry
   of what survives. Near the end of a task, carrying dead context is cheaper.
5. **Output compounds, reasoning is output.** Output costs 5-8x input and then pays rent if it
   stays in context. Reasoning tokens bill at the output rate. Default to a lower effort, check
   the result cheaply, and escalate only on a failed check (the effort ladder).

Rules never outrank correctness. A saving that causes a retry, a wrong answer, or a missed
requirement is a loss, so every ladder has a check and every layout change keeps meaning intact.

## Pick the mode

- **A. Code that calls the OpenAI API** (the user's app, service, eval harness, agent). Go to
  Workflow A.
- **B. You are running inside Codex CLI** and the cost is your own session. Go to Workflow B.

Both can apply: in Codex, optimizing the user's OpenAI app is Workflow A.

## Tools

All scripts are Python 3.9+, standard library only, in `scripts/` next to this file. Run them
with the skill folder's path, e.g. `python <skill-dir>/scripts/strata_lint.py src/`.

| Script | Answers |
|---|---|
| `strata_lint.py <paths>` | Which lines break prompt caching (clock, ids, shuffles, per-user data in the system prompt, dynamic tools, unsorted JSON, wrong cache params). Exit 1 on high findings, so it can run in CI. |
| `strata_plan.py spec.json` | The cheapest block order, breakpoints and TTLs for one prompt, with $/request and $/day against the current order. Spec format: `examples/strata.example.json`. |
| `rent_audit.py logs.jsonl` / `--codex-latest` | Where real spend went: entry, writes, rent, hikes, reasoning, output; the biggest context tenants in a Codex session; and what to change. |
| `rent_calc.py admit\|compact\|ladder\|ttl` | The four break-even decisions with numbers. `-h` on each subcommand lists its inputs. |

Prices come from `data/pricing.json`. The scripts warn when it is more than 60 days old. If the
model is missing or the file is stale, say so, and offer to refresh it from the URL in the file.
Never quote a dollar figure as current when it came from a stale table.

## Workflow A: an app that calls OpenAI

1. **Find the call sites.** Search for `responses.create`, `chat.completions.create`,
   `responses.parse`, `Runner.run`, `prompt_cache`. Note the model string at each one: GPT-5.6+
   and GPT-6 use explicit caching (`prompt_cache_options`, breakpoints, billed writes). Earlier
   models use implicit caching (`prompt_cache_retention`, `prompt_cache_key`).
2. **Lint.** Run `strata_lint.py` on the repo. Read each finding in context before changing
   code; the linter is a heuristic.
3. **Plan the hot prompts.** For each high-traffic prompt, list its blocks with token size
   (count with tiktoken if installed, otherwise characters ÷ 4), how often each changes, and how
   many live variants it has (users, tenants, open conversations). Ask the user for traffic
   figures. If they don't know, state your assumptions in the spec file and in the report.
   Run `strata_plan.py`, then reorder the code to match while keeping instructions readable and
   meaning unchanged.
4. **Measure.** If usage logs exist, run `rent_audit.py` on them. If they don't, add the usage
   meter from `references/api-playbook.md` so the next audit has data, and say that the savings
   so far are estimates.
5. **Reasoning and output.** Find calls whose reasoning share is high, or calls that use one
   fixed high effort for everything. Propose the effort ladder (`rent_calc.py ladder`) with a
   concrete check: schema validation, tests, or a cheap judge model. Lower `text.verbosity`. Ask
   for diffs, ids or fields instead of rewritten text. Move non-interactive work to Batch or Flex
   (50% off everything).
6. **Report** in this shape:

```
## Token rent report: <app or prompt>
Spend split (measured | estimated): entry x% · writes x% · rent x% · hikes x% · reasoning x% · output x%
| Change | File:line | Rule | Est. saving | Confidence |
Assumptions: <traffic, change rates, pricing.json date>
Verify after deploy: usage.input_tokens_details.cached_tokens rises to ~<n>% of input_tokens
```

Read `references/api-playbook.md` for parameters, code patterns and pitfalls by model generation.

## Workflow B: inside a Codex session

Keep a rough rent clock: current context size (Codex shows it), and how many more requests the
task needs. Then:

- **Before reading anything big** (over ~2k tokens; a file's size in bytes ÷ 4), check the
  admission rule. Early in a long session, narrow: `rg -n -m 20`, `sed -n '120,220p'`,
  `git diff --stat` before `git diff`, `head -c 4000`. Late in a big context with few requests
  left, one wide read is cheaper than several narrow ones.
- **Batch independent commands into one turn.** Every round trip re-reads the whole context.
- **Cap noisy output at the source:** `-q`, `--silent`, `| tail -n 60`, `--name-only`,
  `--tb=short`. Logs, lockfiles, minified bundles and generated code are the usual big tenants.
- **Keep the prefix stable.** Don't switch model or reasoning effort mid-session, and don't
  rewrite earlier context. If a subtask needs a different effort, give it its own session.
- **At a task boundary**, run `rent_calc.py compact` with your best estimate of dead context and
  requests left, then suggest `/compact` or a new session only if the numbers say so.
- **When the user asks where the tokens went**, run `rent_audit.py --codex-latest`.

Read `references/codex-playbook.md` for config.toml levers, a break-even table for narrow reads,
and installation paths.

## Going deeper

- `references/rent-model.md`: the maths behind every rule, with derivations and stated
  assumptions. Read it when a user challenges a recommendation or a case falls outside the rules.
- `references/api-playbook.md`: OpenAI API parameters and patterns by model generation.
- `references/codex-playbook.md`: Codex CLI behaviour and configuration.
