# openai-token-rent

[![tests](https://github.com/YahyaLendani/openai-token-rent/actions/workflows/test.yml/badge.svg)](https://github.com/YahyaLendani/openai-token-rent/actions/workflows/test.yml)

**Lifetime-cost token optimization for OpenAI models.** An [Agent Skill](https://agentskills.io)
for Codex CLI, Claude Code and any other agent that reads `SKILL.md`, plus five small
zero-dependency Python tools.

Most token advice counts tokens: shorter prompts, smaller models, turn caching on. This skill
prices tokens over their **lifetime**. Every token is a tenant: it pays an entry price, then
**rent** (a cached re-read) on every later request it stays in, and a **rent hike** (full price
again) whenever something before it changes.

```
lifetime cost = entry + rent × requests it stays + hikes when its prefix breaks
```

That one change of accounting produces rules the usual advice gets wrong:

| Usual advice | Token-rent answer |
|---|---|
| Put static content first. | Sort blocks by **churn per token shielded** (Smith's rule). A 60k corpus that changes 4×/hour belongs *before* a 1.5k tool list that changes 2×/hour. |
| Always cache. | On GPT-5.6+ a cache write costs 1.25× (30m) or 2.5× (12h). A write that is never read costs *more* than no cache. 30m pays after 1 hit, 12h after 2. |
| Always grep before reading. | Early in a long agent session, yes. Late in a 200k context, one extra round trip costs as much as reading ~4-8k tokens, so read wide once. |
| Compact at N% of the context window. | Compact when `dead tokens × rent × requests left` beats compaction plus re-entry. Near the end of a task, never. |
| Use the cheapest model and effort. | Use the cheapest **expected** cost: run low effort, check it cheaply, escalate on failure, and keep the prefix when escalating (`configuration_update` on GPT-6). |

## What's inside

```
SKILL.md                    the instructions the agent follows (lean on purpose: it pays rent too)
agents/openai.yaml          Codex display metadata
references/rent-model.md    the maths and derivations behind every rule
references/api-playbook.md  OpenAI API parameters by model generation, code patterns, pitfalls
references/codex-playbook.md Codex CLI habits, config.toml levers, break-even tables
scripts/strata_lint.py      finds cache-breaking code (CI-friendly exit codes)
scripts/strata_plan.py      cheapest block order + breakpoints + TTLs for a prompt
scripts/rent_audit.py       splits real spend from API logs or Codex session files
scripts/rent_calc.py        admit / compact / ladder / model-ladder / ttl break-even calculator
scripts/refresh_pricing.py  fetches the latest pricing table into a local cache
install.sh  install.ps1     one-command installers
data/pricing.json           prices as of the date inside it (refreshed automatically)
examples/  tests/  evals/
```

## Install

One command. It finds Codex CLI and Claude Code on your machine and installs the skill for each
one it finds. There is nothing to configure afterwards: the agent loads the skill by itself
when OpenAI costs come up.

**macOS, Linux, WSL, Git Bash:**

```bash
curl -fsSL https://raw.githubusercontent.com/YahyaLendani/openai-token-rent/main/install.sh | sh
```

**Windows PowerShell:**

```powershell
irm https://raw.githubusercontent.com/YahyaLendani/openai-token-rent/main/install.ps1 | iex
```

Run the same command again to upgrade. The installer downloads the latest release from this
repository and copies it to `~/.agents/skills/openai-token-rent` (Codex) and/or
`~/.claude/skills/openai-token-rent` (Claude Code). If neither agent is found it uses
`~/.agents/skills`. It uploads nothing about you, touches nothing outside those folders, and leaves a git clone alone.
To read it before you run it, open [install.sh](install.sh) or [install.ps1](install.ps1).

**Other ways:**

- *git clone*, if you want to pull updates yourself:
  `git clone https://github.com/YahyaLendani/openai-token-rent ~/.agents/skills/openai-token-rent`
  (use `~/.claude/skills/` for Claude Code, or `.agents/skills/` / `.claude/skills/` inside a
  repo for one project only).
- *claude.ai*: download `openai-token-rent.skill` from the
  [latest release](https://github.com/YahyaLendani/openai-token-rent/releases/latest) and upload
  it in the Skills section of claude.ai settings.
- *Anything else*: point your agent at `SKILL.md`.

In Codex you can also call it by name with `$openai-token-rent`. The scripts need Python 3.9+
and nothing else; the agent runs them itself and falls back to doing the maths by hand if Python
is missing. `tiktoken` is used for token counts if it is installed, otherwise the scripts
estimate characters ÷ 4.

## The tools, on the bundled examples

**Lint**: `python scripts/strata_lint.py examples/leaky_app.py`

```
examples/leaky_app.py:17: [high] TR001 clock-in-prefix
examples/leaky_app.py:17: [high] TR004 per-request-value-in-system
examples/leaky_app.py:23: [high] TR002 random-in-prefix
examples/leaky_app.py:30: [high] TR008 retention-on-new-models
examples/leaky_app.py:18: [medium] TR005 dynamic-tool-list
...
8 finding(s): 5 high, 2 medium, 1 low
```

**Plan**: `python scripts/strata_plan.py examples/strata.example.json`

```
 #  segment                tokens    prefix  breakpoint  hit rate
 1  system_rules            1,200     1,200
 2  tool_defs               3,500     4,700
 3  help_center            28,000    32,700  30m             100%
 4  chat_history            2,500    35,200  12h              94%
 5  user_profile              600    35,800  12h              93%
 6  today_date                 20    35,820  12h              90%
 7  latest_message            120    35,940

input cost/request  recommended $0.0048   current $0.0199   no cache $0.0719
reordering saves 75.9% of input spend
```

The 20-token `today_date` block sat second in the original order. Moving it is most of the saving.

**Audit**: `python scripts/rent_audit.py --codex-latest` (or any API usage JSONL)

```
== codex-rollout.example.jsonl  (30 requests, gpt-6.1-sol)
   total $0.8670   cache hit rate 90%
   entry (new tokens)          $0.0000    0%
   cache writes                $0.3066   35%  ###########
   rent (cached re-reads)      $0.1104   13%  ####
   reasoning                   $0.2700   31%  #########
   visible output              $0.1800   21%  ######
   rent hikes: 1 (requests 15) lost $0.1314
   biggest tenants (lifetime cost = entry + rent until compaction or end):
       $0.1050  ~ 25,000 tok x  17 req  shell: cat package-lock.json
```

**Decide**: `python scripts/rent_calc.py compact --context 180000 --keep 40000 --turns-left 30`

```
[compact] COMPACT: 140,000 dead tokens cost $0.0140 per request in rent; compacting costs
$0.1540 once. Compact only if more than 11.0 requests remain (you said 30).
```

## Keeping prices current

`data/pricing.json` carries an `as_of` date and its source URL. When it is more than 60 days
old, the scripts say so and the agent runs `scripts/refresh_pricing.py`, which downloads the
current table from this repository (one GET, nothing sent), checks it, and saves it to a cache
(`%LOCALAPPDATA%\token-rent` on Windows, `~/.cache/token-rent` elsewhere). Every script then
uses whichever copy is newer, so the prices stay current without reinstalling. If the network is
blocked, the bundled table is used and the agent says how old it is.

OpenAI changes prices and models often, so PRs that refresh the table are the most useful
contribution. Point `TOKEN_RENT_PRICING=/path/to/your.json` at a private table for negotiated
rates; it always wins over the cache.

## Accuracy and limits

The planner and calculator are models. They assume steady traffic, independent change processes
and list prices, and they state their inputs so you can argue with them. Treat their numbers as
estimates until `rent_audit.py` measures the change after deploy. The linter is a line-based
heuristic: read each finding before you act on it. The Codex rollout format is not a public
contract. The parser is defensive, but a new Codex release may need an update. Details are in
`references/rent-model.md` §11.

**What has been tested.** The unit tests and behaviour evals run on Claude Code. The Codex log
parser was checked field by field against the rollout format in the
[openai/codex](https://github.com/openai/codex) source (`codex-rs/history`, `codex-rs/rollout`,
`codex-rs/protocol`) on 2026-11-14. It has been tested on synthetic rollout files, not yet in a
live Codex session. Codex records cache writes as one total with no TTL split, so the audit
prices them at the 30m rate. If you use Codex, please open an issue with what
`rent_audit.py --codex-latest` printed (the summary only, not your session file), especially if
it errors or the numbers look wrong.

## How this differs from related tools

Good tools already exist for **measuring** cache behaviour after the fact. This skill is about
**deciding** ahead of time: what to put in context, in what order, for how long, and when to
compact. As far as their READMEs say, none of the projects below prices a block of context
over the requests it has left, or works out a break-even point for compaction, TTL or model
escalation. Facts below are from each project's README as of 2026-11-14.

| Project | What it does | Overlap | What token-rent adds |
|---|---|---|---|
| [promptcachelint](https://github.com/OsmnvAslan/promptcachelint) | Python CLI that diffs recorded requests and names the block and offset that broke the cache. Lint rules for dynamic prefix content, breakpoint count and `prompt_cache_key` changes. | `strata_lint` | Explains a miss after it happens but does not reorder. Token-rent also plans the order and prices each choice. |
| [prefixcash](https://github.com/Isk4R1oT/prefixcash) | Hit rate and dollars saved from call logs (CLI, TUI, LiteLLM callback). Suggests static-first order and replays fix variants. | `rent_audit` | Its numbers look back. Token-rent's calculator looks forward: is it worth admitting, compacting or keeping this block? |
| [prompt-cache-optimizer](https://github.com/leonhail-nell/prompt-cache-optimizer) | TypeScript SDK wrappers: per-call savings, automatic Anthropic breakpoints, canonical tool and document order. | `strata_plan` | It canonicalizes order so payloads match. Token-rent orders by churn per token shielded (Smith's rule) and picks TTLs. |
| [token-optimizer](https://github.com/alexgreensh/token-optimizer) | Claude Code plugin for context waste, compaction survival and cache keep-warm across many coding agents. Anthropic pricing, PolyForm Noncommercial license. | Codex session audit | Built for OpenAI pricing, explicit vs implicit caching, and OpenAI API apps as well as Codex. MIT. |
| [cache-audit](https://skills.sh/darraghh1/my-claude-setup/cache-audit) | Claude Code skill that scores your config against prompt-caching practice. | Prefix-order advice | Works on OpenAI apps and Codex sessions, and puts numbers on the trade-offs. |
| [codex-token-skills](https://github.com/lokikill123/codex-token-skills) | Codex skills for terse output and memory after compaction, aimed at DeepSeek. | Codex, frozen prefixes | Measures and prices cost instead of asserting savings. |

They work well together. Run promptcachelint or prefixcash in production to catch misses, and
use token-rent when you are designing the prompt or deciding mid-session. If a project here
has added lifetime pricing since this was written, please open a PR to fix this table.

## Development

```bash
python -m unittest discover -s tests -v
python tests/make_examples.py      # regenerate the synthetic example logs
```

`evals/` holds behaviour tests in the `claude plugin eval` layout. They are real, billed model runs.

## License

MIT. Not affiliated with or endorsed by OpenAI.
