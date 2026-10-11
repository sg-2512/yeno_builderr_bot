#!/usr/bin/env python3
"""Download and Test Strictly 24-Hour Datasets (Exactly 1,440 1-min bars = 288 5-min rounds).

Mirrors the EXACT official Builderr evaluation window:
- Exactly 24.0 hours (288 rounds) per test.
- Starting cash: $10.00.
- Evaluates across 7 distinct consecutive 24-hour trading days from Binance.
"""

from __future__ import annotations

import json
import math
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from yeno_production_bot import ProductionTradingBot, _normal_cdf, _compute_ema


def fees(p: float, s: float) -> float:
    return round(0.07 * s * p * (1.0 - p), 5) + round(s * p * 0.01, 5)


def fetch_24h_candle_day(filename: str, end_time_sec: float) -> list[dict[str, float]]:
    target_count = 1440  # Exactly 24 hours * 60 minutes
    path = Path(filename)
    if path.exists():
        data = json.loads(path.read_text())
        if len(data) == target_count:
            return data

    print(f"Downloading strictly 24-hour dataset: {filename}...")
    candles: list[dict[str, float]] = []
    end_time_ms = int(end_time_sec * 1000)
    batch_size = 1000

    while len(candles) < target_count:
        url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit={batch_size}&endTime={end_time_ms}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                batch = json.loads(resp.read())
        except Exception as e:
            print(f"Error fetching batch: {e}")
            break

        if not batch:
            break

        parsed = []
        for row in batch:
            parsed.append({
                "timestamp": float(row[0]) / 1000.0,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })

        candles = parsed + candles
        end_time_ms = int(batch[0][0]) - 1
        time.sleep(0.15)

    seen = set()
    unique = []
    for c in sorted(candles, key=lambda x: x["timestamp"]):
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"])
            unique.append(c)

    # Trim to exactly 1440 bars (last 24 hours of that window)
    if len(unique) > target_count:
        unique = unique[-target_count:]

    path.write_text(json.dumps(unique, indent=2))
    return unique


def evaluate_24h_day(candles: list[dict[str, float]], day_name: str) -> dict[str, any]:
    assert len(candles) == 1440, f"Expected exactly 1440 bars for 24h, got {len(candles)}"
    num_markets = 288  # Exactly 288 5-minute markets in 24 hours

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

    rolling_history: list[float] = []

    for m in range(num_markets):
        if qual:
            break
        if m < cooldown_until_market:
            continue

        bars = candles[m * 5 : (m + 1) * 5]
        s0 = bars[0]["open"]
        s2 = bars[2]["close"]
        gap2 = s2 - s0

        for b in bars[:3]:
            rolling_history.append(b["close"])
        if len(rolling_history) > 60:
            rolling_history = rolling_history[-60:]

        # Real-time Volatility Estimator (floor 35.0, ceiling 100.0)
        recent = rolling_history[-15:] if len(rolling_history) >= 15 else rolling_history
        local_range = max(recent) - min(recent) if len(recent) >= 5 else 45.0
        sigma = max(35.0, min(100.0, local_range * 1.2))

        # Dynamic Regime Adaptation
        if sigma < 42.0:
            min_gap = 13.0
        elif sigma < 65.0:
            min_gap = 14.0
        else:
            min_gap = 16.0

        spike_limit = 115.0

        # Two-Tier Closing Sprint Accelerator
        if m >= 210 and cash >= 15.0 and vol < 1000.0:
            min_gap = max(9.5, min_gap - 4.5)
        if m >= 240 and cash >= 20.0 and vol < 1000.0:
            min_gap = max(7.5, min_gap - 6.5)

        # Extreme Volatility Spike Shield
        hl0 = abs(bars[0]["high"] - bars[0]["low"])
        hl1 = abs(bars[1]["high"] - bars[1]["low"])
        hl2 = abs(bars[2]["high"] - bars[2]["low"])
        if max(hl0, hl1, hl2) >= spike_limit:
            continue

        if abs(gap2) >= min_gap and cash >= 1.50:
            side = "YES" if gap2 > 0 else "NO"

            # Multi-Market EMA Alignment (Cross Check)
            if len(rolling_history) >= 8:
                fast_ema = _compute_ema(rolling_history, period=5)
                slow_ema = _compute_ema(rolling_history, period=15)
                if side == "NO" and fast_ema > slow_ema + 7.0:
                    continue
                if side == "YES" and fast_ema < slow_ema - 7.0:
                    continue

            z2 = (gap2 if side == "YES" else -gap2) / (sigma * math.sqrt(3.0))
            p2 = _normal_cdf(z2)

            ask_ceiling = 0.55 if cash < 4.0 else 0.82
            max_affordable_ask = min(ask_ceiling, (cash - 0.10) / 5.2)
            if max_affordable_ask < 0.35:
                continue
            ask = min(max_affordable_ask, max(0.40, round(p2 + 0.01, 2)))

            # Risk-Managed Bankroll Sizing
            if cash < 6.0:
                spend = min(2.50, max(1.80, cash * 0.35))
            elif cash < 12.0:
                spend = min(3.50, cash * 0.38)
            else:
                spend = 4.88

            fee_est = (0.07 * ask * (1.0 - ask)) + (ask * 0.01)
            shares = math.floor(spend / (ask + fee_est))

            if shares < 5:
                needed = 5 * (ask + fee_est) + 0.05
                if cash >= needed:
                    shares = 5

            if shares >= 5:
                buy_gross = shares * ask
                buy_fee = fees(ask, shares)
                tot = buy_gross + buy_fee

                if tot <= cash:
                    cash -= tot

                    # Minute 3 check
                    s3 = bars[3]["close"]
                    gap3 = s3 - s0
                    z3 = (gap3 if side == "YES" else -gap3) / (sigma * math.sqrt(2.0))
                    p3 = _normal_cdf(z3)
                    bid3 = min(0.99, max(0.01, round(p3 - 0.01, 2)))
                    proceeds3 = (shares * bid3) - fees(bid3, shares)
                    pnl3 = proceeds3 - tot

                    s4 = bars[4]["close"]
                    gap4 = s4 - s0
                    momentum_decay = (gap3 < gap2 * 0.4 if side == "YES" else gap3 > gap2 * 0.4)

                    # 1. Take Profit (+1c/share harvest)
                    if pnl3 >= 0.01 * shares:
                        cash += proceeds3
                        vol += buy_gross + (shares * bid3)
                        cycles += 1
                        consecutive_losses = 0
                        wins += 1
                    # 2. Dual-Confirmation Stop Loss
                    elif pnl3 <= -0.10 * shares and momentum_decay:
                        cash += proceeds3
                        vol += buy_gross + (shares * bid3)
                        cycles += 1
                        losses += 1
                        consecutive_losses += 1
                        cooldown_until_market = m + (2 if cash < 6.0 or consecutive_losses >= 2 else 1)
                    else:
                        in_the_money = (gap4 > 0 if side == "YES" else gap4 < 0)
                        if in_the_money:
                            bid4 = min(0.96, max(0.85, 0.90 + min(0.06, abs(gap4) * 0.001)))
                            wins += 1
                            consecutive_losses = 0
                        else:
                            bid4 = max(0.04, min(0.15, 0.10 - min(0.06, abs(gap4) * 0.001)))
                            losses += 1
                            consecutive_losses += 1
                            cooldown_until_market = m + (2 if cash < 6.0 or consecutive_losses >= 2 else 1)

                        proceeds4 = (shares * bid4) - fees(bid4, shares)
                        cash += proceeds4
                        vol += buy_gross + (shares * bid4)
                        cycles += 1

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
    elapsed_hours = (qual_round * 5.0 / 60.0) if qual else 24.0

    return {
        "day_name": day_name,
        "qualified": qual,
        "settled_cash": qual_cash if qual else cash,
        "profit": (qual_cash - 10.0) if qual else (cash - 10.0),
        "volume": vol,
        "win_rate": wr,
        "cycles": cycles,
        "qual_round": qual_round if qual else num_markets,
        "elapsed_hours": elapsed_hours,
        "max_drawdown": max_dd,
    }


def main() -> None:
    print("=" * 85)
    print("YENO OFFICIAL 24-HOUR EVALUATION SUITE (EXACTLY 1,440 BARS / 288 MARKETS)")
    print("=" * 85)

    # Current timestamp reference
    t_now = 1791692000.0  # Approx Oct 11 04:00 UTC
    sec_in_day = 86400.0

    day_configs = [
        ("Day 1 (Oct 10 - Oct 11, 24h)", "data_24h_day1.json", t_now),
        ("Day 2 (Oct 09 - Oct 10, 24h)", "data_24h_day2.json", t_now - sec_in_day),
        ("Day 3 (Oct 08 - Oct 09, 24h)", "data_24h_day3.json", t_now - 2 * sec_in_day),
        ("Day 4 (Oct 07 - Oct 08, 24h)", "data_24h_day4.json", t_now - 3 * sec_in_day),
        ("Day 5 (Oct 06 - Oct 07, 24h)", "data_24h_day5.json", t_now - 4 * sec_in_day),
        ("Day 6 (Oct 05 - Oct 06, 24h)", "data_24h_day6.json", t_now - 5 * sec_in_day),
        ("Day 7 (Oct 04 - Oct 05, 24h)", "data_24h_day7.json", t_now - 6 * sec_in_day),
    ]

    results = []

    for label, fname, end_t in day_configs:
        candles = fetch_24h_candle_day(fname, end_t)
        res = evaluate_24h_day(candles, label)
        results.append(res)

    print("\n" + "=" * 85)
    print("OFFICIAL 24-HOUR TEST SCORECARD ACROSS 7 CONSECUTIVE 24-HOUR DAYS")
    print("=" * 85)
    print(f"{'Evaluation Day (24h Window)':<30} | {'Status':<10} | {'Settled Cash':<12} | {'Volume':<10} | {'Win Rate':<9} | {'Finish Time':<11}")
    print("-" * 85)

    for r in results:
        status_str = "QUALIFIED" if r["qualified"] else "FAILED"
        cash_str = f"${r['settled_cash']:.2f}"
        vol_str = f"${r['volume']:.2f}"
        wr_str = f"{r['win_rate']:.1f}%"
        finish_str = f"Hour {r['elapsed_hours']:.1f}" if r["qualified"] else "Exceeded 24h"
        print(f"{r['day_name']:<30} | {status_str:<10} | {cash_str:<12} | {vol_str:<10} | {wr_str:<9} | {finish_str:<11}")

    print("-" * 85)
    print("\nSUMMARY STATS ACROSS ALL 7 DAYS:")
    qualified_count = sum(1 for r in results if r["qualified"])
    avg_cash = sum(r["settled_cash"] for r in results) / len(results)
    avg_wr = sum(r["win_rate"] for r in results) / len(results)
    avg_time = sum(r["elapsed_hours"] for r in results if r["qualified"]) / max(1, qualified_count)

    print(f"  • Total 24h Days Tested:      {len(results)}")
    print(f"  • Days Qualified:             {qualified_count}/{len(results)} ({qualified_count/len(results)*100:.1f}%)")
    print(f"  • Average Settled Cash:       ${avg_cash:.2f}")
    print(f"  • Average Win Rate:           {avg_wr:.1f}%")
    print(f"  • Average Time to Finish:     Hour {avg_time:.1f} (well before 24.0h deadline)")
    print("=" * 85)


if __name__ == "__main__":
    main()
