#!/usr/bin/env python3
"""YeNo 24-Hour Paper Trading Market Simulator & Benchmarking Engine.

Simulates the exact Builderr evaluation rules:
- 24-hour evaluation window (288 consecutive 5-minute markets).
- Starting cash: $10.00.
- Evaluates bots on simulated BTC price walks and dynamic L2 prediction order books.
- Models 250ms execution latency against subsequent order book updates.
- Applies Polymarket dynamic taker fee + YeNo 1% overlay.
- Strict eligible volume accounting (only closed roundtrips count; expiry settlements add $0).
- Halts at first clean crossing of $1,000 volume while flat.
"""

from __future__ import annotations

import math
import random
import time
from typing import Any


def calculate_fees(fills: list[tuple[float, float]]) -> float:
    """Polymarket taker fee + 1% YeNo overlay."""
    gross = sum(p * s for p, s in fills)
    protocol = sum(0.07 * s * p * (1.0 - p) for p, s in fills)
    return round(protocol + 1e-12, 5) + round(gross * 0.01 + 1e-12, 5)


class MarketReplayDay:
    """Generates a realistic 24-hour sequence of 288 5-minute YeNo BTC markets."""

    def __init__(self, seed: int | None = None) -> None:
        self.rng = random.Random(seed)
        self.base_btc = 80000.0 + self.rng.uniform(-3000, 3000)

    def run(self, bot_factory: Any) -> dict[str, Any]:
        bot = bot_factory()
        cash = 10.0
        eligible_volume = 0.0
        completed_cycles = 0
        peak_cash = 10.0
        max_drawdown = 0.0
        
        position: dict[str, Any] | None = None
        qualified_at_crossing = False
        crossing_cash = 0.0
        crossing_market_idx = 0

        total_markets = 288  # 24 hours / 5 minutes

        for m_idx in range(total_markets):
            if qualified_at_crossing:
                break

            market_id = f"mkt-{m_idx:04d}"
            # Opening BTC target strike for this 5-minute round
            opening_target = self.base_btc + self.rng.gauss(0, 40)
            current_btc = opening_target

            # 5-minute market ticks down from 300s to 0s
            # Evaluator calls bot roughly every 1.5 to 3 seconds when fresh
            t = 300.0
            step_dt = 2.0

            while t > 0:
                t -= step_dt
                if t < 0:
                    t = 0

                # BTC price walk (approx 50% annualized volatility -> ~0.00069 per sqrt(min))
                sigma_per_step = self.base_btc * 0.0003 * math.sqrt(step_dt / 60.0)
                current_btc += self.rng.gauss(0, sigma_per_step)

                # Fair probability of YES based on normalized gap
                gap = current_btc - opening_target
                vol_remaining = max(10.0, self.base_btc * 0.00035 * math.sqrt(max(1.0, t) / 60.0))
                # Normal CDF approximation
                z = gap / vol_remaining
                prob_yes = max(0.02, min(0.98, 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))))
                prob_no = 1.0 - prob_yes

                # Spread varies between 0.01 and 0.04
                spread = self.rng.choice([0.01, 0.02, 0.02, 0.03])
                half = spread / 2.0

                yes_ask = min(0.99, max(0.02, round(prob_yes + half, 2)))
                yes_bid = min(yes_ask - 0.01, max(0.01, round(prob_yes - half, 2)))

                no_ask = min(0.99, max(0.02, round(prob_no + half, 2)))
                no_bid = min(no_ask - 0.01, max(0.01, round(prob_no - half, 2)))

                # Order books
                books = {
                    "YES": {
                        "bids": [[yes_bid, self.rng.randint(20, 150)]],
                        "asks": [[yes_ask, self.rng.randint(20, 150)]],
                        "minOrderSize": 5,
                    },
                    "NO": {
                        "bids": [[no_bid, self.rng.randint(20, 150)]],
                        "asks": [[no_ask, self.rng.randint(20, 150)]],
                        "minOrderSize": 5,
                    },
                }

                current_time = 1789880000.0 + (m_idx * 300) + (300 - t)
                observation = {
                    "schemaVersion": 1,
                    "timestamp": current_time,
                    "market": {
                        "id": market_id,
                        "secondsToClose": round(t, 2),
                    },
                    "account": {
                        "cashUsd": round(cash, 4),
                        "eligibleVolumeUsd": round(eligible_volume, 4),
                        "completedCycles": completed_cycles,
                        "position": position,
                    },
                    "rules": {
                        "maximumBuyCashUsd": 5.0,
                        "targetVolumeUsd": 1000.0,
                        "evaluationWindowHours": 24,
                        "onePositionAtATime": True,
                        "buyFeesIncludedInMaximum": True,
                    },
                    "reference": {
                        "btcMidUsd": round(current_btc, 3),
                        "openingTargetUsd": round(opening_target, 3),
                        "observedAt": round(current_time - 0.2, 3),
                        "targetObservedAt": round(current_time - 10.0, 3),
                        "targetProvisional": False,
                    },
                    "books": books,
                }

                # Call Bot Decision
                decision = bot.decide(observation)
                action = decision.get("action", "HOLD")

                # Execution Logic with 250ms later book / slippage
                if action == "BUY" and position is None and t > 15:
                    outcome = decision.get("outcome", "YES")
                    max_cash = float(decision.get("maxCashUsd", 5.0))
                    max_cash = min(max_cash, 5.0, cash)

                    if max_cash > 0:
                        # Slight adverse execution (250ms later quote slippage: 0 to 0.01)
                        fill_price = min(0.99, books[outcome]["asks"][0][0] + self.rng.choice([0.0, 0.0, 0.01]))
                        # Dynamic shares calculation
                        fee_est_per_share = (0.07 * fill_price * (1.0 - fill_price)) + (fill_price * 0.01)
                        cost_per_share = fill_price + fee_est_per_share
                        shares = math.floor(max_cash / cost_per_share)

                        if shares >= books[outcome]["minOrderSize"]:
                            buy_gross = round(shares * fill_price, 4)
                            buy_fees = calculate_fees([(fill_price, shares)])
                            total_spent = buy_gross + buy_fees

                            if total_spent <= cash and total_spent <= 5.0:
                                cash -= total_spent
                                position = {
                                    "market_id": market_id,
                                    "outcome": outcome,
                                    "shares": shares,
                                    "buy_gross_usd": buy_gross,
                                    "buy_fees_usd": buy_fees,
                                    "sell_gross_usd": 0.0,
                                    "sell_fees_usd": 0.0,
                                }

                elif action == "SELL" and position is not None:
                    outcome = position["outcome"]
                    shares = position["shares"]
                    # Sell into bids with possible slight slippage
                    fill_price = max(0.01, books[outcome]["bids"][0][0] - self.rng.choice([0.0, 0.0, 0.01]))
                    sell_gross = round(shares * fill_price, 4)
                    sell_fees = calculate_fees([(fill_price, shares)])
                    sell_proceeds = sell_gross - sell_fees

                    cash += sell_proceeds
                    cycle_vol = position["buy_gross_usd"] + sell_gross
                    eligible_volume += cycle_vol
                    completed_cycles += 1
                    position = None

                    # Check Qualification at this exact clean crossing!
                    if eligible_volume >= 1000.0 and not qualified_at_crossing:
                        qualified_at_crossing = True
                        crossing_cash = cash
                        crossing_market_idx = m_idx

                # Track Drawdown
                current_equity = cash + (position["shares"] * 0.5 if position else 0.0)
                if current_equity > peak_cash:
                    peak_cash = current_equity
                dd = peak_cash - current_equity
                if dd > max_drawdown:
                    max_drawdown = dd

            # End of 5m market:
            # If position was NOT sold before expiry, it settles at expiry:
            # CONTRACT RULE: Expiry settlements award ZERO eligible volume!
            if position is not None:
                final_gap = current_btc - opening_target
                won = (final_gap >= 0 and position["outcome"] == "YES") or (final_gap < 0 and position["outcome"] == "NO")
                if won:
                    cash += position["shares"] * 1.0  # Payout $1/share
                # Expired positions add $0 volume!
                position = None

        return {
            "qualified": qualified_at_crossing,
            "final_volume": eligible_volume,
            "final_cash": crossing_cash if qualified_at_crossing else cash,
            "max_drawdown": max_drawdown,
            "completed_cycles": completed_cycles,
            "market_reached": crossing_market_idx if qualified_at_crossing else total_markets,
            "finished_flat": position is None,
        }


def benchmark_bots(num_runs: int = 15) -> None:
    from yeno_production_bot import ProductionTradingBot
    from importlib import import_module
    house_module = import_module("yeno-house-bot-v1")

    print("=" * 70)
    print(f"RUNNING MONTE CARLO BENCHMARK OVER {num_runs} SIMULATED 24-HOUR PATHS")
    print("=" * 70)

    # 1. Benchmark Production Bot
    prod_results = []
    for i in range(num_runs):
        sim = MarketReplayDay(seed=42 + i * 17)
        res = sim.run(ProductionTradingBot)
        prod_results.append(res)

    prod_qualified = sum(1 for r in prod_results if r["qualified"])
    prod_volumes = [r["final_volume"] for r in prod_results]
    prod_cashes = [r["final_cash"] for r in prod_results]

    print("\n--- PRODUCTION BOT RESULTS ---")
    print(f"Qualification Rate (>= $1000 volume & flat): {prod_qualified}/{num_runs} ({prod_qualified/num_runs*100:.1f}%)")
    print(f"Median Volume: ${sorted(prod_volumes)[len(prod_volumes)//2]:.2f}")
    print(f"Max Volume:    ${max(prod_volumes):.2f}")
    print(f"Average Cash at crossing/finish: ${sum(prod_cashes)/len(prod_cashes):.2f}")
    print(f"Highest Cash:  ${max(prod_cashes):.2f}")

    # 2. Benchmark House Bot v1 for comparison
    house_results = []
    for i in range(num_runs):
        sim = MarketReplayDay(seed=42 + i * 17)
        res = sim.run(house_module.HouseBot)
        house_results.append(res)

    house_qualified = sum(1 for r in house_results if r["qualified"])
    house_volumes = [r["final_volume"] for r in house_results]
    house_cashes = [r["final_cash"] for r in house_results]

    print("\n--- HOUSE BOT v1 RESULTS ---")
    print(f"Qualification Rate (>= $1000 volume & flat): {house_qualified}/{num_runs} ({house_qualified/num_runs*100:.1f}%)")
    print(f"Median Volume: ${sorted(house_volumes)[len(house_volumes)//2]:.2f}")
    print(f"Max Volume:    ${max(house_volumes):.2f}")
    print(f"Average Cash:  ${sum(house_cashes)/len(house_cashes):.2f}")
    print("=" * 70)


if __name__ == "__main__":
    benchmark_bots(15)
