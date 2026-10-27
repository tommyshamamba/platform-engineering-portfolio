#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Provenance: recovered verbatim from deepseek_python_20261002_dfca2a.py (Downloads, 2 Oct 2026),
# saved from a DeepSeek chat as ai_arb_layer.py. Only these provenance lines were added.
# Research code: it returns an EXECUTE/SKIP decision and never signs or sends a transaction.
"""
ai_arb_layer.py
===============
Complete AI Adaptation Layer for ELYON12.py — SINGLE FILE EDITION.

This file contains EVERYTHING:
  1. AI configuration (LLM provider, rate limits, thresholds)
  2. LLM auditor (asks an AI before executing every trade)
  3. Smart money tracker (follows profitable wallets)
  4. Conviction scoring
  5. Auto-installer that patches ELYON12.py in place
  6. CLI for testing standalone

USAGE:
  # Auto-install into ELYON12.py:
  python ai_arb_layer.py --install

  # Test the AI auditor standalone:
  python ai_arb_layer.py --test

  # Show the manual patch (if you prefer editing by hand):
  python ai_arb_layer.py --show-patch

REQUIREMENTS:
  pip install aiohttp python-dotenv

ENV VARS (add to your .env):
  AI_PROVIDER=deepseek
  DEEPSEEK_API_KEY=sk-...
  # or OPENAI_API_KEY / ANTHROPIC_API_KEY / OLLAMA_BASE_URL
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import aiohttp
except ImportError:
    print("ERROR: pip install aiohttp python-dotenv")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    def load_dotenv(**kwargs): pass

# =============================================================================
# SECTION 1: CONFIGURATION
# =============================================================================

AI_PROVIDER = os.getenv("AI_PROVIDER", "deepseek").strip().lower()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022").strip()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b").strip()

AI_AUDIT_MIN_CONFIDENCE = float(os.getenv("AI_AUDIT_MIN_CONFIDENCE", "0.65"))
AI_MAX_TRADES_PER_HOUR = int(os.getenv("AI_MAX_TRADES_PER_HOUR", "10"))
AI_MIN_PROFIT_FLOOR_USD = float(os.getenv("AI_MIN_PROFIT_FLOOR_USD", "30"))
AI_DECISION_CACHE_TTL = int(os.getenv("AI_DECISION_CACHE_TTL", "15"))

AI_SMART_MONEY_ENABLED = os.getenv("AI_SMART_MONEY_ENABLED", "true").lower() in ("1", "true", "yes")
SMART_MONEY_WALLETS = [a.strip() for a in os.getenv("SMART_MONEY_WALLETS", "").split(",") if a.strip()]
SMART_MONEY_LOOKBACK_BLOCKS = int(os.getenv("SMART_MONEY_LOOKBACK_BLOCKS", "50"))
SMART_MONEY_MIN_CONSENSUS = int(os.getenv("SMART_MONEY_MIN_CONSENSUS", "2"))

logger = logging.getLogger("ai-arb-layer")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

# =============================================================================
# SECTION 2: RATE LIMITER
# =============================================================================

class TradeRateLimiter:
    """Enforces 'fewer trades = profit' rule."""
    def __init__(self, max_per_hour: int):
        self.max_per_hour = max_per_hour
        self.timestamps: List[float] = []
        self._lock = asyncio.Lock()

    async def allow(self) -> bool:
        async with self._lock:
            now = time.time()
            self.timestamps = [t for t in self.timestamps if now - t < 3600]
            if len(self.timestamps) >= self.max_per_hour:
                logger.warning("AI rate limit hit: %d/hr (max=%d)", len(self.timestamps), self.max_per_hour)
                return False
            return True

    async def record(self):
        async with self._lock:
            self.timestamps.append(time.time())

_trade_limiter = TradeRateLimiter(AI_MAX_TRADES_PER_HOUR)

# =============================================================================
# SECTION 3: DECISION CACHE
# =============================================================================

class DecisionCache:
    def __init__(self, ttl: int):
        self.ttl = ttl
        self._cache: Dict[str, Tuple[bool, float, str]] = {}

    def _key(self, opp: Dict[str, Any]) -> str:
        return f"{opp.get('path','')}|{int(opp.get('spread_bps',0))}|{int(opp.get('size_usd',0))}"

    def get(self, opp: Dict[str, Any]) -> Optional[Tuple[bool, float, str]]:
        entry = self._cache.get(self._key(opp))
        if entry and (time.time() - entry[1]) < self.ttl:
            return entry
        return None

    def put(self, opp: Dict[str, Any], execute: bool, conf: float, reason: str):
        self._cache[self._key(opp)] = (execute, time.time(), reason)
        now = time.time()
        self._cache = {k: v for k, v in self._cache.items() if now - v[1] < self.ttl * 4}

_decision_cache = DecisionCache(AI_DECISION_CACHE_TTL)

# =============================================================================
# SECTION 4: LLM PROVIDERS
# =============================================================================

async def _call_openai(prompt: str) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": "You are a risk-averse crypto arbitrage auditor. Reply ONLY with valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=headers, json=payload, timeout=20) as r:
            data = await r.json()
            return data["choices"][0]["message"]["content"]

async def _call_anthropic(prompt: str) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY missing")
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {"model": ANTHROPIC_MODEL, "max_tokens": 256, "messages": [{"role": "user", "content": prompt}]}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=headers, json=payload, timeout=20) as r:
            data = await r.json()
            return data["content"][0]["text"]

async def _call_deepseek(prompt: str) -> str:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY missing")
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "You are a risk-averse crypto arbitrage auditor. Reply ONLY with valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=headers, json=payload, timeout=20) as r:
            data = await r.json()
            return data["choices"][0]["message"]["content"]

async def _call_ollama(prompt: str) -> str:
    url = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "format": "json"}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload, timeout=60) as r:
            data = await r.json()
            return data.get("response", "{}")

async def _call_llm(prompt: str) -> str:
    try:
        if AI_PROVIDER == "openai":
            return await _call_openai(prompt)
        if AI_PROVIDER == "anthropic":
            return await _call_anthropic(prompt)
        if AI_PROVIDER == "deepseek":
            return await _call_deepseek(prompt)
        if AI_PROVIDER == "ollama":
            return await _call_ollama(prompt)
        raise ValueError(f"Unknown AI_PROVIDER: {AI_PROVIDER}")
    except Exception as e:
        logger.warning("LLM call failed (%s): %s", AI_PROVIDER, e)
        return ""

# =============================================================================
# SECTION 5: PROMPT + AUDITOR
# =============================================================================

def _build_audit_prompt(opp: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return f"""Audit this Arbitrum arbitrage opportunity before execution.

OPPORTUNITY:
- Path: {opp.get('path','N/A')}
- DEX: {opp.get('dexes','N/A')}
- Spread: {opp.get('spread_bps',0):.1f} bps
- Size: ${opp.get('size_usd',0):.2f}
- Gross: ${opp.get('gross_usd',0):.2f}
- Gas: ${opp.get('gas_usd',0):.2f}
- Net: ${opp.get('net_usd',0):.2f}
- Slippage: {opp.get('slip_bps',0):.1f} bps

CONTEXT:
- Block: {ctx.get('block',0)}
- Gas (gwei): {ctx.get('gas_gwei',0):.4f}
- Win rate (recent): {ctx.get('win_rate',0):.1%}
- Vol 24h: {ctx.get('vol_24h',0):.2f}%
- Smart money: {ctx.get('smart_money','none')}

RULES:
1. Reject net < ${AI_MIN_PROFIT_FLOOR_USD:.0f}
2. Reject stale/mispriced spreads
3. Reject if gas is spiking and net marginal
4. Be skeptical — most opportunities are traps

Reply ONLY with valid JSON:
{{"decision": "EXECUTE" or "SKIP", "confidence": 0.0-1.0, "reason": "one short sentence"}}"""

async def ai_audit_opportunity(opp: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Tuple[bool, float, str]:
    ctx = ctx or {}

    if opp.get("net_usd", 0) < AI_MIN_PROFIT_FLOOR_USD:
        return False, 1.0, f"net<${AI_MIN_PROFIT_FLOOR_USD:.0f}"

    if not await _trade_limiter.allow():
        return False, 1.0, "hourly-rate-limit"

    cached = _decision_cache.get(opp)
    if cached:
        return cached[0], cached[1], "cached:" + cached[2]

    raw = await _call_llm(_build_audit_prompt(opp, ctx))
    if not raw:
        return False, 0.0, "llm-unavailable"

    try:
        parsed = json.loads(raw)
        decision = str(parsed.get("decision", "SKIP")).upper()
        conf = float(parsed.get("confidence", 0.0))
        reason = str(parsed.get("reason", ""))[:120]
    except Exception as e:
        logger.warning("LLM JSON parse failed: %s | raw=%s", e, raw[:200])
        return False, 0.0, "parse-error"

    execute = (decision == "EXECUTE") and (conf >= AI_AUDIT_MIN_CONFIDENCE)
    _decision_cache.put(opp, execute, conf, reason)
    if execute:
        await _trade_limiter.record()

    logger.info("[AI-AUDIT] %s conf=%.2f reason='%s' path=%s net=$%.2f",
                "EXECUTE" if execute else "SKIP", conf, reason,
                opp.get("path", "?"), opp.get("net_usd", 0.0))
    return execute, conf, reason

# =============================================================================
# SECTION 6: SMART MONEY TRACKER
# =============================================================================

class SmartMoneyTracker:
    def __init__(self, wallets: List[str], lookback: int, min_consensus: int):
        self.wallets = [w.lower() for w in wallets]
        self.lookback = lookback
        self.min_consensus = min_consensus
        self._signal: Dict[str, Tuple[int, float]] = {}

    async def refresh(self, w3, transfer_topic: str) -> None:
        if not self.wallets:
            return
        try:
            latest = w3.eth.block_number
            from_block = max(0, latest - self.lookback)
            counts: Dict[str, int] = {}
            for wallet in self.wallets:
                topic_to = "0x" + "0" * 24 + wallet[2:]
                try:
                    logs = w3.eth.get_logs({
                        "fromBlock": from_block, "toBlock": latest,
                        "topics": [transfer_topic, None, topic_to],
                    })
                    for log in logs:
                        tok = log["address"].lower()
                        counts[tok] = counts.get(tok, 0) + 1
                except Exception:
                    continue
            now = time.time()
            self._signal = {t: (c, now) for t, c in counts.items() if c >= self.min_consensus}
            if self._signal:
                logger.info("[SMART-MONEY] signals=%s", list(self._signal.keys())[:5])
        except Exception as e:
            logger.debug("smart money refresh failed: %s", e)

    def signal_for_path(self, path: str) -> str:
        if not self._signal:
            return "none"
        tokens = [t.lower() for t in path.split("->")] if path else []
        best = 0
        for tok, (cnt, _) in self._signal.items():
            if tok in tokens:
                best = max(best, cnt)
        if best >= self.min_consensus * 2:
            return "strong"
        if best >= self.min_consensus:
            return "weak"
        return "none"

_smart_money: Optional[SmartMoneyTracker] = None

def init_smart_money(wallets: List[str], lookback: int, consensus: int) -> SmartMoneyTracker:
    global _smart_money
    _smart_money = SmartMoneyTracker(wallets, lookback, consensus)
    return _smart_money

async def get_smart_money_signal(path: str) -> str:
    return _smart_money.signal_for_path(path) if _smart_money else "none"

# =============================================================================
# SECTION 7: CONVICTION SCORE
# =============================================================================

def compute_conviction(ai_conf: float, sm: str, net_usd: float, spread_bps: float) -> float:
    sm_map = {"none": 0.0, "weak": 0.5, "strong": 1.0}
    smv = sm_map.get(sm, 0.0)
    econ = min(1.0, math.log1p(max(0.0, net_usd)) / math.log1p(500))
    spread = min(1.0, max(0.0, spread_bps) / 50.0)
    return round(0.50 * ai_conf + 0.25 * smv + 0.15 * econ + 0.10 * spread, 4)

# =============================================================================
# SECTION 8: MAIN ENTRY POINT (called from ELYON12.py)
# =============================================================================

async def evaluate_opportunity(
    cycle: List[str],
    dexes: str,
    size_usd: float,
    gross_usd: float,
    gas_usd: float,
    net_usd: float,
    spread_bps: float,
    slip_bps: float,
    block: int,
    gas_gwei: float,
    win_rate: float = 0.0,
    vol_24h: float = 0.0,
) -> Tuple[bool, float, str]:
    path_str = "->".join(cycle)
    sm = await get_smart_money_signal(path_str)

    opp = {
        "path": path_str, "dexes": dexes, "size_usd": size_usd,
        "gross_usd": gross_usd, "gas_usd": gas_usd, "net_usd": net_usd,
        "spread_bps": spread_bps, "slip_bps": slip_bps,
    }
    ctx = {
        "block": block, "gas_gwei": gas_gwei, "win_rate": win_rate,
        "vol_24h": vol_24h, "smart_money": sm,
    }
    execute, conf, reason = await ai_audit_opportunity(opp, ctx)
    conviction = compute_conviction(conf, sm, net_usd, spread_bps)
    if conviction < AI_AUDIT_MIN_CONFIDENCE:
        return False, conviction, f"low-conviction({conviction})"
    return execute, conviction, reason

# =============================================================================
# SECTION 9: AUTO-INSTALLER (patches ELYON12.py)
# =============================================================================

PATCH_IMPORT = """
# --- AI layer imports (auto-installed by ai_arb_layer.py) ---
from ai_arb_layer import (
    evaluate_opportunity as _ai_evaluate_opportunity,
    init_smart_money as _ai_init_smart_money,
    _smart_money as _ai_smart_money,
    AI_SMART_MONEY_ENABLED as _AI_SM_ENABLED,
    SMART_MONEY_WALLETS as _AI_SM_WALLETS,
    SMART_MONEY_LOOKBACK_BLOCKS as _AI_SM_LOOKBACK,
    SMART_MONEY_MIN_CONSENSUS as _AI_SM_CONSENSUS,
)
# --- end AI layer imports ---
"""

PATCH_SMART_MONEY_INIT = """
    # --- AI: initialize smart money tracker (auto-installed) ---
    if _AI_SM_ENABLED and _AI_SM_WALLETS:
        _ai_init_smart_money(_AI_SM_WALLETS, _AI_SM_LOOKBACK, _AI_SM_CONSENSUS)
        log.info("Smart money tracker initialized with %d wallets", len(_AI_SM_WALLETS))

        async def _ai_sm_refresh_loop():
            while not STOP_EVENT.is_set():
                try:
                    if _ai_smart_money:
                        await _ai_smart_money.refresh(w3, TRANSFER_TOPIC)
                except Exception as e:
                    log.debug("smart money refresh: %s", e)
                await asyncio.sleep(30)

        create_task_tracked(_ai_sm_refresh_loop(), name="smart-money-refresh")
    # --- end AI smart money init ---
"""

PATCH_AUDIT_BLOCK = """        # ==========================================================
        # AI AUDIT LAYER (auto-installed by ai_arb_layer.py)
        # ==========================================================
        try:
            _ai_in_amt = int(decision.size_input_token)
            _ai_out_amt = int(decision.min_out)
            _ai_spread_bps = 0.0
            if _ai_in_amt > 0 and _ai_out_amt > 0:
                _ai_spread_bps = float((Decimal(_ai_out_amt - _ai_in_amt) / Decimal(_ai_in_amt)) * Decimal(10000))
            _ai_gas_gwei = float(predict_gas_price_wei(fast=True) or 0) / 1e9
            _ai_blk = int(METRICS.get("last_block") or 0)
            _ai_size_usd = float(usd_notional(exec_path[0], _ai_in_amt))
            _ai_gas_usd = float(estimate_gas_cost(TX_GAS_LIMIT, fast=True))
            _ai_tot = METRICS.get("trades_total", 0)
            _ai_win = METRICS.get("trades_success", 0)
            _ai_win_rate = (_ai_win / _ai_tot) if _ai_tot > 0 else 0.0
            _ai_should_exec, _ai_conf, _ai_why = await _ai_evaluate_opportunity(
                cycle=exec_path,
                dexes=str(decision.provider),
                size_usd=_ai_size_usd,
                gross_usd=float(gross_usd),
                gas_usd=_ai_gas_usd,
                net_usd=float(net_usd),
                spread_bps=_ai_spread_bps,
                slip_bps=float(dyn_slip) * 10000.0,
                block=_ai_blk,
                gas_gwei=_ai_gas_gwei,
                win_rate=_ai_win_rate,
                vol_24h=0.0,
            )
            if not _ai_should_exec:
                logger.info("[AI-REJECT] path=%s net=$%.2f conf=%.2f reason=%s",
                            "->".join(exec_path), float(net_usd), _ai_conf, _ai_why)
                await bandit_update(bandit_key(exec_path), success=False)
                continue
        except Exception as _ai_err:
            logger.warning("AI audit error, defaulting to SKIP: %s", _ai_err)
            await bandit_update(bandit_key(exec_path), success=False)
            continue
        # ==========================================================
        # END AI AUDIT LAYER
        # ==========================================================
"""

def _find_insertion_point_for_import(src: str) -> int:
    """Insert imports after the last top-level import block."""
    lines = src.split("\n")
    last_import_idx = 0
    for i, line in enumerate(lines[:200]):
        s = line.strip()
        if s.startswith("import ") or s.startswith("from "):
            last_import_idx = i
    return last_import_idx

def _find_strategy_audit_hook(src: str) -> Optional[int]:
    """
    Find where to insert the audit block inside arbitrage_strategy().
    Look for: 'provider = decision.provider' followed by 'size = int('
    """
    lines = src.split("\n")
    for i, line in enumerate(lines):
        if "provider = decision.provider" in line:
            for j in range(i, min(i + 15, len(lines))):
                if "size = int(decision.size_input_token)" in lines[j]:
                    return j + 1
    return None

def _find_run_body_hook(src: str) -> Optional[int]:
    """
    Find a good place inside run() to insert smart money init.
    Look for 'log.info("Bot is now running' or similar banner line.
    """
    lines = src.split("\n")
    for i, line in enumerate(lines):
        if "Bot is now running" in line or "Press Ctrl+C to stop" in line:
            return i + 1
    return None

def install(target: str = "ELYON12.py", backup: bool = True) -> bool:
    target_path = Path(target)
    if not target_path.exists():
        print(f"ERROR: {target} not found in {Path.cwd()}")
        return False

    src = target_path.read_text(encoding="utf-8")

    if "ai_arb_layer" in src and "AI AUDIT LAYER" in src:
        print("Already installed. Skipping.")
        return True

    if backup:
        bak = target_path.with_suffix(".py.bak")
        shutil.copy2(target_path, bak)
        print(f"Backup created: {bak}")

    # 1. Insert import
    insert_at = _find_insertion_point_for_import(src)
    lines = src.split("\n")
    lines.insert(insert_at + 1, PATCH_IMPORT)
    src = "\n".join(lines)
    print(f"✓ Inserted AI imports at line {insert_at + 1}")

    # 2. Insert smart money init inside run()
    run_hook = _find_run_body_hook(src)
    if run_hook is None:
        print("WARN: Could not find run() banner line — skipping smart money init.")
        print("      You'll need to add PATCH_SMART_MONEY_INIT manually.")
    else:
        lines = src.split("\n")
        lines.insert(run_hook, PATCH_SMART_MONEY_INIT)
        src = "\n".join(lines)
        print(f"✓ Inserted smart money init at line {run_hook}")

    # 3. Insert audit block inside arbitrage_strategy()
    audit_hook = _find_strategy_audit_hook(src)
    if audit_hook is None:
        print("WARN: Could not find arbitrage_strategy() hook point.")
        print("      You'll need to add PATCH_AUDIT_BLOCK manually (see --show-patch).")
    else:
        lines = src.split("\n")
        lines.insert(audit_hook, PATCH_AUDIT_BLOCK)
        src = "\n".join(lines)
        print(f"✓ Inserted AI audit block at line {audit_hook}")

    target_path.write_text(src, encoding="utf-8")
    print(f"✓ Patched {target_path}")
    print("\nNext steps:")
    print("  1. Add to .env:  AI_PROVIDER=deepseek")
    print("  2. Add to .env:  DEEPSEEK_API_KEY=sk-...")
    print("  3. Run:  python ELYON12.py")
    print("  4. Watch for [AI-AUDIT] / [AI-REJECT] log lines")
    return True

def show_patch():
    print("=" * 70)
    print("MANUAL PATCH INSTRUCTIONS")
    print("=" * 70)
    print("\n[1] Add these imports near the top of ELYON12.py:\n")
    print(PATCH_IMPORT)
    print("\n[2] Inside run(), after the 'Bot is now running' line, add:\n")
    print(PATCH_SMART_MONEY_INIT)
    print("\n[3] Inside arbitrage_strategy(), after 'size = int(decision.size_input_token)', add:\n")
    print(PATCH_AUDIT_BLOCK)
    print("=" * 70)

# =============================================================================
# SECTION 10: CLI TEST
# =============================================================================

async def _test_auditor():
    print("Testing AI auditor with a synthetic opportunity...\n")
    ok, conf, why = await evaluate_opportunity(
        cycle=["0xWETH", "0xUSDC", "0xWETH"],
        dexes="UNISWAP_V3",
        size_usd=5000,
        gross_usd=45,
        gas_usd=8,
        net_usd=37,
        spread_bps=90,
        slip_bps=30,
        block=0,
        gas_gwei=0.01,
        win_rate=0.35,
        vol_24h=1.2,
    )
    print(f"\nRESULT: execute={ok} confidence={conf:.3f} reason='{why}'")
    if not os.getenv("DEEPSEEK_API_KEY") and not os.getenv("OPENAI_API_KEY") \
       and not os.getenv("ANTHROPIC_API_KEY") and AI_PROVIDER != "ollama":
        print("\n⚠️  No API key found — configure one in .env")
        print("   Cheapest: DEEPSEEK_API_KEY (https://platform.deepseek.com/)")
        print("   Free:    OLLAMA_BASE_URL=http://localhost:11434")

# =============================================================================
# SECTION 11: MAIN
# =============================================================================

def main():
    ap = argparse.ArgumentParser(description="AI Arbitrage Layer for ELYON12.py")
    ap.add_argument("--install", action="store_true", help="Auto-patch ELYON12.py in place")
    ap.add_argument("--target", default="ELYON12.py", help="Target file to patch (default: ELYON12.py)")
    ap.add_argument("--no-backup", action="store_true", help="Skip .bak backup")
    ap.add_argument("--test", action="store_true", help="Test the AI auditor standalone")
    ap.add_argument("--show-patch", action="store_true", help="Show manual patch instructions")
    args = ap.parse_args()

    if args.install:
        ok = install(args.target, backup=not args.no_backup)
        sys.exit(0 if ok else 1)
    if args.test:
        asyncio.run(_test_auditor())
        return
    if args.show_patch:
        show_patch()
        return

    # No args — show help
    ap.print_help()
    print("\nTypical usage:")
    print("  python ai_arb_layer.py --install     # Auto-patch ELYON12.py")
    print("  python ai_arb_layer.py --test        # Test the auditor")
    print("  python ai_arb_layer.py --show-patch  # Manual instructions")

if __name__ == "__main__":
    main()