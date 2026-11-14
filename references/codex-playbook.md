# Codex CLI playbook

For when the agent is Codex and the bill is the session itself. Checked against the Codex docs on
2026-11-14. Codex changes often: check `codex --help` and the config reference
(https://developers.openai.com/codex/config-reference) when a key is rejected.

## Contents
1. Install
2. Why agent sessions are a rent problem
3. Narrow or wide: break-even table
4. Command recipes
5. Session hygiene
6. config.toml levers
7. Auditing a session

## 1. Install

Codex discovers skills in:

| Scope | Path |
|---|---|
| user | `$HOME/.agents/skills/openai-token-rent/` |
| repo | `<repo>/.agents/skills/openai-token-rent/` (also `$CWD/.agents/skills` and the parent folder when inside a Git repo) |
| admin | `/etc/codex/skills/` |

Invoke it explicitly with `$openai-token-rent` or through `/skills`. It also triggers on its own
when a task matches the description. `agents/openai.yaml` sets the display name and keeps
implicit invocation on. Codex lists skills within ~2% of the context window
(`skills.max_context_tokens`, capped at 10,000), so this skill keeps its description short and
loads the rest only when used.

## 2. Why agent sessions are a rent problem

Every request in a Codex turn re-sends the whole conversation: AGENTS.md, the skill list, every
command output so far. Cached reads are cheap per token (0.1x or less), but they repeat on every
request, so the size and *timing* of what enters the context decide the bill:

- A 25k-token lockfile `cat` at request 3 of a 40-request session costs entry plus 37 re-reads.
  On gpt-6.1-sol that is about 2.5x its entry price.
- A prefix break (model switch, effort change, rewritten history) bills the whole context at
  entry price again on the next request.
- Output (including reasoning) costs 5-8x input and then sits in context paying rent.

## 3. Narrow or wide: break-even table

Narrow first if it saves more than this many tokens (150-token tool call; GPT-5.6+ entry priced
as a 30m write). Generated with `rent_calc.py admit`; rerun it for other models.

**gpt-6.1-sol**

| context \ requests left | 3 | 10 | 30 | 60 |
|---|---|---|---|---|
| 30k | 1,600 | 1,300 | 800 | 500 |
| 100k | 4,100 | 3,300 | 2,100 | 1,400 |
| 200k | 7,700 | 6,100 | 3,900 | 2,500 |

**gpt-5.3-codex**

| context \ requests left | 3 | 10 | 30 | 60 |
|---|---|---|---|---|
| 30k | 3,200 | 2,100 | 1,100 | 600 |
| 100k | 8,600 | 5,600 | 2,800 | 1,600 |
| 200k | 16,300 | 10,600 | 5,300 | 3,000 |

How to read it: early in a long session (small context, many requests left), a narrow read pays
if it saves even ~500-1,000 tokens. Late in a big context, an extra round trip costs as much as
reading thousands of tokens, so batch reads and read wide once. Add the chance that you'll need
the wide read anyway (`--p-need-wide`). If that chance is high, read wide straight away.

## 4. Command recipes

| Instead of | Use | Why |
|---|---|---|
| `cat big_file.py` | `rg -n "def handler\|class Auth" big_file.py` then `sed -n '120,220p' big_file.py` | admits the 100 lines you need |
| `cat package-lock.json` / `yarn.lock` | `rg -n '"name": "lodash"' -A3 package-lock.json` or `npm ls lodash` | lockfiles are the classic 20k+ tenant |
| `git diff` | `git diff --stat`, then `git diff -- path/to/file` | the stat costs ~1% of the diff |
| `git log` | `git log --oneline -n 20` | |
| `pytest` | `pytest -q -x --tb=short 2>&1 \| tail -n 80` | failures without passing noise |
| `npm test` | `npm test --silent 2>&1 \| tail -n 80` | |
| `ls -R` / `tree` | `rg --files \| head -n 200` or `tree -L 2 -I node_modules` | |
| `curl api` | `curl -s url \| jq '.items[:5] \| map({id,name})'` | |
| reading a minified or generated file | don't; find the source | |

Batch independent reads in one turn. Codex runs them in one request instead of one each.

## 5. Session hygiene

- **AGENTS.md** is entered at the start of every session and re-read on every request. Keep it to
  rules that change behaviour, and put long reference material in files the agent opens when it
  needs them.
- **One task per session.** At a clean boundary, a new session (or `/new`) is compaction with
  perfect recall of what matters, because the agent rereads only what the next task needs.
- **Compaction**: Codex compacts automatically near a token limit. A fixed limit ignores how many
  requests are left. When you're at a boundary with many steps ahead, compact early (`/compact`).
  In the last few steps, don't. Check with
  `rent_calc.py compact --context <now> --keep <what survives> --turns-left <estimate>`.
- **Don't switch model or effort mid-session.** Both rebuild the prefix. Start a separate session
  for the hard subproblem, or run it non-interactively with `codex exec` so its context starts
  small.
- **Let the session end.** A finished task that keeps chatting pays rent on all of its context
  with every message.

## 6. config.toml levers

`~/.codex/config.toml` (or `.codex/config.toml` in a repo):

```toml
model = "gpt-6.1-sol"              # cheaper per token than gpt-6-astra for most coding work
model_reasoning_effort = "medium"  # levels depend on model and client: low, medium, high, xhigh, max...
model_reasoning_summary = "concise" # auto | concise | detailed | none
model_verbosity = "low"            # low | medium | high
# service_tier = "fast"            # maps to "priority": ~2-2.5x price. Only when latency matters.
# model_auto_compact_token_limit = 180000   # see section 5; check the key in your Codex version
# skills.max_context_tokens = 4000          # cap the skill list's share of context
```

Pick effort per kind of work, not per session mood. Use profiles if your Codex version has them
(check `codex --help` for `--profile`), or override one run with `-c`:
`codex -c model_reasoning_effort="high" "…"`. Mechanical work (renames, formatting, bulk edits)
is fine at `low`. Debugging that already failed once deserves `high` from the start of a new session.

## 7. Auditing a session

```
python <skill-dir>/scripts/rent_audit.py --codex-latest
python <skill-dir>/scripts/rent_audit.py ~/.codex/sessions/2026/11/14/rollout-*.jsonl
```

The report splits spend into entry, rent, reasoning and output, lists **rent hikes** (requests
whose cached tokens fell far below the previous context, outside compactions), and ranks the
**biggest tenants**: tool outputs by lifetime cost (estimated tokens × requests they stayed in
context). The rollout format is not a public contract. The parser looks for `token_count` events
and `function_call_output` items, so if a new Codex version breaks it, open an issue with a
redacted sample.
