"""Offline tests for ai_arb_layer.py. The LLM call and web3 are stubbed; no network, no keys."""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# The module calls load_dotenv() at import time; import from a directory without a .env.
_cwd = os.getcwd()
os.chdir(tempfile.mkdtemp())
try:
    import ai_arb_layer as gate
finally:
    os.chdir(_cwd)

OPP = {"path": "0xa->0xb->0xa", "dexes": "UNISWAP_V3", "size_usd": 5000, "gross_usd": 60,
       "gas_usd": 5, "net_usd": 55, "spread_bps": 20, "slip_bps": 10}


def reply(decision, confidence, reason="stub"):
    async def _llm(prompt):
        _llm.calls += 1
        _llm.prompt = prompt
        if isinstance(decision, str) and decision.startswith("RAW:"):
            return decision[4:]
        return json.dumps({"decision": decision, "confidence": confidence, "reason": reason})
    _llm.calls = 0
    return _llm


class GateTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = {k: getattr(gate, k) for k in ("_call_llm", "_trade_limiter", "_decision_cache", "_smart_money")}
        gate._trade_limiter = gate.TradeRateLimiter(3)
        gate._decision_cache = gate.DecisionCache(15)
        gate._smart_money = None

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(gate, k, v)


class AuditorTests(GateTestCase):
    async def test_profit_floor_rejects_without_calling_llm(self):
        gate._call_llm = reply("EXECUTE", 0.99)
        ok, _conf, why = await gate.ai_audit_opportunity(dict(OPP, net_usd=gate.AI_MIN_PROFIT_FLOOR_USD - 1))
        self.assertFalse(ok)
        self.assertTrue(why.startswith("net<"))
        self.assertEqual(gate._call_llm.calls, 0)

    async def test_llm_unavailable_fails_closed(self):
        gate._call_llm = reply("RAW:", 0)
        self.assertEqual(await gate.ai_audit_opportunity(OPP), (False, 0.0, "llm-unavailable"))

    async def test_unknown_provider_fails_closed(self):
        saved = gate.AI_PROVIDER
        gate.AI_PROVIDER = "not-a-provider"
        try:
            self.assertEqual(await gate._call_llm("x"), "")
        finally:
            gate.AI_PROVIDER = saved

    async def test_non_json_reply_is_a_skip(self):
        gate._call_llm = reply("RAW:sure, execute it", 0)
        self.assertEqual(await gate.ai_audit_opportunity(OPP), (False, 0.0, "parse-error"))

    async def test_execute_needs_minimum_confidence(self):
        gate._call_llm = reply("EXECUTE", gate.AI_AUDIT_MIN_CONFIDENCE - 0.01)
        ok, _, _ = await gate.ai_audit_opportunity(OPP)
        self.assertFalse(ok)
        self.assertEqual(gate._trade_limiter.timestamps, [])

    async def test_execute_is_recorded_and_hourly_limit_applies(self):
        gate._trade_limiter = gate.TradeRateLimiter(1)
        gate._call_llm = reply("EXECUTE", 0.9)
        ok, conf, _ = await gate.ai_audit_opportunity(OPP)
        self.assertTrue(ok)
        self.assertEqual(conf, 0.9)
        self.assertEqual(len(gate._trade_limiter.timestamps), 1)
        ok, _, why = await gate.ai_audit_opportunity(dict(OPP, path="0xc->0xd->0xc"))
        self.assertEqual((ok, why), (False, "hourly-rate-limit"))

    async def test_prompt_carries_opportunity_and_floor(self):
        gate._call_llm = reply("SKIP", 0.5)
        await gate.ai_audit_opportunity(OPP, {"block": 7})
        self.assertIn("Path: 0xa->0xb->0xa", gate._call_llm.prompt)
        self.assertIn(f"Reject net < ${gate.AI_MIN_PROFIT_FLOOR_USD:.0f}", gate._call_llm.prompt)

    async def test_finding_cache_hit_returns_a_timestamp_as_confidence(self):
        # DecisionCache.put stores (execute, time.time(), reason); the auditor returns entry[1] as confidence.
        gate._call_llm = reply("EXECUTE", 0.9)
        await gate.ai_audit_opportunity(OPP)
        ok, conf, why = await gate.ai_audit_opportunity(OPP)
        self.assertEqual(gate._call_llm.calls, 1)
        self.assertTrue(ok)
        self.assertTrue(why.startswith("cached:"))
        self.assertGreater(conf, time.time() - 60)  # a Unix timestamp, not a 0-1 confidence

    async def test_finding_cached_execute_is_not_counted_by_rate_limiter(self):
        gate._call_llm = reply("EXECUTE", 0.9)
        for _ in range(3):
            await gate.ai_audit_opportunity(OPP)
        self.assertEqual(len(gate._trade_limiter.timestamps), 1)


class ConvictionTests(GateTestCase):
    def test_weights(self):
        self.assertEqual(gate.compute_conviction(1.0, "strong", 500, 50), 1.0)
        self.assertEqual(gate.compute_conviction(0.0, "none", 0, 0), 0.0)

    def test_finding_without_wallet_signal_ai_confidence_must_reach_0_8(self):
        # With smart money "none" the best case is 0.5*conf + 0.15 + 0.10, so the 0.65 gate needs conf >= 0.8.
        self.assertLess(gate.compute_conviction(0.79, "none", 10**9, 10**9), gate.AI_AUDIT_MIN_CONFIDENCE)
        self.assertGreaterEqual(gate.compute_conviction(0.80, "none", 10**9, 10**9), gate.AI_AUDIT_MIN_CONFIDENCE)

    async def test_evaluate_opportunity_applies_conviction_gate(self):
        gate._call_llm = reply("EXECUTE", 0.7)
        ok, conviction, why = await gate.evaluate_opportunity(
            cycle=["0xa", "0xb", "0xa"], dexes="UNISWAP_V3", size_usd=5000, gross_usd=60, gas_usd=5,
            net_usd=55, spread_bps=20, slip_bps=10, block=1, gas_gwei=0.01)
        self.assertFalse(ok)
        self.assertTrue(why.startswith("low-conviction"))
        self.assertLess(conviction, gate.AI_AUDIT_MIN_CONFIDENCE)


class FakeEth:
    block_number = 1000

    def __init__(self, logs_by_wallet):
        self.logs_by_wallet = logs_by_wallet
        self.queries = []

    def get_logs(self, query):
        self.queries.append(query)
        return self.logs_by_wallet.get(query["topics"][2], [])


class SmartMoneyTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_counts_transfers_and_scores_paths(self):
        w1, w2 = "0x" + "1" * 40, "0x" + "2" * 40

        def topic(w):
            return "0x" + "0" * 24 + w[2:]

        tok = "0x" + "a" * 40
        eth = FakeEth({topic(w1): [{"address": tok}] * 2, topic(w2): [{"address": tok}] * 2})
        w3 = type("W3", (), {"eth": eth})()
        tracker = gate.SmartMoneyTracker([w1, w2], lookback=50, min_consensus=2)
        await tracker.refresh(w3, "0xddf252ad")
        self.assertEqual(eth.queries[0]["fromBlock"], 950)
        self.assertEqual(tracker.signal_for_path(f"{tok}->0xb->{tok}"), "strong")
        self.assertEqual(tracker.signal_for_path("0xb->0xc"), "none")


TARGET = """import asyncio
from decimal import Decimal

async def run():
    log.info("Bot is now running")

async def arbitrage_strategy():
    while True:
        provider = decision.provider
        size = int(decision.size_input_token)
"""


class InstallerTests(unittest.TestCase):
    def _install(self, path):
        with redirect_stdout(io.StringIO()) as out:
            ok = gate.install(str(path))
        return ok, out.getvalue()

    def test_patches_synthetic_target_once_and_backs_up(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "bot.py"
            target.write_text(TARGET, encoding="utf-8")
            ok, _ = self._install(target)
            self.assertTrue(ok)
            src = target.read_text(encoding="utf-8")
            self.assertIn("from ai_arb_layer import", src)
            self.assertIn("AI AUDIT LAYER", src)
            self.assertIn("_ai_init_smart_money", src)
            self.assertEqual(Path(d, "bot.py.bak").read_text(encoding="utf-8"), TARGET)
            ok, out = self._install(target)
            self.assertTrue(ok)
            self.assertIn("Already installed", out)
            self.assertEqual(target.read_text(encoding="utf-8"), src)

    def test_finding_rerun_duplicates_imports_when_audit_hook_missing(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "bot.py"
            target.write_text(TARGET.replace("provider = decision.provider", "provider = None"), encoding="utf-8")
            self._install(target)
            self._install(target)
            self.assertEqual(target.read_text(encoding="utf-8").count("from ai_arb_layer import"), 2)

    def test_missing_target(self):
        ok, out = self._install(Path(tempfile.mkdtemp()) / "nope.py")
        self.assertFalse(ok)
        self.assertIn("not found", out)


if __name__ == "__main__":
    unittest.main()
