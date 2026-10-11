#!/usr/bin/env python3
"""Multi-Dataset and Cross-Regime Verification Suite for YeNo Bot.

Evaluates bot behavior across:
Part A: 4 Independent Real Historical Bitcoin Datasets from Binance (12,000 1-min bars = 200 hours)
Part B: 5 Independent 24-Hour Simulated Market Days (Seeds 42, 101, 202, 303, 777)
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from yeno_production_bot import ProductionTradingBot, _normal_cdf
from yeno_simulator import MarketReplayDay


def fees(p: float, s: float) -> float:
    return round(0.07 * s * p * (1.0 - p), 5) + round(s * p * 0.01, 5)


def evaluate_dataset(candles: list[dict[str, float]], name: str) -> dict[str, any]:
    num_markets = len(candles) // 5
    cash = 10.0
    vol = 0.0
    cycles = 0
    wins = 0
    losses = 0
    peak_cash = 10.0
    max_dd = 0.0
    qual = False
    qual_cash = 0.0
    qual_round = 0
    consecutive_losses = 0
    cooldown_until_market = 0

    for m in range(num_markets):
        if qual:
            break
        if m < cooldown_until_market:
            continue

        bars = candles[m * 5 : (m + 1) * 5]
        s0 = bars[0]["open"]
        s1 = bars[1]["close"]
        s2 = bars[2]["close"]
        gap2 = s2 - s0
        mom2 = s2 - s1
        tau2 = 3.0

        # Check extreme volatility wicks in first 2 bars:
        hl0 = abs(bars[0]["high"] - bars[0]["low"])
        hl1 = abs(bars[1]["high"] - bars[1]["low"])
        if max(hl0, hl1) >= 85.0:
            # Extreme volatility spike / flash crash wick -> SHIELD & HOLD!
            continue

        # Dynamic realized volatility from first 2 minutes
        sigma = max(20.0, min(100.0, (hl0 + hl1) * 0.9))

        if abs(gap2) >= 18.0 and cash >= 2.0:
            side = "YES" if gap2 > 0 else "NO"
            z2 = (gap2 if side == "YES" else -gap2) / (45.0 * math.sqrt(3.0))
            p2 = _normal_cdf(z2)
            ask = min(0.82, max(0.40, round(p2 + 0.01, 2)))

            # Risk-Managed Tiered Sizing with Loss Throttling
            if consecutive_losses >= 2 or cash < 7.0:
                spend = min(2.75, max(2.20, cash * 0.35))
            elif consecutive_losses == 1 or cash < 14.0:
                spend = min(3.80, cash * 0.40)
            else:
                spend = 4.88

            fee_est = (0.07 * ask * (1.0 - ask)) + (ask * 0.01)
            shares = math.floor(spend / (ask + fee_est))

            if shares < 5:
                needed = 5 * (ask + fee_est) + 0.05
                if cash >= needed and cash >= 3.0:
                    shares = 5

            if shares >= 5:
                buy_gross = shares * ask
                buy_fee = fees(ask, shares)
                tot = buy_gross + buy_fee

                if tot <= cash:
                    cash -= tot

                    # Minute 3 take profit check (Avellaneda-Stoikov fast harvest)
                    s3 = bars[3]["close"]
                    gap3 = s3 - s0
                    z3 = (gap3 if side == "YES" else -gap3) / (sigma * math.sqrt(2.0))
                    p3 = _normal_cdf(z3)
                    bid3 = min(0.99, max(0.01, round(p3 - 0.01, 2)))
                    proceeds3 = (shares * bid3) - fees(bid3, shares)
                    pnl3 = proceeds3 - tot

                    s4 = bars[4]["close"]
                    gap4 = s4 - s0

                    if pnl3 >= 0.01 * shares:
                        cash += proceeds3
                        vol += buy_gross + (shares * bid3)
                        cycles += 1
                        consecutive_losses = 0
                        wins += 1
                    else:
                        in_the_money = (gap4 > 0 if side == "YES" else gap4 < 0)
                        if in_the_money:
                            bid4 = min(0.96, max(0.85, 0.90 + min(0.06, abs(gap4) * 0.001)))
                        else:
                            bid4 = max(0.04, min(0.15, 0.10 - min(0.06, abs(gap4) * 0.001)))

                        proceeds4 = (shares * bid4) - fees(bid4, shares)
                        pnl4 = proceeds4 - tot
                        cash += proceeds4
                        vol += buy_gross + (shares * bid4)
                        cycles += 1

                        if in_the_money:
                            wins += 1
                            consecutive_losses = 0
                        else:
                            losses += 1
                            consecutive_losses += 1
                            if consecutive_losses >= 3:
                                cooldown_until_market = m + 4

                    if cash > peak_cash:
                        peak_cash = cash
                    dd = peak_cash - cash
                    if dd > max_dd:
                        max_dd = dd

                    if vol >= 1000.0 and not qual:
                        qual = True
                        qual_cash = cash
                        qual_round = m

    wr = (wins / max(1, wins + losses)) * 100.0
    return {
        "name": name,
        "qualified": qual,
        "round": qual_round if qual else num_markets,
        "hour": f"{qual_round * 5 / 60:.1f}h" if qual else "24.0h+",
        "settled_cash": qual_cash if qual else cash,
        "volume": vol,
        "win_rate": wr,
        "cycles": cycles,
        "max_dd": max_dd,
    }


def main() -> None:
    print("=" * 85)
    print("YENO MULTI-DATASET & CROSS-REGIME VERIFICATION SUITE")
    print("=" * 85)

    # -------------------------------------------------------------------------
    # PART A: 4 Real Historical Bitcoin Datasets
    # -------------------------------------------------------------------------
    print("\n--- PART A: TESTING ON 4 INDEPENDENT REAL BITCOIN DATASETS (12,000 CANDLES) ---")
    real_sets = [
        ("Set 0 (Oct 08 - Oct 10)", Path("data_btc_historical.json")),
        ("Set 1 (Oct 06 - Oct 08)", Path("data_btc_unseen_set1.json")),
        ("Set 2 (Oct 04 - Oct 06)", Path("data_btc_unseen_set2.json")),
        ("Set 3 (Sep 16 - Sep 19)", Path("data_btc_unseen_set3.json")),
    ]

    real_results = []
    for name, path in real_sets:
        if path.exists():
            candles = json.loads(path.read_text())
            res = evaluate_dataset(candles, name)
            real_results.append(res)

    print(f"{'Dataset':<26} | {'Qualified?':<10} | {'Settled Cash':<12} | {'Volume':<10} | {'Win Rate':<9} | {'Time to Finish':<14}")
    print("-" * 85)
    for r in real_results:
        q_str = "QUALIFIED" if r["qualified"] else "DRAWDOWN"
        cash_str = f"${r['settled_cash']:.2f}"
        vol_str = f"${r['volume']:.2f}"
        wr_str = f"{r['win_rate']:.1f}%"
        print(f"{r['name']:<26} | {q_str:<10} | {cash_str:<12} | {vol_str:<10} | {wr_str:<9} | {r['hour']:<14}")

    # -------------------------------------------------------------------------
    # PART B: 5 Independent 24-Hour Simulated Market Paths (2-sec ticks)
    # -------------------------------------------------------------------------
    print("\n--- PART B: TESTING ON 5 INDEPENDENT 24-HOUR SIMULATED MARKET REGIMES ---")
    sim_seeds = [
        ("Regime A (Seed 42  - Bull Trend)", 42),
        ("Regime B (Seed 101 - Choppy Range)", 101),
        ("Regime C (Seed 202 - Bear Dump)", 202),
        ("Regime D (Seed 303 - Consolidation)", 303),
        ("Regime E (Seed 777 - Random Walk)", 777),
    ]

    print(f"{'Regime':<35} | {'Flat Finish?':<12} | {'24h Volume':<11} | {'Final Cash':<11} | {'Max DD':<8}")
    print("-" * 85)
    for label, seed in sim_seeds:
        sim = MarketReplayDay(seed=seed)
        res = sim.run(ProductionTradingBot)
        flat_str = "100% FLAT" if res["finished_flat"] else "OPEN"
        vol_str = f"${res['final_volume']:.2f}"
        cash_str = f"${res['final_cash']:.2f}"
        dd_str = f"${res['max_drawdown']:.2f}"
        print(f"{label:<35} | {flat_str:<12} | {vol_str:<11} | {cash_str:<11} | {dd_str:<8}")

    print("=" * 85)


if __name__ == "__main__":
    main()
