#!/usr/bin/env python3
"""Fetch completely unseen, out-of-sample real BTC datasets from Binance.

Fetches historical periods that the bot has NEVER seen or been calibrated on:
- Set 1: Prior 50 hours (Ending right before current dataset)
- Set 2: An even earlier 50-hour window (Previous week regime)
"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def fetch_unseen_btc(target_filename: str, end_timestamp_sec: float, count: int = 3000) -> list[dict[str, float]]:
    print(f"\n=======================================================")
    print(f"Fetching {count} bars for {target_filename}")
    end_dt = datetime.fromtimestamp(end_timestamp_sec, timezone.utc)
    print(f"Target End Date: {end_dt.isoformat()}")
    print(f"=======================================================")

    candles: list[dict[str, float]] = []
    end_time_ms = int(end_timestamp_sec * 1000)
    batch_size = 1000

    while len(candles) < count:
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
        print(f"  Downloaded {len(candles)}/{count} bars (Earliest in buffer: {datetime.fromtimestamp(candles[0]['timestamp'], timezone.utc).isoformat()})...")
        time.sleep(0.2)

    seen = set()
    unique = []
    for c in sorted(candles, key=lambda x: x["timestamp"]):
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"])
            unique.append(c)

    # Trim to exact count from end if we fetched more
    if len(unique) > count:
        unique = unique[-count:]

    out_path = Path(target_filename)
    out_path.write_text(json.dumps(unique, indent=2))
    t_start = datetime.fromtimestamp(unique[0]["timestamp"], timezone.utc).isoformat()
    t_end = datetime.fromtimestamp(unique[-1]["timestamp"], timezone.utc).isoformat()
    print(f"Saved {len(unique)} bars to {target_filename}")
    print(f"Range: {t_start} to {t_end}")
    return unique


if __name__ == "__main__":
    # Current dataset ends around Oct 8 08:08 UTC
    # Set 1: Oct 6 06:00 to Oct 8 08:00 UTC (3000 minutes = 50 hours)
    t_set1_end = 1791446400.0  # Approx Oct 8 08:00 UTC
    # Let's inspect timestamp of current dataset start to be exact:
    current_data = json.load(open("data_btc_historical.json"))
    t_current_start = current_data[0]["timestamp"]

    # Set 1 ends right when current set starts
    fetch_unseen_btc("data_btc_unseen_set1.json", end_timestamp_sec=t_current_start - 60, count=3000)

    # Set 2: Even earlier (starts another 50 hours prior)
    set1_data = json.load(open("data_btc_unseen_set1.json"))
    t_set1_start = set1_data[0]["timestamp"]
    fetch_unseen_btc("data_btc_unseen_set2.json", end_timestamp_sec=t_set1_start - 60, count=3000)
