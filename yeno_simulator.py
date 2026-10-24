#!/usr/bin/env python3
"""YeNo 24-Hour Paper Trading Market Simulator & Builderr Segmented Replay Engine.

Simulates the exact Builderr evaluation rules (yeno-evaluator-contract.md):
- 24-hour / 248-to-288 consecutive or resampled 5-minute BTC markets from recorded regimes.
- Starting cash: $10.00.
- High-frequency 2-second L2 order-book tick stream with realistic intra-minute BTC price paths.
- Models 250ms execution latency against subsequent order-book updates.
- Applies Polymarket dynamic taker fee (`round(0.07 * s * p * (1 - p), 5)`) + YeNo 1% overlay (`round(0.01 * gross, 5)`).
- Strict eligible volume accounting (only closed roundtrips count; expiry settlements add $0).
- Tracks every contract violation / decision error (sub-minOrderSize BUYs, unexecutable SELLs, cap breaches, etc.).
- Halts at first clean crossing of $1,000 volume while flat.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any


def calculate_fees(fills: list[tuple[float, float]]) -> float:
    """Polymarket dynamic crypto taker fee + 1% YeNo overlay."""
    gross = sum(p * s for p, s in fills)
    protocol = sum(0.07 * s * p * (1.0 - p) for p, s in fills)
    return round(protocol + 1e-12, 5) + round(gross * 0.01 + 1e-12, 5)


_CACHED_CANDLES: list[dict[str, Any]] | None = None


def _load_historical_candles() -> list[dict[str, Any]]:
    global _CACHED_CANDLES
    if _CACHED_CANDLES is not None:
        return _CACHED_CANDLES
    path = Path("data_btc_historical.json")
    if path.exists():
        _CACHED_CANDLES = json.loads(path.read_text(encoding="utf-8"))
    else:
        _CACHED_CANDLES = []
    return _CACHED_CANDLES


class MarketReplayDay:
    """Replays a 24-hour / 248-to-288 market segmented path under exact Builderr contract rules."""

    def __init__(self, seed: int | None = None, num_markets: int = 248, adverse_cents: float = 0.0) -> None:
        self.seed = seed if seed is not None else 42
        self.rng = random.Random(self.seed)
        self.num_markets = num_markets
        self.adverse_cents = adverse_cents

    def run(self, bot_factory: Any) -> dict[str, Any]:
        try:
            bot = bot_factory(state_file_path=":memory:")
        except TypeError:
            bot = bot_factory()

        cash = 10.0
        eligible_volume = 0.0
        completed_cycles = 0
        total_fees_paid = 0.0
        decision_errors = 0
        error_reasons: list[str] = []
        peak_cash = 10.0
        max_drawdown = 0.0

        position: dict[str, Any] | None = None
        qualified_at_crossing = False
        crossing_cash = 0.0
        crossing_market_idx = 0

        candles = _load_historical_candles()
        total_available_markets = len(candles) // 5

        # Construct resampled segmented blocks of 15-30 recorded markets (matching Builderr segmented diagnostics)
        market_indices: list[int] = []
        if total_available_markets >= 40:
            while len(market_indices) < self.num_markets:
                seg_start = self.rng.randint(0, max(0, total_available_markets - 35))
                seg_len = self.rng.randint(15, 30)
                for m in range(seg_start, min(total_available_markets, seg_start + seg_len)):
                    if len(market_indices) < self.num_markets:
                        market_indices.append(m)
        else:
            market_indices = list(range(self.num_markets))

        for m_idx, src_m in enumerate(market_indices):
            if qualified_at_crossing:
                break

            market_id = f"mkt-{m_idx:04d}"
            bars = candles[src_m * 5 : (src_m + 1) * 5]
            s0 = float(bars[0]["open"])
            base_ts = float(bars[0].get("timestamp", 1789880000.0 + src_m * 300.0))
            p_scale = max(0.035, min(1.5, s0 / 80000.0))

            # Construct 2-second price series (150 steps from 300s down to 0s) from the 5 1-minute bars
            btc_path: list[float] = []
            for bar in bars:
                b_open = float(bar["open"])
                b_high = float(bar["high"])
                b_low = float(bar["low"])
                b_close = float(bar["close"])
                first_ext, second_ext = (b_low, b_high) if b_close >= b_open else (b_high, b_low)
                waypoints = [(0, b_open), (10, first_ext), (20, second_ext), (30, b_close)]
                for step in range(30):
                    for w_i in range(len(waypoints) - 1):
                        t0, p0 = waypoints[w_i]
                        t1, p1 = waypoints[w_i + 1]
                        if t0 <= step <= t1:
                            frac = (step - t0) / max(1, (t1 - t0))
                            interp = p0 + frac * (p1 - p0)
                            noise = self.rng.gauss(0, 1.5 * p_scale) if 0 < step < 29 else 0.0
                            btc_path.append(interp + noise)
                            break

            # Precompute L2 order books along the 2-second tick stream
            lagged_gap = 0.0
            ticks: list[tuple[float, float, float, dict[str, Any]]] = []
            for step_i, cur_btc in enumerate(btc_path):
                t_rem = 300.0 - (step_i + 1) * 2.0
                now = base_ts + (step_i + 1) * 2.0
                true_gap = cur_btc - s0
                lagged_gap = 0.80 * true_gap + 0.20 * lagged_gap
                tau_min = max(0.25, t_rem / 60.0)
                vol_rem = max(10.0 * p_scale, 42.0 * p_scale * math.sqrt(tau_min))
                z = lagged_gap / vol_rem
                prob_yes = max(0.02, min(0.98, 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))))
                prob_no = 1.0 - prob_yes

                spread = self.rng.choice([0.01, 0.02, 0.02, 0.02, 0.03])
                half = spread / 2.0
                yes_ask = min(0.99, max(0.02, round(prob_yes + half, 2)))
                yes_bid = min(yes_ask - 0.01, max(0.01, round(prob_yes - half, 2)))
                no_ask = min(0.99, max(0.02, round(prob_no + half, 2)))
                no_bid = min(no_ask - 0.01, max(0.01, round(prob_no - half, 2)))

                books = {
                    "YES": {
                        "bids": [[yes_bid, self.rng.randint(25, 150)]],
                        "asks": [[yes_ask, self.rng.randint(25, 150)]],
                        "minOrderSize": 5,
                        "observedAt": round(now - 0.1, 3),
                    },
                    "NO": {
                        "bids": [[no_bid, self.rng.randint(25, 150)]],
                        "asks": [[no_ask, self.rng.randint(25, 150)]],
                        "minOrderSize": 5,
                        "observedAt": round(now - 0.1, 3),
                    },
                }
                ticks.append((t_rem, now, cur_btc, books))

            for idx, (t_rem, now, cur_btc, books) in enumerate(ticks):
                observation = {
                    "schemaVersion": 1,
                    "timestamp": now,
                    "market": {
                        "id": market_id,
                        "secondsToClose": round(t_rem, 2),
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
                        "btcMidUsd": round(cur_btc, 3),
                        "openingTargetUsd": round(s0, 3),
                        "observedAt": round(now - 0.15, 3),
                        "targetObservedAt": round(base_ts, 3),
                        "targetProvisional": False,
                    },
                    "books": books,
                }

                decision = bot.decide(observation)
                action = decision.get("action")

                if action not in ("HOLD", "BUY", "SELL"):
                    decision_errors += 1
                    error_reasons.append(f"invalid_action:{action}")
                    continue

                # 250ms execution latency: orders execute against subsequent L2 book state
                exec_books = ticks[min(idx + 1, len(ticks) - 1)][3]

                if action == "BUY":
                    if position is not None:
                        decision_errors += 1
                        error_reasons.append("buy_while_position_open")
                        continue
                    if t_rem <= 15.0:
                        decision_errors += 1
                        error_reasons.append("buy_inside_final_15s")
                        continue
                    outcome = decision.get("outcome")
                    max_cash = float(decision.get("maxCashUsd", 0.0))
                    if outcome not in ("YES", "NO") or max_cash <= 0.0 or max_cash > 5.0 + 1e-6 or max_cash > cash + 1e-6:
                        decision_errors += 1
                        error_reasons.append(f"invalid_buy_params:outcome={outcome},max_cash={max_cash}")
                        continue

                    asks = exec_books[outcome]["asks"]
                    if not asks:
                        decision_errors += 1
                        error_reasons.append("buy_no_asks")
                        continue
                    fill_price = min(0.99, asks[0][0] + self.adverse_cents)
                    fee_est_per_share = (0.07 * fill_price * (1.0 - fill_price)) + (fill_price * 0.01)
                    cost_per_share = fill_price + fee_est_per_share
                    shares = math.floor((min(max_cash, cash, 5.0) + 1e-9) / cost_per_share)

                    if shares < exec_books[outcome]["minOrderSize"]:
                        decision_errors += 1
                        error_reasons.append(f"buy_below_minOrderSize:shares={shares},fill_p={fill_price},max_cash={max_cash}")
                        continue

                    buy_gross = round(shares * fill_price, 4)
                    buy_fees = calculate_fees([(fill_price, shares)])
                    total_spent = buy_gross + buy_fees

                    if total_spent > cash + 1e-5 or total_spent > 5.0 + 1e-5:
                        decision_errors += 1
                        error_reasons.append(f"buy_exceeded_cash:spent={total_spent},cash={cash}")
                        continue

                    cash -= total_spent
                    total_fees_paid += buy_fees
                    position = {
                        "market_id": market_id,
                        "outcome": outcome,
                        "shares": shares,
                        "buy_gross_usd": buy_gross,
                        "buy_fees_usd": buy_fees,
                        "sell_gross_usd": 0.0,
                        "sell_fees_usd": 0.0,
                    }

                elif action == "SELL":
                    if position is None:
                        decision_errors += 1
                        error_reasons.append("sell_when_flat")
                        continue
                    outcome = position["outcome"]
                    shares = position["shares"]
                    bids = exec_books[outcome]["bids"]
                    if not bids or bids[0][1] < shares:
                        decision_errors += 1
                        error_reasons.append("sell_insufficient_bids")
                        continue

                    fill_price = max(0.01, bids[0][0] - self.adverse_cents)
                    sell_gross = round(shares * fill_price, 4)
                    sell_fees = calculate_fees([(fill_price, shares)])
                    sell_proceeds = sell_gross - sell_fees

                    cash += sell_proceeds
                    total_fees_paid += sell_fees
                    cycle_vol = position["buy_gross_usd"] + sell_gross
                    eligible_volume += cycle_vol
                    completed_cycles += 1
                    position = None

                    if eligible_volume >= 1000.0 - 1e-4 and not qualified_at_crossing:
                        qualified_at_crossing = True
                        crossing_cash = cash
                        crossing_market_idx = m_idx + 1
                        break

                current_equity = cash + (position["shares"] * 0.5 if position else 0.0)
                if current_equity > peak_cash:
                    peak_cash = current_equity
                dd = peak_cash - current_equity
                if dd > max_drawdown:
                    max_drawdown = dd

            # End of 5m market: Expiry settlements award ZERO eligible volume!
            if position is not None:
                final_gap = btc_path[-1] - s0
                won = (final_gap >= 0 and position["outcome"] == "YES") or (final_gap < 0 and position["outcome"] == "NO")
                if won:
                    cash += position["shares"] * 1.0
                position = None

        return {
            "qualified": qualified_at_crossing,
            "final_volume": round(eligible_volume, 2),
            "final_cash": round(crossing_cash if qualified_at_crossing else cash, 2),
            "max_drawdown": round(max_drawdown, 2),
            "completed_cycles": completed_cycles,
            "total_fees": round(total_fees_paid, 2),
            "decision_errors": decision_errors,
            "error_reasons": error_reasons[:5],
            "market_reached": crossing_market_idx if qualified_at_crossing else self.num_markets,
            "finished_flat": position is None,
        }


def benchmark_bots(num_runs: int = 10) -> None:
    from yeno_production_bot import ProductionTradingBot
    from importlib import import_module

    house_module = import_module("yeno-house-bot-v1")

    print("=" * 75)
    print(f"RUNNING BUILDERR SEGMENTED REPLAY BENCHMARK ({num_runs} PATHS OF 248 MARKETS)")
    print("=" * 75)

    prod_results = []
    for i in range(num_runs):
        sim = MarketReplayDay(seed=100 + i, num_markets=248)
        res = sim.run(ProductionTradingBot)
        prod_results.append(res)

    prod_qualified = sum(1 for r in prod_results if r["qualified"])
    prod_volumes = [r["final_volume"] for r in prod_results]
    prod_cashes = [r["final_cash"] for r in prod_results]
    prod_errors = sum(r["decision_errors"] for r in prod_results)

    print("\n--- OUR PRODUCTION BOT RESULTS ---")
    print(f"Qualification Rate (>= $1000 volume & flat): {prod_qualified}/{num_runs} ({prod_qualified/num_runs*100:.1f}%)")
    print(f"Median Volume:            ${sorted(prod_volumes)[len(prod_volumes)//2]:.2f}")
    print(f"Average Cash at Crossing: ${sum(prod_cashes)/len(prod_cashes):.2f}")
    print(f"Highest Cash at Crossing: ${max(prod_cashes):.2f}")
    print(f"Total Decision Errors:    {prod_errors}")

    house_results = []
    for i in range(num_runs):
        sim = MarketReplayDay(seed=100 + i, num_markets=248)
        res = sim.run(house_module.HouseBot)
        house_results.append(res)

    house_qualified = sum(1 for r in house_results if r["qualified"])
    house_volumes = [r["final_volume"] for r in house_results]
    house_cashes = [r["final_cash"] for r in house_results]
    house_errors = sum(r["decision_errors"] for r in house_results)

    print("\n--- HOUSE BOT v1 RESULTS ---")
    print(f"Qualification Rate (>= $1000 volume & flat): {house_qualified}/{num_runs} ({house_qualified/num_runs*100:.1f}%)")
    print(f"Median Volume:            ${sorted(house_volumes)[len(house_volumes)//2]:.2f}")
    print(f"Average Cash:             ${sum(house_cashes)/len(house_cashes):.2f}")
    print(f"Total Decision Errors:    {house_errors}")
    print("=" * 75)


if __name__ == "__main__":
    benchmark_bots(10)
