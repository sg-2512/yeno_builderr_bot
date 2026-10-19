#!/usr/bin/env python3
"""YeNo Autonomous Self-Tuning & Walk-Forward Optimization Engine.

Systematically searches, verifies, and self-tunes bot hyperparameters
using empirical walk-forward testing on historical Bitcoin market data.

Key Architecture:
- Walk-Forward Split: In-Sample (Train) vs. Out-of-Sample (Validation).
- Binary Qualification Gate: Rejects any candidate with < 100% qualification rate.
- Risk-Adjusted Fitness: Maximizes settled cash while penalizing drawdown and variance.
- Atomic Update: Only updates bot_hyperparameters.json when candidate strictly beats champion out-of-sample.
- Full Audit Trail: Appends optimization history to evolution_log.json.
- Zero External Dependencies: Pure Python Standard Library.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

from harness_real_bot_evaluation import run_real_day_evaluation
from yeno_production_bot import ProductionTradingBot

CONFIG_PATH = Path("bot_hyperparameters.json")
BACKUP_PATH = Path("bot_hyperparameters.backup.json")
LOG_PATH = Path("evolution_log.json")

# Hyperparameter search space specification (min, max, step, type)
PARAM_SPEC: dict[str, dict[str, Any]] = {
    "base_min_gap": {"min": 8.0, "max": 20.0, "step": 1.0, "type": "float"},
    "vol_scale_mult": {"min": 1.8, "max": 3.5, "step": 0.1, "type": "float"},
    "take_profit": {"min": 0.015, "max": 0.035, "step": 0.0025, "type": "float"},
    "mid_hold_profit": {"min": 0.005, "max": 0.015, "step": 0.0025, "type": "float"},
    "pre_expiry_harvest": {"min": -0.045, "max": -0.015, "step": 0.005, "type": "float"},
    "stop_loss": {"min": 0.070, "max": 0.140, "step": 0.010, "type": "float"},
    "forced_exit_sec": {"min": 48.0, "max": 54.0, "step": 1.0, "type": "float"},
    "obi_threshold": {"min": -0.45, "max": -0.15, "step": 0.05, "type": "float"},
    "tier2_budget_pct": {"min": 0.30, "max": 0.50, "step": 0.05, "type": "float"},
}


def load_candles(day_idx: int) -> list[dict[str, float]]:
    """Load 1440 1-minute candles for day `day_idx` (1 to 7)."""
    p = Path(f"data_24h_day{day_idx}.json")
    if not p.exists():
        raise FileNotFoundError(f"Dataset {p} not found.")
    return json.loads(p.read_text(encoding="utf-8"))


def load_config() -> dict[str, Any]:
    """Load active configuration from bot_hyperparameters.json."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config file {CONFIG_PATH} not found.")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(cfg: dict[str, Any]) -> None:
    """Atomically save configuration to bot_hyperparameters.json."""
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def backup_config() -> None:
    """Create a backup of bot_hyperparameters.json if backup does not already exist."""
    if CONFIG_PATH.exists() and not BACKUP_PATH.exists():
        BACKUP_PATH.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")


def evaluate_dataset_subset(
    days: list[int],
    cached_data: dict[int, list[dict[str, float]]],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Run real bot evaluation across specified days with given config.
    
    Returns aggregated metrics:
    - qual_rate: fraction of days qualified (1.0 = 100%)
    - mean_cash: average cash at qualification/crossing
    - min_cash: lowest cash across days
    - max_drawdown: worst peak-to-trough drawdown across all days
    - cash_std: standard deviation of cash across days
    - fitness: composite risk-adjusted score
    """
    results: list[dict[str, Any]] = []
    for day in days:
        candles = cached_data[day]
        res = run_real_day_evaluation(candles, config=cfg)
        results.append(res)

    n = len(results)
    qual_count = sum(1 for r in results if r["qualified"])
    qual_rate = qual_count / n

    cashes = [r["final_cash"] for r in results]
    mean_cash = sum(cashes) / n
    min_cash = min(cashes)
    var_cash = sum((c - mean_cash) ** 2 for c in cashes) / n
    cash_std = math.sqrt(var_cash)
    worst_drawdown = max(r["max_drawdown"] for r in results)

    # Fitness Function Formulation:
    # 1. Binary Qualification Barrier: If any day fails, impose massive penalty
    if qual_rate < 1.0:
        avg_vol = sum(r["final_volume"] for r in results) / n
        fitness = -10000.0 + avg_vol
    else:
        # 2. Risk-Adjusted Wealth Objective:
        # Maximize mean cash, reward cash floor (min_cash), penalize drawdown and variance
        fitness = (
            mean_cash * 1.0
            + min_cash * 0.25
            - (worst_drawdown * 0.40)
            - (cash_std * 0.20)
        )

    return {
        "qual_rate": qual_rate,
        "qual_count": qual_count,
        "total_days": n,
        "mean_cash": round(mean_cash, 2),
        "min_cash": round(min_cash, 2),
        "max_drawdown": round(worst_drawdown, 2),
        "cash_std": round(cash_std, 2),
        "fitness": round(fitness, 3),
        "details": results,
    }


def mutate_config(
    parent: dict[str, Any],
    mutation_rate: float = 0.35,
    temperature: float = 1.0,
) -> tuple[dict[str, Any], list[str]]:
    """Generate a valid mutated configuration using bounded random perturbations."""
    child = copy.deepcopy(parent)
    mutated_keys: list[str] = []

    # Choose at least 1 parameter to mutate
    params = list(PARAM_SPEC.keys())
    # Ensure at least 1 mutation
    k = max(1, sum(1 for _ in params if random.random() < mutation_rate))
    chosen_params = random.sample(params, k)

    for p in chosen_params:
        spec = PARAM_SPEC[p]
        p_min = spec["min"]
        p_max = spec["max"]
        step = spec["step"]

        cur_val = child.get(p, (p_min + p_max) / 2.0)
        # Determine number of discrete steps to mutate: -3, -2, -1, 1, 2, 3
        max_steps = max(1, int(round(3 * temperature)))
        delta_steps = random.choice([i for i in range(-max_steps, max_steps + 1) if i != 0])
        new_val = cur_val + (delta_steps * step)

        # Enforce bounds
        new_val = max(p_min, min(p_max, new_val))
        # Round to appropriate precision
        decimals = 4 if step < 0.01 else (3 if step < 0.1 else (2 if step < 1.0 else 1))
        new_val = round(new_val, decimals)

        if new_val != cur_val:
            child[p] = new_val
            mutated_keys.append(f"{p}: {cur_val} -> {new_val}")

    return child, mutated_keys


def log_event(entry: dict[str, Any]) -> None:
    """Append event to evolution_log.json."""
    logs: list[dict[str, Any]] = []
    if LOG_PATH.exists():
        try:
            logs = json.loads(LOG_PATH.read_text(encoding="utf-8"))
        except Exception:
            logs = []
    logs.append(entry)
    LOG_PATH.write_text(json.dumps(logs, indent=2) + "\n", encoding="utf-8")


def run_evaluation_suite(cfg: dict[str, Any], days: list[int] | None = None) -> None:
    """Print full detailed diagnostic evaluation of a configuration across days."""
    if days is None:
        days = list(range(1, 8))

    print(f"\nEvaluating configuration across {len(days)} 24-hour datasets (Days {days})...")
    cached = {d: load_candles(d) for d in days}
    start_t = time.perf_counter()
    res = evaluate_dataset_subset(days, cached, cfg)
    elapsed = time.perf_counter() - start_t

    print("=" * 78)
    print(f"{'DAY':<8} | {'STATUS':<12} | {'QUAL ROUND':<12} | {'FINAL CASH':<12} | {'VOLUME':<12} | {'MAX DD':<10}")
    print("-" * 78)
    for i, d in enumerate(days):
        sub = res["details"][i]
        status = "QUALIFIED" if sub["qualified"] else "FAILED"
        q_round = f"Round {sub['markets_to_qualify']}" if sub["qualified"] else "N/A"
        cash_str = f"${sub['final_cash']:.2f}"
        vol_str = f"${sub['final_volume']:.2f}"
        dd_str = f"${sub['max_drawdown']:.2f}"
        print(f"Day {d:<4} | {status:<12} | {q_round:<12} | {cash_str:<12} | {vol_str:<12} | {dd_str:<10}")
    print("=" * 78)
    print(f"Summary: Qualification Rate = {res['qual_count']}/{res['total_days']} ({res['qual_rate']*100:.1f}%)")
    print(f"Mean Settled Cash: ${res['mean_cash']:.2f} | Min Cash: ${res['min_cash']:.2f} | Max Drawdown: ${res['max_drawdown']:.2f}")
    print(f"Risk-Adjusted Fitness: {res['fitness']:.3f} | Total Execution Time: {elapsed:.2f}s ({elapsed/len(days)*1000:.1f} ms/day)")
    print("=" * 78 + "\n")


def evaluate_historical_file(
    file_path: Path | str,
    max_days: int = 7,
    cfg: dict[str, Any] | None = None,
) -> None:
    """Evaluate active bot configuration across days inside an arbitrary historical JSON dataset."""
    path = Path(file_path)
    if not path.exists():
        print(f"Error: file {path} not found.")
        return
    if cfg is None:
        cfg = load_config()

    print(f"\nEvaluating active configuration on historical dataset: {path.name} (Up to {max_days} days)...")
    all_candles = json.loads(path.read_text(encoding="utf-8"))
    available_days = len(all_candles) // 1440
    days_to_run = min(max_days, available_days)

    if days_to_run == 0:
        print(f"Dataset has {len(all_candles)} candles, need at least 1440 for a full 24h day.")
        return

    results = []
    start_t = time.perf_counter()
    for d in range(days_to_run):
        day_candles = all_candles[d * 1440 : (d + 1) * 1440]
        res = run_real_day_evaluation(day_candles, config=cfg)
        s_open = day_candles[0]["open"]
        s_close = day_candles[-1]["close"]
        res["day_idx"] = d + 1
        res["btc_open"] = s_open
        res["btc_close"] = s_close
        results.append(res)
    elapsed = time.perf_counter() - start_t

    print("=" * 86)
    print(f"{'DAY':<6} | {'BTC OPEN':<10} | {'BTC CLOSE':<10} | {'STATUS':<12} | {'QUAL ROUND':<12} | {'CASH':<10} | {'VOL':<10} | {'MAX DD':<8}")
    print("-" * 86)
    for r in results:
        status = "QUALIFIED" if r["qualified"] else "FAILED"
        q_round = f"Round {r['markets_to_qualify']}" if r["qualified"] else "N/A"
        print(f"Day {r['day_idx']:<2} | ${r['btc_open']:<9.1f} | ${r['btc_close']:<9.1f} | {status:<12} | {q_round:<12} | ${r['final_cash']:<9.2f} | ${r['final_volume']:<9.2f} | ${r['max_drawdown']:<7.2f}")
    print("=" * 86)
    qual_count = sum(1 for r in results if r["qualified"])
    mean_cash = sum(r["final_cash"] for r in results) / len(results)
    min_cash = min(r["final_cash"] for r in results)
    worst_dd = max(r["max_drawdown"] for r in results)
    print(f"Historical Epoch Summary ({path.name}): Qualification Rate = {qual_count}/{len(results)} ({qual_count/len(results)*100:.1f}%)")
    print(f"Mean Settled Cash: ${mean_cash:.2f} | Min Cash: ${min_cash:.2f} | Worst Drawdown: ${worst_dd:.2f}")
    print(f"Total Execution Time: {elapsed:.2f}s ({elapsed/len(results)*1000:.1f} ms/day)")
    print("=" * 86 + "\n")


def run_self_tuning(
    iterations: int = 10,
    seed: int | None = 42,
    train_days: list[int] | None = None,
    val_days: list[int] | None = None,
) -> None:
    """Run autonomous walk-forward hyperparameter optimization."""
    if seed is not None:
        random.seed(seed)

    if train_days is None:
        train_days = [1, 2, 3, 4]
    if val_days is None:
        val_days = [5, 6, 7]

    all_days = sorted(list(set(train_days + val_days)))
    print("=" * 78)
    print("YENO AUTONOMOUS WALK-FORWARD OPTIMIZATION ENGINE")
    print("=" * 78)
    print(f"Target Iterations: {iterations}")
    print(f"Train Partition:   Days {train_days} (In-Sample)")
    print(f"Val Partition:     Days {val_days} (Out-of-Sample)")
    print("Loading historical candlestick datasets into memory...")

    cached_data = {d: load_candles(d) for d in all_days}
    print("All datasets loaded.")

    backup_config()
    champion_cfg = load_config()

    print("\n[STEP 1] Baselining Active Champion Configuration...")
    champ_train = evaluate_dataset_subset(train_days, cached_data, champion_cfg)
    champ_val = evaluate_dataset_subset(val_days, cached_data, champion_cfg)

    print(f"Champion Train Fitness: {champ_train['fitness']:.3f} (Mean Cash: ${champ_train['mean_cash']:.2f}, Qual: {champ_train['qual_count']}/{len(train_days)})")
    print(f"Champion Val Fitness:   {champ_val['fitness']:.3f} (Mean Cash: ${champ_val['mean_cash']:.2f}, Qual: {champ_val['qual_count']}/{len(val_days)})")

    best_val_fitness = champ_val["fitness"]
    best_train_fitness = champ_train["fitness"]
    improvements = 0

    print("\n[STEP 2] Starting Evolutionary Perturbation Search Loop...\n")
    print(f"{'GEN':<5} | {'MUTATIONS':<32} | {'TRAIN FIT':<10} | {'VAL FIT':<10} | {'OUTCOME':<12}")
    print("-" * 78)

    for it in range(1, iterations + 1):
        temp = max(0.4, 1.0 - (it / (iterations * 1.5)))
        candidate_cfg, mut_descriptions = mutate_config(champion_cfg, temperature=temp)
        mut_str = "; ".join(mut_descriptions)[:32] if mut_descriptions else "None"

        # 1. Evaluate on Train partition (In-Sample)
        cand_train = evaluate_dataset_subset(train_days, cached_data, candidate_cfg)

        # Gate 1: Must qualify 100% on train days
        if cand_train["qual_rate"] < 1.0:
            print(f"{it:<5} | {mut_str:<32} | {cand_train['fitness']:<10.2f} | {'---':<10} | DISQUALIFIED")
            continue

        # Gate 2: Train fitness must exceed or match champion
        if cand_train["fitness"] <= best_train_fitness - 0.5:
            print(f"{it:<5} | {mut_str:<32} | {cand_train['fitness']:<10.2f} | {'---':<10} | TRAIN-REJECT")
            continue

        # 2. Evaluate on Validation partition (Out-of-Sample)
        cand_val = evaluate_dataset_subset(val_days, cached_data, candidate_cfg)

        # Gate 3: Must qualify 100% on validation days
        if cand_val["qual_rate"] < 1.0:
            print(f"{it:<5} | {mut_str:<32} | {cand_train['fitness']:<10.2f} | {cand_val['fitness']:<10.2f} | VAL-DISQUAL")
            continue

        # Gate 4: Out-of-Sample generalization test
        # Accept if validation fitness strictly beats current champion val fitness
        if cand_val["fitness"] > best_val_fitness:
            old_val_fit = best_val_fitness
            best_val_fitness = cand_val["fitness"]
            best_train_fitness = cand_train["fitness"]
            champion_cfg = candidate_cfg
            improvements += 1

            save_config(champion_cfg)
            print(f"{it:<5} | {mut_str:<32} | {cand_train['fitness']:<10.2f} | {cand_val['fitness']:<10.2f} | *** NEW BEST ***")

            log_entry = {
                "iteration": it,
                "timestamp": time.time(),
                "mutations": mut_descriptions,
                "train_fitness": cand_train["fitness"],
                "val_fitness": cand_val["fitness"],
                "old_val_fitness": old_val_fit,
                "train_mean_cash": cand_train["mean_cash"],
                "val_mean_cash": cand_val["mean_cash"],
                "config": champion_cfg,
            }
            log_event(log_entry)
        else:
            print(f"{it:<5} | {mut_str:<32} | {cand_train['fitness']:<10.2f} | {cand_val['fitness']:<10.2f} | VAL-REJECT")

    print("=" * 78)
    print(f"OPTIMIZATION RUN COMPLETE: {improvements} improvements recorded.")
    print("=" * 78)

    print("\n[STEP 3] Final Verification of Active Champion across ALL 7 Days...")
    run_evaluation_suite(champion_cfg, days=all_days)


def main() -> None:
    parser = argparse.ArgumentParser(description="YeNo Autonomous Self-Tuning Engine")
    parser.add_argument("--iterations", type=int, default=10, help="Number of optimization iterations")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument("--eval-active", action="store_true", help="Evaluate current active config on benchmark days")
    parser.add_argument("--eval-file", type=str, default=None, help="Evaluate active config on arbitrary historical JSON dataset")
    parser.add_argument("--days", type=int, default=7, help="Number of 24h days to evaluate when using --eval-file")
    parser.add_argument("--reset", action="store_true", help="Restore config from backup")

    args = parser.parse_args()

    if args.reset:
        if BACKUP_PATH.exists():
            CONFIG_PATH.write_text(BACKUP_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"Restored {CONFIG_PATH} from {BACKUP_PATH}.")
        else:
            print("No backup file found to restore from.")
        return

    if args.eval_file:
        evaluate_historical_file(args.eval_file, max_days=args.days)
        return

    if args.eval_active:
        cfg = load_config()
        run_evaluation_suite(cfg)
        return

    run_self_tuning(iterations=args.iterations, seed=args.seed)


if __name__ == "__main__":
    main()
