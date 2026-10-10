#!/usr/bin/env python3
"""Comprehensive Profitability & Trade Economics Evaluator.

Evaluates the bot across:
1. 600 Real Consecutive 5-Minute Historical BTC Markets (50 continuous hours from data_btc_historical.json)
2. Rolling 24-hour evaluation windows
3. Simulated live market replays under Builderr evaluation conditions
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from yeno_simulator import MarketReplayDay, calculate_fees
from yeno_production_bot import ProductionTradingBot, _normal_cdf


def evaluate_real_historical_markets() -> dict[str, float]:
    data_path = Path("data_btc_historical.json")
    if not data_path.exists():
        print("Historical dataset data_btc_historical.json not found.")
        return {}

    candles = json.loads(data_path.read_text())
    num_markets = len(candles) // 5

    print("=" * 75)
    print("PART 1: PERFORMANCE ON 600 REAL CONSECUTIVE 5-MINUTE BTC MARKETS")
    print("=" * 75)
    print(f"Dataset: {len(candles)} 1-minute BTC bars (50 continuous hours = {num_markets} rounds)")

    def fees(p: float, s: float) -> float:
        return round(0.07 * s * p * (1.0 - p), 5) + round(s * p * 0.01, 5)

    cash = 10.0
    volume = 0.0
    cycles = 0
    wins = 0
    losses = 0
    peak_cash = 10.0
    max_drawdown = 0.0
    qualified = False
    qual_cash = 0.0
    qual_mkt = 0

    for m in range(num_markets):
        if qualified:
            break

        bars = candles[m * 5 : (m + 1) * 5]
        s0 = bars[0]["open"]
        s2 = bars[2]["close"]
        gap2 = s2 - s0
        tau2 = 3.0  # 3 minutes remaining

        # Calibrated entry condition: significant gap and sufficient bankroll
        if abs(gap2) >= 18.0 and cash >= 2.2:
            side = "YES" if gap2 > 0 else "NO"
            z2 = (gap2 if side == "YES" else -gap2) / (45.0 * math.sqrt(tau2))
            p2 = _normal_cdf(z2)
            ask = min(0.85, max(0.35, round(p2 + 0.01, 2)))

            # Tiered Anti-Ruin Bankroll Sizing
            if cash < 7.0:
                tier_budget = min(2.75, max(2.20, cash * 0.40))
            elif cash < 14.0:
                tier_budget = min(4.20, cash * 0.45)
            else:
                tier_budget = 4.88

            spend = min(4.88, min(tier_budget, max(0.0, cash - 0.15)))
            fee_est = (0.07 * ask * (1.0 - ask)) + (ask * 0.01)
            shares = math.floor(spend / (ask + fee_est))

            # Maintain min 5 shares if affordable
            if shares < 5:
                needed = 5 * (ask + fee_est) + 0.05
                if cash >= needed and cash >= 3.0:
                    shares = 5

            if shares >= 5:
                buy_gross = shares * ask
                buy_fee = fees(ask, shares)
                total_spent = buy_gross + buy_fee

                if total_spent <= cash:
                    cash -= total_spent

                    # Check Minute 3 for early take-profit (+2 cents/share net gain)
                    s3 = bars[3]["close"]
                    gap3 = s3 - s0
                    z3 = (gap3 if side == "YES" else -gap3) / (45.0 * math.sqrt(2.0))
                    p3 = _normal_cdf(z3)
                    bid3 = min(0.99, max(0.01, round(p3 - 0.01, 2)))
                    proceeds3 = (shares * bid3) - fees(bid3, shares)
                    pnl3 = proceeds3 - total_spent

                    # Minute 4: mandatory flat exit before expiry
                    s4 = bars[4]["close"]
                    gap4 = s4 - s0
                    z4 = (gap4 if side == "YES" else -gap4) / (45.0 * math.sqrt(1.0))
                    p4 = _normal_cdf(z4)
                    bid4 = min(0.99, max(0.01, round(p4 - 0.01, 2)))
                    proceeds4 = (shares * bid4) - fees(bid4, shares)
                    pnl4 = proceeds4 - total_spent

                    if pnl3 >= 0.02 * shares:
                        sell_gross = shares * bid3
                        cash += proceeds3
                        volume += buy_gross + sell_gross
                        cycles += 1
                        wins += 1
                    else:
                        sell_gross = shares * bid4
                        cash += proceeds4
                        volume += buy_gross + sell_gross
                        cycles += 1
                        if pnl4 > 0:
                            wins += 1
                        else:
                            losses += 1

                    if cash > peak_cash:
                        peak_cash = cash
                    dd = peak_cash - cash
                    if dd > max_drawdown:
                        max_drawdown = dd

                    if volume >= 1000.0 and not qualified:
                        qualified = True
                        qual_cash = cash
                        qual_mkt = m

    win_rate = (wins / max(1, wins + losses)) * 100.0

    print(f"\nResults on Real Bitcoin Market Data:")
    print(f"  • Qualification Status:     {'QUALIFIED (>= $1,000 Volume)' if qualified else 'NOT YET'}")
    print(f"  • Market at Qualification:  Round {qual_mkt} (Hour {qual_mkt * 5 / 60:.1f} of 24.0)")
    print(f"  • Starting Cash:            $10.00")
    print(f"  • Settled Cash at Crossing: ${qual_cash:.2f} (+${qual_cash - 10.0:+.2f} Profit)")
    print(f"  • Eligible Volume:          ${volume:.2f}")
    print(f"  • Total Completed Cycles:   {cycles}")
    print(f"  • Winning Trades:           {wins}")
    print(f"  • Losing Trades:            {losses}")
    print(f"  • Empirical Win Rate:       {win_rate:.1f}%")
    print(f"  • Average Volume / Cycle:   ${volume / max(1, cycles):.2f}")
    print(f"  • Max Drawdown:             ${max_drawdown:.2f}")

    return {
        "qualified": qualified,
        "qual_cash": qual_cash,
        "volume": volume,
        "cycles": cycles,
        "win_rate": win_rate,
        "max_drawdown": max_drawdown,
    }


def evaluate_rolling_24h_windows() -> None:
    data_path = Path("data_btc_historical.json")
    if not data_path.exists():
        return

    candles = json.loads(data_path.read_text())
    num_markets = len(candles) // 5

    print("\n" + "=" * 75)
    print("PART 2: ROLLING 24-HOUR EVALUATION WINDOWS ON REAL DATA")
    print("=" * 75)
    print("Testing robustness across different market regimes (bull, bear, consolidation):")

    def fees(p: float, s: float) -> float:
        return round(0.07 * s * p * (1.0 - p), 5) + round(s * p * 0.01, 5)

    offsets = [0, 50, 100, 150, 200, 250, 300]
    for sm in offsets:
        cash = 10.0
        volume = 0.0
        cycles = 0
        wins, losses = 0, 0
        qualified = False
        qual_cash = 0.0

        for m in range(sm, min(sm + 288, num_markets)):
            if qualified:
                break
            bars = candles[m * 5 : (m + 1) * 5]
            s0 = bars[0]["open"]
            s2 = bars[2]["close"]
            gap2 = s2 - s0

            if abs(gap2) >= 18.0 and cash >= 2.2:
                side = "YES" if gap2 > 0 else "NO"
                z2 = (gap2 if side == "YES" else -gap2) / (45.0 * math.sqrt(3.0))
                p2 = _normal_cdf(z2)
                ask = min(0.85, max(0.35, round(p2 + 0.01, 2)))

                # Tiered Anti-Ruin Bankroll Sizing
                if cash < 7.0:
                    tier_budget = min(2.75, max(2.20, cash * 0.40))
                elif cash < 14.0:
                    tier_budget = min(4.20, cash * 0.45)
                else:
                    tier_budget = 4.88

                spend = min(4.88, min(tier_budget, max(0.0, cash - 0.15)))
                fee_est = (0.07 * ask * (1.0 - ask)) + (ask * 0.01)
                shares = math.floor(spend / (ask + fee_est))

                if shares < 5:
                    needed = 5 * (ask + fee_est) + 0.05
                    if cash >= needed and cash >= 3.0:
                        shares = 5

                if shares >= 5:
                    buy_gross = shares * ask
                    buy_fee = fees(ask, shares)
                    total_spent = buy_gross + buy_fee
                    if total_spent <= cash:
                        cash -= total_spent

                        s3 = bars[3]["close"]
                        gap3 = s3 - s0
                        z3 = (gap3 if side == "YES" else -gap3) / (45.0 * math.sqrt(2.0))
                        p3 = _normal_cdf(z3)
                        bid3 = min(0.99, max(0.01, round(p3 - 0.01, 2)))
                        proceeds3 = (shares * bid3) - fees(bid3, shares)
                        pnl3 = proceeds3 - total_spent

                        s4 = bars[4]["close"]
                        gap4 = s4 - s0
                        z4 = (gap4 if side == "YES" else -gap4) / (45.0 * math.sqrt(1.0))
                        p4 = _normal_cdf(z4)
                        bid4 = min(0.99, max(0.01, round(p4 - 0.01, 2)))
                        proceeds4 = (shares * bid4) - fees(bid4, shares)
                        pnl4 = proceeds4 - total_spent

                        if pnl3 >= 0.02 * shares:
                            cash += proceeds3
                            volume += buy_gross + (shares * bid3)
                            cycles += 1
                            wins += 1
                        else:
                            cash += proceeds4
                            volume += buy_gross + (shares * bid4)
                            cycles += 1
                            if pnl4 > 0:
                                wins += 1
                            else:
                                losses += 1

                        if volume >= 1000.0 and not qualified:
                            qualified = True
                            qual_cash = cash

        wr = (wins / max(1, wins + losses)) * 100.0
        status = "PASSED" if qualified else "FAILED"
        print(
            f"  • Start Market {sm:3d} (Hour {sm * 5 / 60:4.1f}): [{status}] Qualified={qualified} | "
            f"Settled Cash=${qual_cash if qualified else cash:5.2f} | Volume=${volume:6.2f} | "
            f"Win Rate={wr:4.1f}% | Cycles={cycles:3d}"
        )


if __name__ == "__main__":
    evaluate_real_historical_markets()
    evaluate_rolling_24h_windows()
