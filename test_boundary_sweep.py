#!/usr/bin/env python3
"""Targeted Continuous Time Sweep for YeNo Bot Liquidation Safeguards.

Tests second-by-second transition from t=65s down to t=0s.
"""

import os
os.environ["YENO_STATE_FILE"] = ":memory:"

from yeno_production_bot import ProductionTradingBot

def make_obs(sec: float, bids: list, shares: float = 5.0, side: str = "YES"):
    return {
        "timestamp": 1000.0 + (65.0 - sec),
        "market": {"id": "m1", "secondsToClose": sec},
        "account": {
            "cashUsd": 7.0,
            "eligibleVolumeUsd": 100.0,
            "position": {
                "market_id": "m1",
                "outcome": side,
                "shares": shares,
                "buy_gross_usd": 2.75,
                "buy_fees_usd": 0.05,
                "sell_gross_usd": 0.0,
                "sell_fees_usd": 0.0,
            },
        },
        "rules": {"maximumBuyCashUsd": 5.0, "minOrderSize": 5.0},
        "reference": {
            "btcMidUsd": 80500.0,
            "openingTargetUsd": 80500.0,
            "observedAt": 1000.0 + (65.0 - sec) - 0.1,
            "targetObservedAt": 1000.0,
            "targetProvisional": False,
        },
        "books": {
            side: {"bids": bids, "asks": [[0.56, 10.0]], "minOrderSize": 5.0},
            "NO": {"bids": [[0.44, 10.0]], "asks": [[0.45, 10.0]], "minOrderSize": 5.0},
        },
    }

def test_sweep(name: str, bot: ProductionTradingBot, bids: list):
    print(f"\n--- Running Sweep: {name} ---")
    transitions = []
    prev_action = None
    for sec_int in range(65, -1, -1):
        sec = float(sec_int)
        obs = make_obs(sec, bids)
        dec = bot.decide(obs)
        action = dec.get("action")
        if action != prev_action:
            transitions.append((sec, action, bot.exiting))
            prev_action = action
            print(f"  t={sec:4.1f}s: Action transitioned to {action} (exiting={bot.exiting})")
    return transitions

if __name__ == "__main__":
    bids_normal = [[0.54, 10.0]]
    bids_empty = []

    bot_def_norm = ProductionTradingBot(state_file_path=":memory:", config={"forced_exit_sec": 50.0})
    test_sweep("Default Config (forced_exit_sec=50.0) with Normal Bids", bot_def_norm, bids_normal)

    bot_def_empty = ProductionTradingBot(state_file_path=":memory:", config={"forced_exit_sec": 50.0})
    test_sweep("Default Config (forced_exit_sec=50.0) with Empty Bids", bot_def_empty, bids_empty)

    bot_tuned_norm = ProductionTradingBot(state_file_path=":memory:", config={"forced_exit_sec": 49.0})
    test_sweep("Tuned Config (forced_exit_sec=49.0) with Normal Bids", bot_tuned_norm, bids_normal)

    bot_tuned_empty = ProductionTradingBot(state_file_path=":memory:", config={"forced_exit_sec": 49.0})
    test_sweep("Tuned Config (forced_exit_sec=49.0) with Empty Bids", bot_tuned_empty, bids_empty)
