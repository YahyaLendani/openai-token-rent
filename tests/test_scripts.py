"""Run from the repo root:  python -m unittest discover -s tests -v"""
import io
import json
import math
import os
import sys
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import rentlib as rl          # noqa: E402
import rent_calc              # noqa: E402
import strata_plan            # noqa: E402
import strata_lint            # noqa: E402
import rent_audit             # noqa: E402

EX = os.path.join(ROOT, "examples")
PRICING = rl.load_pricing(warn=False)


def run(fn, argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = fn(argv)
    return code, buf.getvalue()


class Pricing(unittest.TestCase):
    def test_snapshot_resolves_to_base_model(self):
        key, _ = rl.resolve_model(PRICING, "gpt-6.1-sol-2026-09-01")
        self.assertEqual(key, "gpt-6.1-sol")

    def test_longest_prefix_wins(self):
        self.assertEqual(rl.resolve_model(PRICING, "gpt-5.5-pro")[0], "gpt-5.5-pro")
        self.assertEqual(rl.resolve_model(PRICING, "gpt-5.5-2026-04-01")[0], "gpt-5.5")

    def test_unknown_model_raises(self):
        with self.assertRaises(KeyError):
            rl.rates_for(PRICING, "not-a-model")

    def test_long_context_switches_rates(self):
        short = rl.rates_for(PRICING, "gpt-6.1-sol", 100_000)
        long = rl.rates_for(PRICING, "gpt-6.1-sol", 300_000)
        self.assertAlmostEqual(long.p_in, 2 * short.p_in)

    def test_batch_halves(self):
        self.assertAlmostEqual(rl.rates_for(PRICING, "gpt-6.1-sol", tier="batch").p_in,
                               rl.rates_for(PRICING, "gpt-6.1-sol").p_in / 2)


class Usage(unittest.TestCase):
    def test_responses_usage(self):
        u = rl.normalize_usage({"input_tokens": 5000, "input_tokens_details": {
            "cached_tokens": 3000, "cache_write_tokens": 1000,
            "cache_write_tokens_details": {"ttl_30m_tokens": 1000, "ttl_12h_tokens": 0}},
            "output_tokens": 900, "output_tokens_details": {"reasoning_tokens": 600}}, "gpt-6.1-sol")
        self.assertEqual((u.fresh, u.cached, u.written, u.visible), (1000, 3000, 1000, 300))
        c = rl.cost_of(u, PRICING)
        self.assertAlmostEqual(c.write, 1000 * 2e-6 * 1.25)
        self.assertAlmostEqual(c.rent, 3000 * 0.1e-6)

    def test_chat_and_codex_usage(self):
        chat = rl.normalize_usage({"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 4},
                                   "completion_tokens": 3}, "gpt-5.4")
        self.assertEqual((chat.input, chat.cached, chat.output), (10, 4, 3))
        cx = rl.normalize_usage({"input_tokens": 10, "cached_input_tokens": 6, "output_tokens": 5,
                                 "reasoning_output_tokens": 2}, "gpt-6.1-sol")
        self.assertEqual((cx.cached, cx.reasoning, cx.visible), (6, 2, 3))

    def test_codex_cache_writes_are_priced(self):
        # Codex TokenUsage carries cache_write_input_tokens (no TTL split) on GPT-5.6+.
        cx = rl.normalize_usage({"input_tokens": 5000, "cached_input_tokens": 3000,
                                 "cache_write_input_tokens": 1000, "output_tokens": 10,
                                 "reasoning_output_tokens": 0}, "gpt-6.1-sol")
        self.assertEqual((cx.fresh, cx.written, cx.writes), (1000, 1000, {"30m": 1000}))
        self.assertAlmostEqual(rl.cost_of(cx, PRICING).write, 1000 * 2e-6 * 1.25)

    def test_unknown_write_ttl_on_implicit_model_costs_plain_input(self):
        u = rl.Usage(model="gpt-5.4", input=2000, writes={"30m": 1000})
        self.assertAlmostEqual(rl.cost_of(u, PRICING).write, 1000 * 2.5e-6)


class Calc(unittest.TestCase):
    def test_compact_breakeven_is_consistent(self):
        _, out = run(rent_calc.main, ["compact", "--context", "180000", "--keep", "40000",
                                      "--turns-left", "30", "--json"])
        d = json.loads(out)
        self.assertEqual(d["verdict"], "compact")
        per_turn = 140000 * 0.1e-6
        self.assertAlmostEqual(d["breakeven_turns_left"], d["compact_usd"] / per_turn)
        _, out = run(rent_calc.main, ["compact", "--context", "180000", "--keep", "40000",
                                      "--turns-left", "4", "--json"])
        self.assertEqual(json.loads(out)["verdict"], "keep")

    def test_admit_late_and_small_prefers_wide(self):
        _, out = run(rent_calc.main, ["admit", "--tokens", "3000", "--narrow", "600", "--context",
                                      "250000", "--turns-left", "2", "--p-need-wide", "0.5", "--json"])
        self.assertEqual(json.loads(out)["verdict"], "wide")

    def test_admit_big_early_prefers_narrow(self):
        _, out = run(rent_calc.main, ["admit", "--tokens", "25000", "--narrow", "1500", "--context",
                                      "30000", "--turns-left", "40", "--json"])
        self.assertEqual(json.loads(out)["verdict"], "narrow")

    def test_ladder_threshold(self):
        _, out = run(rent_calc.main, ["ladder", "--input", "20000", "--low", "1500", "--high", "12000",
                                      "--p-fail", "0.3", "--json"])
        d = json.loads(out)
        self.assertEqual(d["verdict"], "ladder")
        self.assertGreater(d["max_p_fail"], 0.3)

    ML = ["model-ladder", "--input", "50000", "--prefix", "45000", "--reasoning-cheap", "4000",
          "--reasoning-strong", "4000", "--p-fail", "0.25", "--json"]

    def test_model_ladder_hand_maths(self):
        d = json.loads(run(rent_calc.main, self.ML)[1])
        self.assertEqual((d["cheap"], d["strong"]), ("gpt-6.1-sol", "gpt-6-astra"))
        # Astra, warm: 45k x $1 + 5k x $10 x 1.25 (write) + 4.8k x $50, per 1M tokens
        self.assertAlmostEqual(d["direct_usd"], 0.045 + 0.0625 + 0.24)
        # Sol 6.1, warm: 45k x $0.10 + 5k x $2 x 1.25 + 4.8k x $10
        self.assertAlmostEqual(d["first_try_usd"], 0.0045 + 0.0125 + 0.048)
        # a cold retry writes the 45k prefix at 1.25x instead of reading it at $1
        self.assertAlmostEqual(d["cold_retry_penalty_usd"], 45000 * 10e-6 * 1.25 - 0.045)
        self.assertAlmostEqual(d["ladder_usd"], d["first_try_usd"] + 0.25 * d["retry_usd"])

    def test_model_ladder_cold_cache_lowers_break_even(self):
        cold = json.loads(run(rent_calc.main, self.ML)[1])
        warm = json.loads(run(rent_calc.main, self.ML + ["--strong-cache-hit", "1"])[1])
        self.assertLess(cold["max_p_fail"], warm["max_p_fail"])
        self.assertLess(warm["ladder_usd"], cold["ladder_usd"])
        # with a 60% failure rate the cold-cache ladder loses
        d = json.loads(run(rent_calc.main, self.ML[:-2] + ["0.6", "--json"])[1])
        self.assertEqual(d["verdict"], "direct")

    def test_ttl_choices(self):
        _, out = run(rent_calc.main, ["ttl", "--prefix", "30000", "--gap", "120", "--reuses", "50", "--json"])
        self.assertEqual(json.loads(out)["verdict"], "30m")
        _, out = run(rent_calc.main, ["ttl", "--prefix", "30000", "--gap", "3600", "--reuses", "6", "--json"])
        self.assertEqual(json.loads(out)["verdict"], "12h")
        _, out = run(rent_calc.main, ["ttl", "--prefix", "30000", "--gap", "3600", "--reuses", "1", "--json"])
        self.assertEqual(json.loads(out)["verdict"], "none")      # a write that is never read is waste

    def test_ttl_break_even_maths(self):
        # 30m: one hit already pays for the 1.25x write; 12h needs two (2.5 > 1 + 0.1 + ... )
        self.assertLess(rl.cached_block_mult(1.25, 0.1, 1), 1.0)
        self.assertGreater(rl.cached_block_mult(2.5, 0.1, 1), 1.0)
        self.assertLess(rl.cached_block_mult(2.5, 0.1, 2), 1.0)


class Plan(unittest.TestCase):
    def test_example_moves_volatile_segments_down(self):
        _, out = run(strata_plan.main, [os.path.join(EX, "strata.example.json"), "--json"])
        d = json.loads(out)
        order = d["recommended_order"]
        self.assertEqual(order[0], "system_rules")
        self.assertEqual(order[-1], "latest_message")
        self.assertLess(order.index("help_center"), order.index("user_profile"))
        self.assertLess(order.index("help_center"), order.index("today_date"))
        self.assertLess(d["usd_per_request"]["recommended"], d["usd_per_request"]["current"])
        self.assertLessEqual(sum(1 for r in d["rows"] if r["breakpoint"]), 4)

    def test_size_beats_rate_when_shielding_more(self):
        # Smith's rule: a big, slightly volatile block can go before a small, stabler one.
        spec = {"model": "gpt-6.1-sol", "requests_per_hour": 3600, "segments": [
            {"name": "tools", "tokens": 1500, "changes_per_hour": 2},
            {"name": "corpus", "tokens": 60000, "changes_per_hour": 4},
            {"name": "q", "tokens": 100, "per_request": True}]}
        path = os.path.join(ROOT, "tests", "_tmp_spec.json")
        with open(path, "w") as fh:
            json.dump(spec, fh)
        try:
            _, out = run(strata_plan.main, [path, "--json"])
        finally:
            os.remove(path)
        self.assertEqual(json.loads(out)["recommended_order"][:2], ["corpus", "tools"])

    def test_implicit_model_reports_retention(self):
        _, out = run(strata_plan.main, [os.path.join(EX, "strata.example.json"), "--model", "gpt-5.4", "--json"])
        self.assertEqual(json.loads(out)["prompt_cache_retention"], "24h")


class Lint(unittest.TestCase):
    def test_leaky_app(self):
        code, out = run(strata_lint.main, [os.path.join(EX, "leaky_app.py"), "--json", "--fail-on", "high"])
        rules = {f["rule"] for f in json.loads(out)}
        self.assertEqual(code, 1)
        self.assertTrue({"TR001", "TR002", "TR003", "TR004", "TR005", "TR007", "TR008"} <= rules, rules)

    def test_fixed_app_is_clean(self):
        code, out = run(strata_lint.main, [os.path.join(EX, "fixed_app.py"), "--json", "--min-severity", "info"])
        self.assertEqual(json.loads(out), [])
        self.assertEqual(code, 0)


class Audit(unittest.TestCase):
    def test_codex_session(self):
        _, out = run(rent_audit.main, [os.path.join(EX, "codex-rollout.example.jsonl"), "--json"])
        rep = json.loads(out)[0]
        self.assertEqual(rep["requests"], 30)                    # duplicate token_count skipped
        self.assertEqual([h["request"] for h in rep["hikes"] if not h["after_compaction"]], [15])
        self.assertIn("package-lock.json", rep["tenants"][0]["label"])
        self.assertEqual(rep["compactions"], [22])
        self.assertTrue(math.isclose(sum(v for k, v in rep["usd"].items() if k != "total"),
                                     rep["usd"]["total"]))

    def test_codex_content_item_outputs(self):
        # function_call_output.output may be a list of content items; images get a flat estimate.
        lines = [
            {"timestamp": "t", "type": "turn_context", "payload": {"model": "gpt-6.1-sol"}},
            {"timestamp": "t", "type": "response_item", "payload": {
                "type": "function_call", "name": "view_image", "arguments": "{\"path\": \"a.png\"}", "call_id": "c1"}},
            {"timestamp": "t", "type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "c1", "output": [
                    {"type": "input_text", "text": "x" * 400},
                    {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 200000}]}},
        ]
        _, _, tenants, _ = rent_audit.parse_codex(lines, "gpt-6.1-sol")
        self.assertEqual(len(tenants), 1)
        self.assertLess(tenants[0]["tokens"], 2000)           # base64 is not counted as text
        self.assertGreaterEqual(tenants[0]["tokens"], rent_audit.IMAGE_TOKENS)
        self.assertIn("view_image", tenants[0]["label"])

    def test_api_groups_and_advice(self):
        _, out = run(rent_audit.main, [os.path.join(EX, "api-usage.example.jsonl"), "--json"])
        reps = {r["group"]: r for r in json.loads(out)}
        classify = next(v for k, v in reps.items() if "classify" in k)
        self.assertEqual(classify["cache_hit_rate"], 0.0)
        self.assertTrue(any("Reasoning" in t for t in classify["advice"]))
        support = next(v for k, v in reps.items() if "support-answer" in k)
        self.assertGreater(support["cache_hit_rate"], 0.8)


if __name__ == "__main__":
    unittest.main()
