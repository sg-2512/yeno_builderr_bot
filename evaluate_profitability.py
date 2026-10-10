#!/usr/bin/env python3
"""Detailed Profitability & Trade Economics Evaluator.

Analyzes trade execution, net PnL per cycle, volume velocity, and ending cash across simulated market days.
"""

from __future__ import annotations

import math
import random
from yeno_simulator import MarketReplayDay, calculate_fees
from yeno_production_bot import ProductionTradingBot


def run_evaluation(num_days: int = 5) -> None:
    print("=" * 70)
    print("EVALUATING TRADE ECONOMICS & PROFITABILITY")
    print("=" * 70)

    for day in range(1, num_days + 1):
        sim = MarketReplayDay(seed=100 + day * 37)
        res = sim.run(ProductionTradingBot)
        
        cycles = res["completed_cycles"]
        vol = res["final_volume"]
        cash = res["final_cash"]
        avg_vol_per_cycle = vol / max(1, cycles)
        pnl_total = cash - 10.0
        avg_pnl_per_cycle = pnl_total / max(1, cycles)

        print(f"\n[DAY {day}]")
        print(f"  • Completed Cycles:      {cycles}")
        print(f"  • Total Volume:          ${vol:.2f}")
        print(f"  • Average Volume/Cycle:  ${avg_vol_per_cycle:.2f}")
        print(f"  • Ending Cash:           ${cash:.2f} (Starting: $10.00)")
        print(f"  • Net PnL:               ${pnl_total:+.2f}")
        print(f"  • Avg PnL per Cycle:     ${avg_pnl_per_cycle:+.3f}")
        print(f"  • Finished 100% Flat:    {res['finished_flat']}")
        print(f"  • Max Drawdown:          ${res['max_drawdown']:.2f}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    run_evaluation(5)
