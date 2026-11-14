# OpenAI API playbook

Parameters and patterns for apps, mapped to the rent model. Facts were checked against the OpenAI
docs on 2026-11-14. The API moves fast, so when a parameter is rejected, check the linked guide
and update the SDK before assuming the advice is wrong.

## Contents
1. Two cache generations
2. Layout template
3. Caching parameters
4. Reasoning: effort, context, mid-conversation changes
5. Output levers
6. State and compaction
7. Tiers and routing
8. Usage meter (for rent_audit.py)
9. Pitfalls

## 1. Two cache generations

| | GPT-5.6+ and GPT-6 (explicit) | Earlier models (implicit) |
|---|---|---|
| Read price | 0.1x input (0.05x gpt-6.1-sol, gpt-6.1-terra) | cached_input column, ~0.1x |
| Write price | 1.25x (30m) or 2.5x (12h) | free |
| TTL control | `prompt_cache_options.ttl`: `"30m"` (default) or `"12h"` (Responses only) | `prompt_cache_retention`: `"in_memory"` (typically 5-10 min idle) or `"24h"`. `24h` is per model (the `retention` list in `data/pricing.json`); gpt-5.5 / 5.5-pro accept only `"24h"` |
| Breakpoints | `prompt_cache_options.mode` `"implicit"` or `"explicit"`, per-block `prompt_cache_breakpoint`, max 4 writes per request, optional `prewarm` | automatic prefix matching |
| `prompt_cache_key` | optional, used for separate cache accounting | steers routing; keep each key under ~15 requests/min across its prefixes |
| Minimum | 1,024 visible input tokens | 1,024 tokens; gpt-5.5 caches in 2,048-token steps; reported cached tokens are rounded down to 128 |

Guide: https://developers.openai.com/api/docs/guides/prompt-caching. Look up the exact
`prompt_cache_breakpoint` shape there before writing it, because it is new and SDK typings lag.

Rule 2 in practice: on the explicit generation, don't cache what won't be reused. A per-request
block with a breakpoint after it pays 1.25x for nothing.

## 2. Layout template

The prefix includes everything the model sees, in order: hidden OpenAI content, then **tool
definitions**, instructions, input items, and images or files in the history. `text.format`
(structured output schema), `reasoning.effort` and `text.verbosity` also change the prefix.

```python
# Python, Responses API: blocks ordered by churn per token (run strata_plan.py for your numbers)
resp = client.responses.create(
    model="gpt-6.1-sol",
    tools=TOOLS,                          # one fixed, sorted list for all traffic
    instructions=SYSTEM,                  # identical for everyone: no names, dates or ids
    input=[
        {"role": "developer", "content": POLICY_AND_EXAMPLES},     # changes on deploy
        {"role": "developer", "content": kb_text},                 # changes hourly or daily
        *history,                                                  # per conversation, append-only
        {"role": "user", "content": f"{user_facts}\n\n{question}"},  # per request: last
    ],
    reasoning={"effort": "low"},
    text={"verbosity": "low"},
    prompt_cache_options={"ttl": "30m"},
    prompt_cache_key="support-answer-v3",  # the template, not the user
)
```

```javascript
// JavaScript, same layout
const resp = await client.responses.create({
  model: "gpt-6.1-sol",
  tools: TOOLS,
  instructions: SYSTEM,
  input: [
    { role: "developer", content: POLICY_AND_EXAMPLES },
    { role: "developer", content: kbText },
    ...history,
    { role: "user", content: `${userFacts}\n\n${question}` },
  ],
  reasoning: { effort: "low" },
  text: { verbosity: "low" },
  prompt_cache_options: { ttl: "30m" },
  prompt_cache_key: "support-answer-v3",
});
```

Serialize anything you put in a prompt deterministically: `json.dumps(x, sort_keys=True,
separators=(",", ":"))`, sorted lists, no set iteration. Round clocks to the coarsest unit the
task needs (usually the day), and put them in the last message.

## 3. Caching parameters

- **TTL** (explicit generation): `30m` unless the gap between reuses exceeds 30 minutes and each
  version is reused at least twice. Then `12h`. Use `rent_calc.py ttl` to check.
- **Retention** (implicit generation): `24h` is the default and costs nothing extra. With Zero
  Data Retention enabled the default becomes `in_memory`; check your data policy before changing it.
- **prompt_cache_key** (implicit generation): one key per prompt template. Above ~15 requests per
  minute per key, shard it (`tmpl:0` … `tmpl:7`, chosen by a stable hash of the conversation) so
  traffic spreads without losing locality. Never use a per-user key on a shared prefix: it splits
  a cache that would otherwise be shared.
- **Dynamic tools**: send the same tools array every time and restrict per request with
  `tool_choice` (an allowed-tools list) or with instructions. Filtering the array changes the
  prefix for everything after it.
- **Verify** after deploy: `usage.input_tokens_details.cached_tokens` against `usage.input_tokens`,
  and on GPT-5.6+ `cache_write_tokens` (split into `ttl_30m_tokens` and `ttl_12h_tokens`). Chat
  Completions reports `usage.prompt_tokens_details.cached_tokens`.

## 4. Reasoning: effort, context, mid-conversation changes

- **Effort values** depend on the model: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`,
  `max`. gpt-6-astra rejects `none` with HTTP 400. gpt-6.1-sol has no `none` and defaults to
  `medium`. gpt-6.1-terra, gpt-6-sol and gpt-6-luna support `none`. gpt-5.5 defaults to `medium`.
- **Reasoning bills as output** (`usage.output_tokens_details.reasoning_tokens`). Reserve room:
  OpenAI suggests at least 25,000 tokens for reasoning plus output when you first tune a task.
- **Effort ladder**: run low, check (schema, tests, a gpt-6-luna judge), escalate on failure.
  `rent_calc.py ladder --input I --low a --high b --p-fail f` tells you whether it pays.
- **Model ladder**: try a cheaper model first (gpt-6.1-sol, gpt-6.1-terra) and retry on gpt-6-astra
  only when the check fails. Caches are per model, so a rare retry usually finds Astra's copy of
  the prefix cold and pays the full write price for it.
  `rent_calc.py model-ladder --input I --prefix P --reasoning-cheap a --reasoning-strong b --p-fail f`
  prices that; pass `--strong-cache-hit` if retries are frequent enough to keep Astra's cache warm.
- **Changing effort mid-conversation** (GPT-6+): leave the top-level `reasoning.effort`
  unchanged and append an input item instead, which keeps the prefix:
  `{"type": "configuration_update", "reasoning": {"effort": "high"}}`.
- **reasoning.context**: `auto`, `current_turn` or `all_turns`. GPT-5.6 defaults to `all_turns`;
  earlier models default to `current_turn`. Carrying all reasoning forward can add rent in long
  chats. Measure `input_tokens` per turn under both settings on a sample before you choose.
- **Pro mode** (`reasoning.mode: "pro"`, GPT-5.6 and GPT-6, Responses): standard prices per
  token, but many more tokens. Treat it as a top rung of the ladder.
- **GPT-6**: when effort is not `none`, remove `temperature`, `top_p` and `top_logprobs`.

## 5. Output levers

Output costs 5-8x input, and in a conversation it then pays rent.

- `text.verbosity: "low"` for anything a program reads.
- Set `max_output_tokens` from observed p95 plus margin, not a round guess. Leave reasoning room.
- Ask for **deltas**: a unified diff, a JSON patch, or changed fields only, instead of the whole
  document again.
- Ask for **handles**: number the inputs (`[d17]`) and have the model answer with ids, enums or
  spans, not quoted text.
- Use **structured outputs** with tight schemas (enums, short keys) so the model can't restate
  the question. Keep the schema stable, because `text.format` is part of the prefix.
- Strip what you won't show. Don't ask for explanations you will discard.

## 6. State and compaction

- `previous_response_id` is the shortest stateful integration. The server holds the
  conversation, and the prefix stays stable automatically.
- Stateless (`store: false`): replay every output item in order, and include encrypted reasoning
  (`include: ["reasoning.encrypted_content"]`) so reasoning is reused instead of redone. For
  function calls, pass the reasoning items back with the tool outputs. Persisted reasoning is
  reusable only within one model family. Switching models mid-conversation loses it.
- **Server-side compaction**: `context_management=[{"type": "compaction", "compact_threshold":
  N}]` on `POST /responses`. The server compacts once the rendered count crosses `N` and emits an
  encrypted compaction item. Choose `N` from the eviction rule: `rent_calc.py compact` tells you
  when compaction pays for a given number of remaining turns. A long-running chat has many turns
  left at any point, so compact earlier. A bounded task should compact late or never.
- **Standalone compaction**: `POST /responses/compact` (SDK `responses.compact`) takes the full
  window and returns a compacted one. Use its output as-is. Don't prune it.

## 7. Tiers and routing

- **Batch** (`/v1/batches`) and **Flex** (`service_tier: "flex"`) are 50% of Standard on every
  line: input, cached input and output. Use them for anything a person isn't waiting on: evals,
  backfills, nightly summaries, embedding prep, classification queues.
- **Fast mode** (formerly Priority, `service_tier: "priority"`) costs about 2-2.5x. Use it only
  where latency is worth money.
- **Model routing** by lifetime cost, not list price. A cheap model that fails and escalates pays
  for both calls. For high-volume, focused tasks start at gpt-6-luna ($0.10 / $0.50). Use
  gpt-6.1-sol for complex work at lower cost than gpt-6-astra.
- Long context (>272k input) doubles the input price on models that list it. Keep agent contexts
  under the threshold by compacting before it.

## 8. Usage meter (for rent_audit.py)

Log one line per call. `group` is the conversation or prompt template, which lets the audit find
rent hikes along a conversation.

```python
import json, time

def metered(client, log_path, group):
    def create(**kw):
        resp = client.responses.create(**kw)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), "group": group, "model": resp.model,
                                 "usage": resp.usage.model_dump()}) + "\n")
        return resp
    return create
```

```javascript
import { appendFileSync } from "node:fs";
export const metered = (client, logPath, group) => async (params) => {
  const resp = await client.responses.create(params);
  appendFileSync(logPath, JSON.stringify({ t: Date.now(), group, model: resp.model, usage: resp.usage }) + "\n");
  return resp;
};
```

Then run `python <skill-dir>/scripts/rent_audit.py usage.jsonl`. Exported Response objects and
Chat Completions responses work too.

## 9. Pitfalls

- Cached tokens still count toward tokens-per-minute rate limits.
- A prompt under the minimum (1,024 tokens) never caches. Padding a 900-token prompt to 1,024 is
  not worth it unless it is reused many times. Run the numbers.
- On older models a `prompt_cache_key` above ~15 RPM spills to other machines and misses.
- An SDK that rejects `prompt_cache_options` or `prompt_cache_retention` is usually just old. Upgrade it.
- Some community reports (unconfirmed by OpenAI) say partial-prefix matching misbehaves on gpt-5.6.
  If hit rates are far below the plan, check whether whole-prefix breakpoints behave better.
- A model switch, an edited system prompt or a new tool definition invalidates every cache entry
  that includes it. Deploy those changes together, not one by one.
