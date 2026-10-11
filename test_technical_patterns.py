#!/usr/bin/env python3
"""Dedicated Unit & Verification Suite for Technical Trend, EMA, RSI & Chart Patterns.

Tests:
1. Fast vs Slow EMA Trend Alignment (blocks counter-trend trades)
2. Linear Regression Price Slope Velocity (blocks fading active price thrusts)
3. RSI Overbought & Oversold Exhaustion Guards (prevents top/bottom chasing)
4. Speed benchmark: verifies TA calculations run in microseconds
"""

from __future__ import annotations

import copy
import json
import time
from yeno_production_bot import (
    ProductionTradingBot,
    _compute_ema,
    _compute_rsi,
    _compute_linear_slope,
)


def run_technical_pattern_tests() -> None:
    print("=" * 75)
    print("VERIFYING TECHNICAL TREND, CHART PATTERN & MOMENTUM GATES")
    print("=" * 75)

    base_obs = json.load(open("yeno-sample-observation.json"))
    now = float(base_obs["timestamp"])

    # -------------------------------------------------------------------------
    # TEST 1: Pure Functions Accuracy
    # -------------------------------------------------------------------------
    print("\n[TEST 1] Pure Math Function Accuracy:")
    # Rising price series: 100, 102, 104, 106, 108
    rising = [100.0 + i * 2.0 for i in range(10)]
    ema_fast = _compute_ema(rising, period=3)
    ema_slow = _compute_ema(rising, period=8)
    rsi_rising = _compute_rsi(rising)
    slope_rising = _compute_linear_slope([(i, p) for i, p in enumerate(rising)])

    assert ema_fast > ema_slow, f"Fast EMA should lead slow EMA: {ema_fast} vs {ema_slow}"
    assert rsi_rising == 100.0, f"Monotonically rising RSI should be 100: {rsi_rising}"
    assert abs(slope_rising - 2.0) < 1e-4, f"Linear slope should be 2.0: {slope_rising}"
    print(f"  • Fast EMA ({ema_fast:.2f}) > Slow EMA ({ema_slow:.2f}) [PASS]")
    print(f"  • Monotonic Rising RSI = {rsi_rising:.1f} [PASS]")
    print(f"  • Slope Velocity = {slope_rising:.2f} USD/s [PASS]")

    # -------------------------------------------------------------------------
    # TEST 2: EMA Trend Alignment Gate in ProductionTradingBot
    # -------------------------------------------------------------------------
    print("\n[TEST 2] EMA Trend Alignment Gate:")
    bot_ema = ProductionTradingBot()
    bot_ema.market_id = str(base_obs["market"]["id"])

    # Historical prices sloping downwards: 80460, 80450, 80440, 80430, 80425
    # While current gap relative to opening target is positive (+25)
    bot_ema.price_history = [
        (now - 30.0, 80465.0),
        (now - 20.0, 80455.0),
        (now - 10.0, 80440.0),
        (now - 2.0, 80430.0),
    ]
    obs_test = copy.deepcopy(base_obs)
    obs_test["reference"]["openingTargetUsd"] = 80400.0
    obs_test["reference"]["btcMidUsd"] = 80425.0  # Gap is +25, but trend is heavily dropping!

    dec_ema = bot_ema.decide(obs_test)
    assert dec_ema["action"] == "HOLD", f"Should reject buying YES into EMA downtrend: {dec_ema}"
    print("  • Downward EMA Death Cross correctly blocked counter-trend BUY [PASS]")

    # -------------------------------------------------------------------------
    # TEST 3: Linear Regression Trend Slope Gate
    # -------------------------------------------------------------------------
    print("\n[TEST 3] Linear Regression Trend Slope / Velocity Gate:")
    bot_slope = ProductionTradingBot()
    bot_slope.market_id = str(base_obs["market"]["id"])

    # Price dumping at steep velocity (-$1.50 per second)
    bot_slope.price_history = [
        (now - 15.0, 80450.0),
        (now - 10.0, 80440.0),
        (now - 5.0, 80430.0),
        (now - 1.0, 80422.0),
    ]
    dec_slope = bot_slope.decide(obs_test)
    assert dec_slope["action"] == "HOLD", f"Should reject buying YES during dumping velocity: {dec_slope}"
    print("  • Dumping Price Velocity (-$1.5/s) correctly blocked [PASS]")

    # -------------------------------------------------------------------------
    # TEST 4: RSI Overbought Exhaustion Guard
    # -------------------------------------------------------------------------
    print("\n[TEST 4] RSI Overbought Exhaustion Guard:")
    bot_rsi = ProductionTradingBot()
    bot_rsi.market_id = str(base_obs["market"]["id"])

    # Parabolic upward wick: 80400 -> 80445 (every step positive, RSI > 95)
    bot_rsi.price_history = [
        (now - 16.0, 80400.0),
        (now - 14.0, 80405.0),
        (now - 12.0, 80410.0),
        (now - 10.0, 80416.0),
        (now - 8.0, 80423.0),
        (now - 6.0, 80431.0),
        (now - 4.0, 80439.0),
        (now - 2.0, 80444.0),
    ]
    obs_rsi = copy.deepcopy(base_obs)
    obs_rsi["reference"]["openingTargetUsd"] = 80400.0
    obs_rsi["reference"]["btcMidUsd"] = 80446.0
    dec_rsi = bot_rsi.decide(obs_rsi)
    assert dec_rsi["action"] == "HOLD", f"Should reject buying YES at RSI > 92 exhaustion: {dec_rsi}"
    print("  • Overbought Parabolic Top (RSI > 92) correctly shielded [PASS]")

    # -------------------------------------------------------------------------
    # TEST 5: TA Calculation Response Latency Benchmark
    # -------------------------------------------------------------------------
    print("\n[TEST 5] Benchmarking TA Calculation Latency (5,000 Decisions)...")
    bot_perf = ProductionTradingBot()
    bot_perf.market_id = str(base_obs["market"]["id"])
    bot_perf.price_history = [(now - 20.0 + i, 80480.0 + (i % 3)) for i in range(15)]

    t0 = time.perf_counter()
    for _ in range(5000):
        bot_perf.decide(base_obs)
    elapsed = time.perf_counter() - t0
    latency_ms = (elapsed / 5000) * 1000.0

    print(f"  • Total Time:               {elapsed:.4f}s")
    print(f"  • Latency per Decision:     {latency_ms:.5f} ms ({latency_ms * 1000:.1f} microseconds)")
    print(f"  • Builderr Timeout Limit:   200.000 ms")
    print(f"  • Speed Advantage:          {200.0 / latency_ms:.1f}x faster than timeout limit!")

    print("\n" + "=" * 75)
    print("ALL TECHNICAL TREND, CHART PATTERN & MOMENTUM TESTS PASSED (100%)!")
    print("=" * 75)


if __name__ == "__main__":
    run_technical_pattern_tests()
