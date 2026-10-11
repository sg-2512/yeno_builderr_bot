#!/usr/bin/env python3
"""YeNo Production Trading Bot — 2024 Full-Year Complete Quantitative Evaluation.

Evaluates the bot across every single 24-hour trading day of the full leap year 2024:
- Exactly 366 distinct, consecutive 24-hour trading days (January 1, 2024 to December 31, 2024).
- Exactly 527,040 continuous 1-minute bars.
- Exactly 105,408 consecutive 5-minute prediction rounds.
- Strictly $10.00 cash reset daily for every individual day.
- Caches monthly datasets locally in `data_2024_cache/` for rapid re-evaluation.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from yeno_production_bot import _compute_ema, _normal_cdf

CACHE_DIR = Path("data_2024_cache")
CACHE_DIR.mkdir(exist_ok=True)


def fees(p: float, s: float) -> float:
    return round(0.07 * s * p * (1.0 - p), 5) + round(s * p * 0.01, 5)


def fetch_candle_batch(start_ms: int, end_ms: int) -> list[dict[str, float]]:
    url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime={start_ms}&endTime={end_ms}&limit=1000"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                batch = json.loads(resp.read())
            candles = []
            for row in batch:
                candles.append({
                    "timestamp": float(row[0]) / 1000.0,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                })
            return candles
        except Exception:
            time.sleep(1.0 + attempt * 1.5)
    return []


def download_or_load_month(year: int, month: int) -> list[dict[str, float]]:
    month_file = CACHE_DIR / f"btc_{year}_{month:02d}.json"
    if month_file.exists():
        try:
            data = json.loads(month_file.read_text())
            if len(data) >= 1440:
                print(f"Loaded existing Month {month:02d} ({len(data)} candles) from cache.")
                return data
        except Exception:
            pass

    # Month start and end timestamps in UTC
    t_start = int(datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    if month == 12:
        t_end = int(datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    else:
        t_end = int(datetime(year, month + 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    total_expected_ms = t_end - t_start
    total_expected_bars = total_expected_ms // 60000
    print(f"Downloading Month {month:02d}/{year} ({total_expected_bars} bars) via concurrent threads...")

    starts = list(range(t_start, t_end, 1000 * 60000))
    tasks = []
    for s in starts:
        e = min(t_end, s + 1000 * 60000 - 1)
        tasks.append((s, e))

    all_candles = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_candle_batch, s, e) for s, e in tasks]
        for f in futures:
            all_candles.extend(f.result())

    seen = set()
    unique = []
    for c in sorted(all_candles, key=lambda x: x["timestamp"]):
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"])
            unique.append(c)

    print(f"Downloaded Month {month:02d}/{year} ({len(unique)} candles) in {time.time()-t0:.1f}s.")
    month_file.write_text(json.dumps(unique))
    return unique


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

            # Multi-Market EMA Alignment (Cross Check) with Closing Mode Scaling
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

                    peak_cash = max(peak_cash, cash)
                    max_dd = max(max_dd, peak_cash - cash)

                    if vol >= 1000.0:
                        qual = True
                        qual_cash = cash
                        qual_round = m + 1
                        break

    final_cash = qual_cash if qual else cash
    elapsed_hours = (qual_round * 5.0) / 60.0 if qual else 24.0
    win_rate = (wins / cycles * 100.0) if cycles > 0 else 0.0

    return {
        "day_label": day_label,
        "qualified": qual,
        "settled_cash": final_cash,
        "volume": vol,
        "win_rate": win_rate,
        "cycles": cycles,
        "elapsed_hours": elapsed_hours,
        "max_drawdown": max_dd,
    }


def main() -> None:
    print("=" * 105)
    print("YENO PRODUCTION BOT — 2024 FULL-YEAR COMPLETE QUANTITATIVE EVALUATION (LEAP YEAR: 366 DAYS)")
    print("=" * 105)

    months_config = [
        ("Jan", 1, 31),
        ("Feb", 2, 29),  # 2024 was a leap year!
        ("Mar", 3, 31),
        ("Apr", 4, 30),
        ("May", 5, 31),
        ("Jun", 6, 30),
        ("Jul", 7, 31),
        ("Aug", 8, 31),
        ("Sep", 9, 30),
        ("Oct", 10, 31),
        ("Nov", 11, 30),
        ("Dec", 12, 31),
    ]

    all_days_results = []
    month_summaries = []

    grand_t0 = time.time()

    for m_name, m_num, num_days in months_config:
        print(f"\n>>> Processing Month {m_num:02d} ({m_name} 2024)...")
        candles = download_or_load_month(2024, m_num)

        expected_candles = num_days * 1440
        if len(candles) < expected_candles:
            print(f"Warning: Month {m_name} has {len(candles)} candles, expected {expected_candles}")

        print(f"Evaluating {num_days} consecutive 24-hour days in {m_name} 2024...")
        m_results = []
        for d in range(num_days):
            day_candles = candles[d * 1440 : (d + 1) * 1440]
            if len(day_candles) < 1440:
                print(f"Skipping Day {d+1}: incomplete candle data ({len(day_candles)}/1440)")
                continue

            day_label = f"{m_name} {d+1:02d}, 2024"
            res = evaluate_single_24h_day(day_candles, day_label)
            m_results.append(res)
            all_days_results.append(res)

        # Monthly roll-up
        m_qual = sum(1 for r in m_results if r["qualified"])
        m_rate = (m_qual / len(m_results)) * 100.0 if m_results else 0.0
        m_cash = sum(r["settled_cash"] for r in m_results) / len(m_results) if m_results else 0.0
        m_wr = sum(r["win_rate"] for r in m_results) / len(m_results) if m_results else 0.0
        m_finish = sum(r["elapsed_hours"] for r in m_results) / len(m_results) if m_results else 0.0

        month_summaries.append({
            "name": f"{m_name} 2024",
            "days": len(m_results),
            "qual": m_qual,
            "rate": m_rate,
            "cash": m_cash,
            "wr": m_wr,
            "finish": m_finish,
        })

        print(f"--- {m_name} 2024 SUMMARY: {m_qual}/{len(m_results)} Qualified ({m_rate:.1f}%) | "
              f"Avg Cash: ${m_cash:.2f} | Avg WR: {m_wr:.1f}% | Avg Finish: Hour {m_finish:.1f} ---")

    # Grand Scorecard Table
    print("\n" + "=" * 105)
    print("2024 FULL-YEAR MONTH-BY-MONTH SCORECARD SUMMARY (LEAP YEAR)")
    print("=" * 105)
    print(f"{'Month':<12} | {'Days':<6} | {'Qualified':<12} | {'Pass Rate':<10} | {'Avg Cash':<12} | {'Avg WR':<10} | {'Avg Finish Time'}")
    print("-" * 105)
    for ms in month_summaries:
        print(f"{ms['name']:<12} | {ms['days']:<6} | {ms['qual']}/{ms['days']:<10} | {ms['rate']:>5.1f}%     | "
              f"${ms['cash']:>7.2f}    | {ms['wr']:>5.1f}%    | Hour {ms['finish']:.1f}")
    print("=" * 105)

    tot_days = len(all_days_results)
    tot_qual = sum(1 for r in all_days_results if r["qualified"])
    tot_rate = (tot_qual / tot_days) * 100.0 if tot_days else 0.0
    tot_cash = sum(r["settled_cash"] for r in all_days_results) / tot_days if tot_days else 0.0
    tot_wr = sum(r["win_rate"] for r in all_days_results) / tot_days if tot_days else 0.0
    tot_finish = sum(r["elapsed_hours"] for r in all_days_results) / tot_days if tot_days else 0.0

    print("GRAND 2024 FULL-YEAR TOTALS:")
    print(f"  * Total 24-Hour Trading Days Evaluated: {tot_days} days (Leap Year)")
    print(f"  * Total Continuous 1-Minute Bars:       {tot_days * 1440:,} bars")
    print(f"  * Total 5-Minute Prediction Rounds:     {tot_days * 288:,} markets")
    print(f"  * Total Days Qualified (>= $1,000 vol): {tot_qual}/{tot_days} ({tot_rate:.2f}%)")
    print(f"  * Overall Average Settled Cash:         ${tot_cash:.2f} (from $10.00 starting cash)")
    print(f"  * Overall Average Win Rate:             {tot_wr:.1f}%")
    print(f"  * Overall Average Completion Time:      Hour {tot_finish:.1f} of 24.0h")
    print(f"  * Completed in:                         {time.time()-grand_t0:.1f}s")
    print("=" * 105)


if __name__ == "__main__":
    main()
