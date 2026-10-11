#!/usr/bin/env python3
"""Download and Evaluate Full September (30 Distinct 24-Hour Trading Days).

Evaluates the YeNo Production Trading Bot across the ENTIRE month of September:
- 30 individual 24-hour trading days (Sept 01 to Sept 30).
- Exactly 1,440 1-minute bars per day (= 43,200 continuous 1-minute bars total).
- Exactly 288 5-minute prediction rounds per day.
- Starting cash: $10.00 each day.
- Unified Production Engine with Dynamic Regime Adaptation, Multi-Market EMA, and Closing Sprint.
"""

from __future__ import annotations

import json
import math
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from yeno_production_bot import _normal_cdf, _compute_ema


def fees(p: float, s: float) -> float:
    return round(0.07 * s * p * (1.0 - p), 5) + round(s * p * 0.01, 5)


def download_september_data(output_file: str = "data_september_full.json") -> list[dict[str, float]]:
    path = Path(output_file)
    target_count = 30 * 1440  # 43,200 1-minute bars
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if len(data) >= target_count:
                print(f"Loaded existing full September dataset ({len(data)} candles) from {output_file}")
                return data[:target_count]
        except Exception:
            pass

    print(f"Downloading full September dataset (43,200 1-minute candles) from Binance API...")
    t_start = int(datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    t_end = int(datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    candles: list[dict[str, float]] = []
    cur_time = t_start
    batch_size = 1000

    while cur_time < t_end:
        url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime={cur_time}&endTime={t_end}&limit={batch_size}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                batch = json.loads(resp.read())
        except Exception as e:
            print(f"Warning: Fetch error at {cur_time}: {e}, retrying in 1s...")
            time.sleep(1.0)
            continue

        if not batch:
            break

        for row in batch:
            candles.append({
                "timestamp": float(row[0]) / 1000.0,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })

        cur_time = int(batch[-1][0]) + 60000
        if len(candles) % 5000 == 0 or cur_time >= t_end:
            print(f"  Fetched {len(candles)} / {target_count} bars ({(len(candles)/target_count)*100:.1f}%)...")
        time.sleep(0.12)

    seen = set()
    unique = []
    for c in sorted(candles, key=lambda x: x["timestamp"]):
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"])
            unique.append(c)

    path.write_text(json.dumps(unique))
    print(f"Successfully downloaded and saved {len(unique)} candles to {output_file}!")
    return unique[:target_count]


def evaluate_single_24h_day(candles_24h: list[dict[str, float]], day_label: str) -> dict[str, any]:
    assert len(candles_24h) == 1440, f"Expected 1440 bars, got {len(candles_24h)}"
    num_markets = 288

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

        bars = candles_24h[m * 5 : (m + 1) * 5]
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
        sigma = max(28.0, min(120.0, local_range * 1.2))

        # Dynamic Base Regime Gap
        min_gap = max(10.5, min(30.0, sigma * 0.22))
        spike_limit = max(115.0, sigma * 2.8)

        # Continuous Volume Run-Rate Governor
        expected_vol = (m / 288.0) * 1000.0
        if cash >= 10.5:
            if m >= 72 and vol < expected_vol:           # Hour 6+
                min_gap = max(7.5, min_gap - 3.0)
            if m >= 144 and vol < expected_vol:          # Hour 12+
                min_gap = max(6.0, min_gap - 5.0)
            if m >= 192 and vol < 1000.0:                # Hour 16+
                min_gap = max(4.5, min_gap - 7.0)
            if m >= 240 and vol < 1000.0:                # Hour 20+
                min_gap = max(3.0, min_gap - 9.0)
            if m >= 264 and vol < 1000.0:                # Hour 22+
                min_gap = max(1.5, min_gap - 11.0)

        # High-Cash Early Pacing Acceleration
        if cash >= 25.0 and m >= 120 and vol < expected_vol: min_gap = max(2.5, min_gap - 6.0)
        if cash >= 20.0 and m >= 168 and vol < 850.0: min_gap = max(2.0, min_gap - 8.0)
        if cash >= 15.0 and m >= 192 and vol < 1000.0: min_gap = max(1.0, min_gap - 14.0)
        elif cash >= 10.5 and m >= 216 and vol < 1000.0: min_gap = max(1.0, min_gap - 14.0)

        # Late-session finish-line ceiling
        if cash >= 8.0:
            if m >= 216 and vol < 1000.0: min_gap = min(min_gap, 4.0)
            if m >= 240 and vol < 1000.0: min_gap = min(min_gap, 2.5)
            if m >= 264 and vol < 1000.0: min_gap = min(min_gap, 1.0)

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
                ema_tol = 18.0 if (cash >= 15.0 and m >= 192) else 7.0
                if side == "NO" and fast_ema > slow_ema + ema_tol:
                    continue
                if side == "YES" and fast_ema < slow_ema - ema_tol:
                    continue

            z2 = (gap2 if side == "YES" else -gap2) / (sigma * math.sqrt(3.0))
            p2 = _normal_cdf(z2)

            if cash < 4.0:
                ask_ceiling = 0.50
            elif cash < 5.5:
                ask_ceiling = 0.54
            elif cash < 12.0:
                ask_ceiling = 0.68
            else:
                ask_ceiling = 0.82

            max_affordable_ask = min(ask_ceiling, (cash - 0.10) / 5.2)
            if max_affordable_ask < 0.35:
                continue
            ask = min(max_affordable_ask, max(0.40, round(p2 + 0.01, 2)))

            fee_est = (0.07 * ask * (1.0 - ask)) + (ask * 0.01)

            # Risk-Managed Bankroll Sizing
            if cash < 12.0:
                shares = 5
            elif cash < 5.5:
                spend = min(2.40, max(1.80, cash * 0.35))
                shares = math.floor(spend / (ask + fee_est))
            elif cash < 10.0:
                spend = min(3.40, cash * 0.38)
                shares = math.floor(spend / (ask + fee_est))
            else:
                spend = 4.88
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

                    # 1. Take Profit (+1c/share harvest)
                    if pnl3 >= 0.01 * shares:
                        cash += proceeds3
                        vol += buy_gross + (shares * bid3)
                        cycles += 1
                        consecutive_losses = 0
                        wins += 1
                    # 2. Dual-Confirmation Stop Loss
                    elif pnl3 <= -0.10 * shares and (gap3 < -12.0 if side == "YES" else gap3 > 12.0):
                        cash += proceeds3
                        vol += buy_gross + (shares * bid3)
                        cycles += 1
                        losses += 1
                        consecutive_losses += 1
                        cooldown_until_market = m + 2
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
                            cooldown_until_market = m + 2

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
        "day_label": day_label,
        "qualified": qual,
        "settled_cash": qual_cash if qual else cash,
        "volume": vol,
        "win_rate": wr,
        "cycles": cycles,
        "elapsed_hours": elapsed_hours,
        "max_drawdown": max_dd,
    }


def main() -> None:
    print("=" * 105)
    print("YENO PRODUCTION BOT — FULL SEPTEMBER EVALUATION (30 CONSECUTIVE 24-HOUR DAYS)")
    print("=" * 105)

    all_candles = download_september_data("data_september_full.json")
    print(f"Total September candles available: {len(all_candles)}")
    num_days = min(30, len(all_candles) // 1440)
    print(f"Running 24-hour evaluation across {num_days} consecutive September days...\n")

    results = []

    for d in range(num_days):
        day_candles = all_candles[d * 1440 : (d + 1) * 1440]
        t0 = day_candles[0]["timestamp"]
        dt_str = datetime.fromtimestamp(t0, timezone.utc).strftime("%b %d, %Y")
        label = f"Sept {d+1:02d} ({dt_str})"
        res = evaluate_single_24h_day(day_candles, label)
        results.append(res)

        status_str = "QUALIFIED" if res["qualified"] else "FAILED"
        finish_str = f"Hour {res['elapsed_hours']:4.1f}" if res["qualified"] else "Exceeded 24h"
        print(f"[{d+1:02d}/30] {label:<22} | {status_str:<10} | Cash: ${res['settled_cash']:6.2f} | Vol: ${res['volume']:7.2f} | WR: {res['win_rate']:4.1f}% | Finish: {finish_str}")

    print("\n" + "=" * 105)
    print("FULL SEPTEMBER 30-DAY SCORECARD SUMMARY")
    print("=" * 105)

    qual_count = sum(1 for r in results if r["qualified"])
    avg_cash_qual = sum(r["settled_cash"] for r in results if r["qualified"]) / max(1, qual_count)
    avg_cash_all = sum(r["settled_cash"] for r in results) / len(results)
    avg_wr = sum(r["win_rate"] for r in results) / len(results)
    avg_time = sum(r["elapsed_hours"] for r in results if r["qualified"]) / max(1, qual_count)
    avg_dd = sum(r["max_drawdown"] for r in results) / len(results)

    print(f"  * Total 24h Trading Days:           {len(results)} days")
    print(f"  * Days Successfully Qualified:      {qual_count}/{len(results)} ({qual_count/len(results)*100:.1f}%)")
    print(f"  * Average Settled Cash (Qualified): ${avg_cash_qual:.2f} (from $10.00 starting bankroll)")
    print(f"  * Average Settled Cash (Overall):   ${avg_cash_all:.2f}")
    print(f"  * Average Win Rate:                  {avg_wr:.1f}%")
    print(f"  * Average Finish Time:               Hour {avg_time:.1f} of 24.0h")
    print(f"  * Average Max Drawdown:              ${avg_dd:.2f}")
    print("=" * 105)


if __name__ == "__main__":
    main()
