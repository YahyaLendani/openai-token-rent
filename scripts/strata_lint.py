#!/usr/bin/env python3
"""strata_lint: find code that breaks OpenAI prompt caching (a "rent hike").

A cached prefix survives only while every token before the break point is identical. One
timestamp, request id or shuffled example near the top of a prompt turns every later token from
a cheap cached read into a full-price (or, on GPT-5.6+, write-price) token on every request.

Scans Python / JS / TS sources that talk to OpenAI, plus prompt template files. Heuristic and
line-based: expect a few false positives, and read each finding before changing code.

  python strata_lint.py path/to/repo [--json] [--fail-on high|medium|never] [--all]
Exit code 1 when a finding at or above --fail-on exists (default: high), so it can gate CI.
Put the comment "strata-lint: skip-file" anywhere in a file to exclude it.
"""
# strata-lint: skip-file   (this file's own patterns would match themselves)
from __future__ import annotations

import argparse
import json
import os
import re
import sys

CODE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}
TEMPLATE_EXT = {".j2", ".jinja", ".jinja2", ".prompt", ".tmpl", ".hbs", ".mustache", ".liquid"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "dist", "build", "__pycache__",
             ".next", ".turbo", "site-packages", ".mypy_cache", ".pytest_cache", "coverage"}
SEV_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}
WINDOW = 12

OPENAI_HINT = re.compile(r"openai|responses\.create|chat\.completions|\.responses\(|prompt_cache", re.I)
SYSTEM_ANCHOR = re.compile(
    r"""instructions\s*[=:]|["']?role["']?\s*[:=]\s*["'](system|developer)["']|system_?prompt|"""
    r"""developer_?prompt|SYSTEM_PROMPT|DEVELOPER_PROMPT|SystemMessage\(|system\s*=\s*f?["']""", re.I)
CALL_ANCHOR = re.compile(r"responses\.create|chat\.completions\.create|\.responses\.stream|"
                         r"responses\.parse|chat\.completions\.parse|client\.responses|Runner\.run", re.I)

CLOCK = re.compile(r"datetime\.(now|utcnow|today)\(|date\.today\(|time\.time\(|time\.strftime|"
                   r"Date\.now\(|new Date\(|toISOString\(|toLocale(Date|Time)?String\(|moment\(|dayjs\(|"
                   r"strftime\(|\bnow\(\)")
RANDOM = re.compile(r"uuid|randomUUID|random\.(shuffle|choice|choices|sample|random|randint)\(|"
                    r"Math\.random|nanoid|secrets\.token|shuffle\(")
DUMPS = re.compile(r"json\.dumps\((?![^)]*sort_keys\s*=\s*True)")
INTERP = re.compile(r"""\bf["'].*\{[^}]+\}|\$\{[^}]+\}|\.format\(|%\s*\(|%s""")
PERSONAL = re.compile(r"(user|name|customer|account|email|session|request|tenant|org|locale|"
                      r"timezone|tz|plan|tier|id)\w*", re.I)
DYN_TOOLS = re.compile(r"""\btools\s*[=:]\s*(\[[^\]]*\bfor\b|[\w.]+\.filter\(|[\w.]+\(|\[\s*\*|\[\s*\.\.\.)""")
SET_ITER = re.compile(r"for\s+\w+\s+in\s+set\(|list\(set\(|\[\.\.\.new Set\(")
VAR_EFFORT = re.compile(r"""["']?(effort|verbosity)["']?\s*[:=]\s*(?!["'])[A-Za-z_][\w.\[\]"']*""")
RETENTION = re.compile(r"""\bprompt_cache_retention\s*=(?!=)|["']prompt_cache_retention["']\s*:|"""
                       r"""\bprompt_cache_retention\s*:\s*["']""")
RETENTION_INMEM = re.compile(r"""prompt_cache_retention["']?\s*[:=]\s*["']in_memory""")
NEW_GEN_MODEL = re.compile(r"""["'`]gpt-(5\.6|6)""")
SKIP_MARK = "strata-lint: skip-file"
GPT55 = re.compile(r"""["']gpt-5\.5(-pro)?["']""")
CACHE_KEY = re.compile(r"prompt_cache_key")
TEMPLATE_VOLATILE = re.compile(r"\{\{\s*(now|today|date|time|timestamp|current_\w+|user\w*|name|"
                               r"session\w*|request_id|uuid|random\w*)\b", re.I)

RULES = {
    "TR001": ("clock-in-prefix", "high",
              "A clock value sits in or near the system/developer prompt. It changes every request "
              "(or every day), so nothing after it can be read from cache.",
              "Move the date/time to the final user message, or round it (e.g. to the day) and place "
              "it after every stable block."),
    "TR002": ("random-in-prefix", "high",
              "A random value or id is generated near the prompt: shuffled few-shot examples, uuids "
              "or nonces make each prefix unique.",
              "Fix the example order (seeded or sorted) and keep ids out of the prompt; send them as "
              "metadata or in the last message."),
    "TR003": ("unstable-serialization", "medium",
              "json.dumps without sort_keys=True near a prompt. Dict order can differ between code "
              "paths, which silently changes the prefix.",
              "Use json.dumps(obj, sort_keys=True, separators=(',', ':')) for anything placed in a prompt."),
    "TR004": ("per-request-value-in-system", "high",
              "The system/developer prompt interpolates a value that looks per-user or per-request. "
              "Every distinct value is a separate cache entry for everything after it.",
              "Keep the system prompt identical for all traffic; pass user facts in a later message, "
              "after the long shared blocks."),
    "TR005": ("dynamic-tool-list", "medium",
              "The tools array is built per request. Tool definitions are part of the cached prefix, "
              "so a filtered or reordered list misses the cache.",
              "Send one fixed, sorted tools array and restrict per request with tool_choice "
              "(allowed tools) or instructions instead."),
    "TR006": ("set-order", "medium",
              "Iteration over a set near prompt or tool assembly. Set order is not stable across "
              "processes, so the rendered prefix can differ.",
              "Sort before rendering: sorted(set(...))."),
    "TR007": ("effort-changes-mid-conversation", "low",
              "reasoning effort / verbosity comes from a variable. Changing it between turns of one "
              "conversation changes the prefix.",
              "Pick it once per conversation. On GPT-6+, change effort with a configuration_update "
              "input item and keep the top-level value fixed."),
    "TR008": ("retention-on-new-models", "high",
              "prompt_cache_retention is used in a file that targets GPT-5.6+/GPT-6. Those models use "
              "prompt_cache_options (ttl '30m' or '12h') instead.",
              "Replace with prompt_cache_options={'ttl': '30m'} (or '12h' when reuse gaps exceed 30 min "
              "and each prefix is reused 2+ times)."),
    "TR009": ("in-memory-on-gpt-5.5", "high",
              "gpt-5.5 / gpt-5.5-pro accept only prompt_cache_retention='24h'.",
              "Use '24h' or drop the parameter."),
    "TR010": ("no-cache-key", "info",
              "This file calls OpenAI without prompt_cache_key. On pre-5.6 models the key steers "
              "routing so requests with the same prefix land where the cache is.",
              "Set prompt_cache_key to the prompt template id (not the user id), keeping each key "
              "under ~15 requests/min; shard as 'tmpl:a'..'tmpl:n' above that."),
    "TR011": ("volatile-template-head", "high",
              "A prompt template renders a volatile variable in its first third.",
              "Move clock, user and request variables to the end of the template."),
}


USER_LINE = re.compile(r"""["']?role["']?\s*[:=]\s*["']user["']""")
ASSIGN = re.compile(r"^\s*(?:const\s+|let\s+|var\s+)?([A-Za-z_]\w*)\s*(?::\s*[\w\[\], ]+)?=(?!=)")
MUTATE = re.compile(r"(?:shuffle|sample)\(\s*([A-Za-z_]\w*)")
DOWNGRADE = {"high": "medium", "medium": "low", "low": "info", "info": "info"}


def finding(path, line, rule, text, severity=None, note=""):
    name, sev, msg, fix = RULES[rule]
    return {"path": path, "line": line, "rule": rule, "name": name, "severity": severity or sev,
            "message": msg + note, "fix": fix, "code": text.strip()[:160]}


def near(idx_set, i, window=WINDOW):
    return any(abs(i - j) <= window for j in idx_set)


def _vars(line):
    out = set()
    m = ASSIGN.match(line)
    if m:
        out.add(m.group(1))
    out.update(MUTATE.findall(line))
    return out


def placement(i, line, lines, sys_lines, user_lines, prompt_lines):
    """Where does a volatile value on line i end up?
    'system' (in or flowing into a system/developer block), 'user' (only into a user message,
    which is the right place), 'near' (close to a prompt, flow unknown) or None."""
    if i in sys_lines:
        return "system"
    names = _vars(line)
    if names:
        pats = [re.compile(r"\b" + re.escape(v) + r"\b") for v in names]
        uses = [j for j in range(i + 1, len(lines))          # a value is used after it is made
                if any(p.search(lines[j]) for p in pats)
                and not lines[j].lstrip().startswith(("#", "//", "*"))]
        if any(j in sys_lines for j in uses):
            return "system"
        if uses and all(j in user_lines for j in uses):
            return "user"
    return "near" if near(prompt_lines, i) else None


def lint_code(path, lines):
    out = []
    sys_lines = {i for i, l in enumerate(lines) if SYSTEM_ANCHOR.search(l)}
    user_lines = {i for i, l in enumerate(lines) if USER_LINE.search(l)}
    call_lines = {i for i, l in enumerate(lines) if CALL_ANCHOR.search(l)}
    prompt_lines = sys_lines | call_lines
    whole = "\n".join(lines)
    new_gen = bool(NEW_GEN_MODEL.search(whole))

    def volatile(i, l, rule):
        where = placement(i, l, lines, sys_lines, user_lines, prompt_lines)
        if where == "system":
            out.append(finding(path, i + 1, rule, l))
        elif where == "near":
            out.append(finding(path, i + 1, rule, l, DOWNGRADE[RULES[rule][1]],
                               " (Found near a prompt; check whether it reaches the stable part.)"))

    for i, l in enumerate(lines):
        s = l.strip()
        if s.startswith(("#", "//", "*", "/*", "import ", "from ")) or s.startswith("require("):
            continue
        n = i + 1
        if CLOCK.search(l):
            volatile(i, l, "TR001")
        if RANDOM.search(l):
            volatile(i, l, "TR002")
        if DUMPS.search(l):
            volatile(i, l, "TR003")
        if i in sys_lines and INTERP.search(l):
            m = re.findall(r"\{([^}]+)\}", l)
            if any(PERSONAL.search(x) for x in m) or "%s" in l or ".format(" in l:
                out.append(finding(path, n, "TR004", l))
        if DYN_TOOLS.search(l) and not re.search(r"\btools\s*[=:]\s*sorted\(", l):
            out.append(finding(path, n, "TR005", l))
        if SET_ITER.search(l):
            volatile(i, l, "TR006")
        if VAR_EFFORT.search(l) and near(call_lines, i, 20):
            out.append(finding(path, n, "TR007", l))
        if RETENTION.search(l) and new_gen and near(call_lines, i, 30):
            out.append(finding(path, n, "TR008", l))
        if RETENTION_INMEM.search(l) and GPT55.search(whole) and near(call_lines, i, 30):
            out.append(finding(path, n, "TR009", l))
    if call_lines and not CACHE_KEY.search(whole):
        first = min(call_lines)
        out.append(finding(path, first + 1, "TR010", lines[first]))
    return out


def lint_template(path, lines):
    head = max(1, len(lines) // 3)
    return [finding(path, i + 1, "TR011", l) for i, l in enumerate(lines[:head])
            if TEMPLATE_VOLATILE.search(l)]


def walk(paths):
    for p in paths:
        if os.path.isfile(p):
            yield p
            continue
        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for f in files:
                yield os.path.join(root, f)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fail-on", default="high", choices=["high", "medium", "low", "info", "never"])
    ap.add_argument("--all", action="store_true", help="scan code files even if they never mention OpenAI")
    ap.add_argument("--min-severity", default="low", choices=["info", "low", "medium", "high"])
    a = ap.parse_args(argv)

    findings = []
    for path in walk(a.paths):
        ext = os.path.splitext(path)[1].lower()
        if ext not in CODE_EXT and ext not in TEMPLATE_EXT:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if SKIP_MARK in text:
            continue
        lines = text.splitlines()
        if ext in TEMPLATE_EXT:
            findings += lint_template(path, lines)
        elif a.all or OPENAI_HINT.search(text):
            findings += lint_code(path, lines)

    floor = SEV_RANK[a.min_severity]
    findings = [f for f in findings if SEV_RANK[f["severity"]] >= floor]
    findings.sort(key=lambda f: (-SEV_RANK[f["severity"]], f["path"], f["line"]))

    if a.json:
        print(json.dumps(findings, indent=2))
    else:
        for f in findings:
            print(f"{f['path']}:{f['line']}: [{f['severity']}] {f['rule']} {f['name']}")
            print(f"    {f['code']}")
            print(f"    why: {f['message']}")
            print(f"    fix: {f['fix']}")
        counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("high", "medium", "low", "info")}
        print(f"\n{len(findings)} finding(s): " + ", ".join(f"{v} {k}" for k, v in counts.items() if v)
              if findings else "no cache-breaking patterns found")
    if a.fail_on == "never":
        return 0
    return 1 if any(SEV_RANK[f["severity"]] >= SEV_RANK[a.fail_on] for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
