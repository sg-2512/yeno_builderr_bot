#!/usr/bin/env python3
"""Comprehensive Local Test Suite for YeNo Production Bot.

Tests all contract edge cases and measures response latency locally without any external dependencies.
"""

from __future__ import annotations

import copy
import json
import time
from yeno_production_bot import ProductionTradingBot, DecideRequestHandler


def run_tests() -> None:
    print("=" * 65)
    print("RUNNING LOCAL PRODUCTION VERIFICATION SUITE")
    print("=" * 65)

    base_obs = json.load(open("yeno-sample-observation.json"))
    base_obs["books"]["YES"] = {"asks": [[0.80, 100]], "bids": [[0.79, 100]], "minOrderSize": 5}
    base_obs["books"]["NO"] = {"asks": [[0.21, 100]], "bids": [[0.20, 100]], "minOrderSize": 5}

    # Test 1: Fresh start -> BUY on positive trend
    bot = ProductionTradingBot()
    d1 = bot.decide(base_obs)
    print(f"[TEST 1] Initial Observation: {d1}")
    assert d1["action"] == "BUY", f"Expected BUY, got {d1}"
    assert d1["outcome"] == "YES"
    assert 0 < d1["maxCashUsd"] <= 5.0, f"Max cash {d1['maxCashUsd']} exceeds $5"
    print("  -> PASSED: Valid BUY within $5 ceiling")

    # Test 2: Negative Gap -> BUY NO
    bot2 = ProductionTradingBot()
    obs_no = copy.deepcopy(base_obs)
    obs_no["reference"]["btcMidUsd"] = 80350.0  # Gap is -74.9 USD
    obs_no["books"]["NO"] = {"asks": [[0.80, 100]], "bids": [[0.79, 100]], "minOrderSize": 5}
    obs_no["books"]["YES"] = {"asks": [[0.21, 100]], "bids": [[0.20, 100]], "minOrderSize": 5}
    d2 = bot2.decide(obs_no)
    print(f"\n[TEST 2] Negative Gap (BTC dropped): {d2}")
    assert d2["action"] == "BUY", f"Expected BUY, got {d2}"
    assert d2["outcome"] == "NO"
    print("  -> PASSED: Correctly switched to outcome NO")

    # Test 3: Wide spread rejection (Spread > 0.03)
    bot3 = ProductionTradingBot()
    obs_wide = copy.deepcopy(base_obs)
    obs_wide["books"]["YES"]["asks"] = [[0.55, 100]]
    obs_wide["books"]["YES"]["bids"] = [[0.48, 100]]  # Spread is 0.07
    d3 = bot3.decide(obs_wide)
    print(f"\n[TEST 3] Wide Spread Filter: {d3}")
    assert d3["action"] == "HOLD", f"Expected HOLD on wide spread, got {d3}"
    print("  -> PASSED: Wide spread rejected to prevent instant fee/slippage loss")

    # Test 4: Final 15 seconds freeze
    bot4 = ProductionTradingBot()
    obs_freeze = copy.deepcopy(base_obs)
    obs_freeze["market"]["secondsToClose"] = 12.0
    d4 = bot4.decide(obs_freeze)
    print(f"\n[TEST 4] Final 15s Freeze Check: {d4}")
    assert d4["action"] == "HOLD", f"Expected HOLD inside final 15s, got {d4}"
    print("  -> PASSED: Blocked entry inside freeze window")

    # Test 5: Open position -> Take Profit
    bot5 = ProductionTradingBot()
    obs_tp = copy.deepcopy(base_obs)
    obs_tp["account"]["position"] = {
        "market_id": "sample-btc-5m-market",
        "outcome": "YES",
        "shares": 9.0,
        "buy_gross_usd": 4.50,
        "buy_fees_usd": 0.20,
        "sell_gross_usd": 0.0,
        "sell_fees_usd": 0.0,
    }
    # Market bid moved up to 0.58
    obs_tp["books"]["YES"]["bids"] = [[0.58, 100]]
    d5 = bot5.decide(obs_tp)
    print(f"\n[TEST 5] Take Profit Trigger: {d5}")
    assert d5["action"] == "SELL", f"Expected SELL on profit, got {d5}"
    print("  -> PASSED: Closed position to lock in volume and gain")

    # Test 6: Open position -> Forced Time Exit (t <= 42s)
    bot6 = ProductionTradingBot()
    obs_forced = copy.deepcopy(base_obs)
    obs_forced["account"]["position"] = obs_tp["account"]["position"]
    obs_forced["market"]["secondsToClose"] = 38.0
    d6 = bot6.decide(obs_forced)
    print(f"\n[TEST 6] Forced Time Exit: {d6}")
    assert d6["action"] == "SELL", f"Expected SELL on time limit, got {d6}"
    print("  -> PASSED: Closed position before market closes")

    # Test 7: Target Volume Reached -> Clean Crossing Lock
    bot7 = ProductionTradingBot()
    obs_goal = copy.deepcopy(base_obs)
    obs_goal["account"]["eligibleVolumeUsd"] = 1005.00
    obs_goal["account"]["position"] = None
    d7 = bot7.decide(obs_goal)
    print(f"\n[TEST 7] Target Volume Lock (>= $1000): {d7}")
    assert d7["action"] == "HOLD", f"Expected HOLD when qualified, got {d7}"
    d7_subsequent = bot7.decide(base_obs)
    assert d7_subsequent["action"] == "HOLD", "Must remain locked in HOLD forever"
    print("  -> PASSED: Permanently locked in flat state at crossing")

    # Test 8: Response Latency (Speed Benchmark)
    print("\n[TEST 8] Benchmarking /decide Response Latency...")
    iterations = 5000
    start = time.perf_counter()
    for _ in range(iterations):
        bot.decide(base_obs)
    elapsed = time.perf_counter() - start
    avg_ms = (elapsed / iterations) * 1000.0
    print(f"  -> Total time for {iterations} decisions: {elapsed:.3f}s")
    print(f"  -> Average Latency per Decision: {avg_ms:.4f} ms")
    assert avg_ms < 5.0, f"Latency {avg_ms} ms is too slow"
    print(f"  -> PASSED: Response latency is {avg_ms:.4f} ms (Well within 200 ms requirement!)")

    # Test 9: Regressions Check — Cash >= $20 & elapsed_hours >= 16.5 (No UnboundLocalError)
    bot9 = ProductionTradingBot()
    obs_rich = copy.deepcopy(base_obs)
    obs_rich["account"]["cashUsd"] = 28.50
    obs_rich["timestamp"] = 1700000000.0 + (18.0 * 3600.0)
    obs_rich["market"]["secondsToClose"] = 180.0
    bot9.session_start_time = 1700000000.0
    bot9.price_history = [
        (1700000000.0 + (18.0 * 3600.0) - 30.0, 80400.0),
        (1700000000.0 + (18.0 * 3600.0) - 20.0, 80410.0),
        (1700000000.0 + (18.0 * 3600.0) - 10.0, 80420.0),
        (1700000000.0 + (18.0 * 3600.0) - 0.0, 80430.0),
    ]
    d9 = bot9.decide(obs_rich)
    print(f"\n[TEST 9] High Bankroll & Late Session Check (No UnboundLocalError): {d9}")
    assert d9["action"] in ("BUY", "HOLD"), f"Expected BUY or HOLD, got {d9}"
    print("  -> PASSED: Executed Horizon 3 smoothly without UnboundLocalError crash!")

    # Test 10: Continuous Price History Recording During Position
    bot10 = ProductionTradingBot()
    obs_pos = copy.deepcopy(base_obs)
    obs_pos["account"]["position"] = obs_tp["account"]["position"]
    for i in range(10):
        obs_pos["timestamp"] = base_obs["timestamp"] + i
        obs_pos["reference"]["btcMidUsd"] = 80400.0 + i
        bot10.decide(obs_pos)
    print(f"\n[TEST 10] Price History Length During Position: {len(bot10.price_history)}")
    assert len(bot10.price_history) >= 10, f"Expected continuous history >= 10, got {len(bot10.price_history)}"
    print("  -> PASSED: Continuous price history recorded even while holding positions!")

    # Test 11: Expanded Entry Price Window (0.50 to 0.92)
    bot11 = ProductionTradingBot()
    obs_early_breakout = copy.deepcopy(base_obs)
    obs_early_breakout["books"]["YES"]["asks"] = [[0.55, 100]]
    obs_early_breakout["books"]["YES"]["bids"] = [[0.53, 100]]
    obs_early_breakout["reference"]["btcMidUsd"] = 80440.0
    d11 = bot11.decide(obs_early_breakout)
    print(f"\n[TEST 11] Early Breakout Entry (Ask = 0.55): {d11}")
    assert d11["action"] == "BUY", f"Expected BUY at 0.55 early breakout, got {d11}"
    print("  -> PASSED: Successfully entered early breakout trade in 0.50-0.92 window!")

    # Test 12: Model Edge Overpriced Book Rejection
    bot12 = ProductionTradingBot()
    obs_overpriced = copy.deepcopy(base_obs)
    obs_overpriced["reference"]["btcMidUsd"] = float(obs_overpriced["reference"]["openingTargetUsd"])
    obs_overpriced["books"]["YES"]["asks"] = [[0.80, 100]]
    obs_overpriced["books"]["YES"]["bids"] = [[0.78, 100]]
    d12 = bot12.decide(obs_overpriced)
    print(f"\n[TEST 12] Overpriced Order Book Rejection: {d12}")
    assert d12["action"] == "HOLD", f"Expected HOLD on heavily overpriced ask, got {d12}"
    print("  -> PASSED: Successfully rejected heavily marked-up order book!")

    # Test 13: Spike Shield Flash Wick Rejection
    bot13 = ProductionTradingBot()
    obs_spike = copy.deepcopy(base_obs)
    now_ts = float(obs_spike["timestamp"])
    bot13.price_history = [
        (now_ts - 20.0, 80400.0),
        (now_ts - 10.0, 80400.0),
        (now_ts - 0.0, 80700.0),
    ]
    d13 = bot13.decide(obs_spike)
    print(f"\n[TEST 13] Flash Spike Shield Trigger: {d13}")
    assert d13["action"] == "HOLD", f"Expected HOLD on flash wick spike, got {d13}"
    print("  -> PASSED: Successfully shielded against extreme flash crash / spike wick!")

    print("\n" + "=" * 65)
    print("ALL LOCAL TESTS PASSED WITH 100% COMPLIANCE!")
    print("=" * 65)


if __name__ == "__main__":
    run_tests()
