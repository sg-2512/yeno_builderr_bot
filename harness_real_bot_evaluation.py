#!/usr/bin/env python3
"""YeNo Real Bot Backtesting Harness — 100% Causal, Zero Future Data.

Directly evaluates the actual `ProductionTradingBot.decide()` implementation
on real historical BTC 1-minute candlestick data.
- NO hardcoded strategy logic in the runner: all decisions come from `bot.decide(obs)`.
- NO future data: order books and reference data at minute t use only information up to minute t.
- Exact fee layers: Polymarket dynamic taker fee + 1% YeNo overlay on both sides.
- Strict volume rule: volume is only credited when a cycle is fully closed via executed SELL.
- Settlements at expiry add $0 volume.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from yeno_production_bot import ProductionTradingBot, _compute_fees, _parse_book_levels


def calculate_fees(fills: list[tuple[float, float]]) -> float:
    """Exact Polymarket dynamic taker fee + 1% YeNo overlay."""
    gross = sum(p * s for p, s in fills)
    protocol = sum(0.07 * s * p * (1.0 - p) for p, s in fills)
    return round(protocol + 1e-12, 5) + round(gross * 0.01 + 1e-12, 5)


def run_real_day_evaluation(candles_1440: list[dict[str, float]], bot_cls: type[ProductionTradingBot] = ProductionTradingBot) -> dict[str, Any]:
    assert len(candles_1440) == 1440, f"Expected 1440 bars, got {len(candles_1440)}"
    
    bot = bot_cls(state_file_path=":memory:")
    cash = 10.0
    eligible_volume = 0.0
    completed_cycles = 0
    peak_cash = 10.0
    max_drawdown = 0.0
    position: dict[str, Any] | None = None
    qualified = False
    crossing_cash = 0.0
    crossing_market = 0

    rolling_history: list[float] = []

    # 288 consecutive 5-minute markets in 24 hours
    for m_idx in range(288):
        if qualified:
            break

        market_id = f"mkt-{m_idx:04d}"
        market_candles = candles_1440[m_idx * 5 : (m_idx + 1) * 5]
        s0 = market_candles[0]["open"]  # Opening strike target

        # Simulate ticks at minute 1, 2, 3, 4 of the 5-minute market
        for bar_idx in range(1, 5):
            bar = market_candles[bar_idx]
            s_t = bar["close"]
            seconds_to_close = (5 - bar_idx) * 60.0  # 240s, 180s, 120s, 60s
            now = bar["timestamp"]

            rolling_history.append(s_t)
            if len(rolling_history) > 60:
                rolling_history = rolling_history[-60:]

            # Causal volatility estimation from observed history up to now
            recent = rolling_history[-15:] if len(rolling_history) >= 15 else rolling_history
            local_range = max(recent) - min(recent) if len(recent) >= 4 else 45.0
            p_scale = max(0.035, min(1.5, s0 / 80000.0))
            sigma_1m = max(25.0 * p_scale, min(150.0 * p_scale, local_range * 1.1))

            # Fair probability under normal distribution of remaining walk
            gap = s_t - s0
            tau_min = max(0.5, seconds_to_close / 60.0)
            vol_rem = max(12.0 * p_scale, sigma_1m * math.sqrt(tau_min))
            z = gap / vol_rem
            prob_yes = max(0.02, min(0.98, 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))))
            prob_no = 1.0 - prob_yes

            spread = 0.02
            half = spread / 2.0
            yes_ask = min(0.99, max(0.02, round(prob_yes + half, 2)))
            yes_bid = min(yes_ask - 0.01, max(0.01, round(prob_yes - half, 2)))
            no_ask = min(0.99, max(0.02, round(prob_no + half, 2)))
            no_bid = min(no_ask - 0.01, max(0.01, round(prob_no - half, 2)))

            books = {
                "YES": {
                    "bids": [[yes_bid, 100]],
                    "asks": [[yes_ask, 100]],
                    "minOrderSize": 5,
                    "observedAt": now,
                },
                "NO": {
                    "bids": [[no_bid, 100]],
                    "asks": [[no_ask, 100]],
                    "minOrderSize": 5,
                    "observedAt": now,
                },
            }

            observation = {
                "schemaVersion": 1,
                "timestamp": now,
                "market": {
                    "id": market_id,
                    "secondsToClose": seconds_to_close,
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
                    "btcMidUsd": round(s_t, 3),
                    "openingTargetUsd": round(s0, 3),
                    "observedAt": round(now - 0.2, 3),
                    "targetObservedAt": round(now - (bar_idx * 60), 3),
                    "targetProvisional": False,
                },
                "books": books,
            }

            # Call real bot decide method
            decision = bot.decide(observation)
            action = decision.get("action", "HOLD")

            if action == "BUY" and position is None and seconds_to_close > 15.0:
                outcome = decision.get("outcome", "YES")
                max_cash = float(decision.get("maxCashUsd", 5.0))
                max_cash = min(max_cash, 5.0, cash)

                if max_cash > 0:
                    fill_price = books[outcome]["asks"][0][0]
                    fee_est_per_share = (0.07 * fill_price * (1.0 - fill_price)) + (fill_price * 0.01)
                    cost_per_share = fill_price + fee_est_per_share
                    shares = math.floor(max_cash / cost_per_share)

                    if shares >= books[outcome]["minOrderSize"]:
                        buy_gross = round(shares * fill_price, 4)
                        buy_fees = calculate_fees([(fill_price, shares)])
                        total_cost = buy_gross + buy_fees

                        if total_cost <= cash and total_cost <= 5.0:
                            cash -= total_cost
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
                fill_price = books[outcome]["bids"][0][0]
                sell_gross = round(shares * fill_price, 4)
                sell_fees = calculate_fees([(fill_price, shares)])
                sell_proceeds = sell_gross - sell_fees

                cash += sell_proceeds
                cycle_vol = position["buy_gross_usd"] + sell_gross
                eligible_volume += cycle_vol
                completed_cycles += 1
                position = None

                if eligible_volume >= 1000.0 - 1e-4 and not qualified:
                    qualified = True
                    crossing_cash = cash
                    crossing_market = m_idx + 1
                    break

            peak_cash = max(peak_cash, cash)
            max_drawdown = max(max_drawdown, peak_cash - cash)

        # Market Expiry Settlement (if position was not closed by bot before expiry):
        if position is not None:
            s_expiry = market_candles[4]["close"]
            won = (s_expiry >= s0 and position["outcome"] == "YES") or (s_expiry < s0 and position["outcome"] == "NO")
            if won:
                cash += position["shares"] * 1.0  # $1 per share binary payout
            # Expiry settlement awards $0 volume!
            position = None

    return {
        "qualified": qualified,
        "final_volume": eligible_volume,
        "final_cash": crossing_cash if qualified else cash,
        "max_drawdown": max_drawdown,
        "completed_cycles": completed_cycles,
        "markets_to_qualify": crossing_market if qualified else 288,
    }


if __name__ == "__main__":
    # Test on a real historical day from cache
    cache = Path("data_2024_cache")
    month_file = cache / "btc_2024_01.json"
    if not month_file.exists():
        print(f"File {month_file} not found.")
        exit(1)

    data = json.loads(month_file.read_text())
    day1_candles = data[:1440]

    print("Running Real Bot on Real BTC Historical Day (Jan 1, 2024)...")
    res = run_real_day_evaluation(day1_candles)
    print("Real Bot Result on Jan 1, 2024:", json.dumps(res, indent=2))
