#!/usr/bin/env python3
"""Fetch Real Historical BTC 1-Minute Candle Data from Public Crypto API.

Downloads historical minute-level price bars for training and calibrating
the YeNo prediction market decision engine.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path


def fetch_historical_btc(target_candles: int = 2500) -> list[dict[str, float]]:
    print(f"Fetching {target_candles} real BTC 1-minute bars from public market feed...")
    candles: list[dict[str, float]] = []
    end_time = int(time.time() * 1000)
    batch_size = 1000

    while len(candles) < target_candles:
        url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit={batch_size}&endTime={end_time}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                batch = json.loads(resp.read())
        except Exception as e:
            print(f"Error fetching batch: {e}")
            break

        if not batch:
            break

        parsed_batch = []
        for row in batch:
            parsed_batch.append({
                "timestamp": float(row[0]) / 1000.0,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })

        # Append in chronological order
        candles = parsed_batch + candles
        end_time = int(batch[0][0]) - 1
        print(f"  Downloaded {len(candles)} candles so far (Earliest: {candles[0]['timestamp']})...")
        time.sleep(0.3)

    # Deduplicate and sort
    seen = set()
    unique_candles = []
    for c in sorted(candles, key=lambda x: x["timestamp"]):
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"])
            unique_candles.append(c)

    output_path = Path("data_btc_historical.json")
    output_path.write_text(json.dumps(unique_candles, indent=2))
    print(f"\nSuccessfully saved {len(unique_candles)} clean 1-minute BTC bars to {output_path.name}")
    print(f"Time span: {len(unique_candles) / 60:.1f} hours ({len(unique_candles) / 5:.0f} real 5-minute market rounds).")
    return unique_candles


if __name__ == "__main__":
    fetch_historical_btc(2500)
