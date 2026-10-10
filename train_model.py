#!/usr/bin/env python3
"""Train & Calibrate Predictive Model and Optimize Strategy on Real BTC Data.

Uses 3,000 real 1-minute BTC bars (50 hours / 600 real 5-minute prediction market rounds) to:
1. Fit an empirical probability model P(YES | gap, tau, momentum).
2. Measure statistical accuracy and Brier calibration score.
3. Optimize execution thresholds (gap, take-profit, stop-loss, hold duration)
   to maximize final settled cash and volume qualification under Builderr rules.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x))))


def train_probability_model(candles: list[dict[str, float]]) -> tuple[float, float, float]:
    """Train logistic regression model on real 5-minute market episodes."""
    print("=" * 70)
    print("STEP 1: TRAINING EMPIRICAL PROBABILITY MODEL ON REAL BTC EPISODES")
    print("=" * 70)

    dataset: list[tuple[float, float, int]] = []
    num_episodes = len(candles) // 5

    for ep in range(num_episodes):
        ep_bars = candles[ep * 5 : (ep + 1) * 5]
        s0 = ep_bars[0]["open"]
        s_final = ep_bars[-1]["close"]
        target_y = 1 if s_final >= s0 else 0

        # Minutes 1, 2, 3, 4 (t = 240, 180, 120, 60 seconds left)
        for m in range(1, 5):
            sm = ep_bars[m]["close"]
            sm_prev = ep_bars[m - 1]["close"]
            gap = sm - s0
            momentum = sm - sm_prev
            tau = (5 - m)  # remaining minutes

            # Normalized features
            feat_gap = gap / math.sqrt(tau)
            feat_mom = momentum / math.sqrt(tau)
            dataset.append((feat_gap, feat_mom, target_y))

    print(f"Total labeled decision points extracted: {len(dataset)}")

    # Train logistic regression via gradient descent
    # P(Y=1) = sigmoid(b0 + b1 * feat_gap + b2 * feat_mom)
    b0, b1, b2 = 0.0, 0.02, 0.01
    lr = 0.001
    epochs = 400

    for epoch in range(epochs):
        grad0, grad1, grad2 = 0.0, 0.0, 0.0
        for feat_gap, feat_mom, y in dataset:
            z = b0 + b1 * feat_gap + b2 * feat_mom
            p = sigmoid(z)
            err = p - y
            grad0 += err
            grad1 += err * feat_gap
            grad2 += err * feat_mom

        n = len(dataset)
        b0 -= lr * (grad0 / n)
        b1 -= lr * (grad1 / n)
        b2 -= lr * (grad2 / n)

    # Evaluate trained model
    correct = 0
    brier_score = 0.0
    for feat_gap, feat_mom, y in dataset:
        p = sigmoid(b0 + b1 * feat_gap + b2 * feat_mom)
        pred = 1 if p >= 0.5 else 0
        if pred == y:
            correct += 1
        brier_score += (p - y) ** 2

    acc = (correct / len(dataset)) * 100.0
    avg_brier = brier_score / len(dataset)

    print(f"Training Complete:")
    print(f"  • Learned Weights: b0 = {b0:.5f}, b1 (Gap/sqrt(tau)) = {b1:.5f}, b2 (Mom/sqrt(tau)) = {b2:.5f}")
    print(f"  • Directional Accuracy: {acc:.2f}%")
    print(f"  • Brier Calibration Error: {avg_brier:.4f} (lower is better, <0.20 is strong)")

    return b0, b1, b2


def simulate_real_markets(
    candles: list[dict[str, float]],
    b0: float,
    b1: float,
    b2: float,
    min_gap: float,
    take_profit: float,
    stop_loss: float,
    max_hold_time: float,
) -> dict[str, float]:
    """Replays real market episodes under Builderr contract rules."""
    cash = 10.0
    eligible_volume = 0.0
    cycles = 0
    num_episodes = len(candles) // 5

    def fees(p: float, s: float) -> float:
        protocol = 0.07 * s * p * (1.0 - p)
        overlay = s * p * 0.01
        return round(protocol, 5) + round(overlay, 5)

    position = None
    rng = random.Random(42)

    for ep in range(num_episodes):
        ep_bars = candles[ep * 5 : (ep + 1) * 5]
        s0 = ep_bars[0]["open"]

        for m in range(1, 5):
            sm = ep_bars[m]["close"]
            sm_prev = ep_bars[m - 1]["close"]
            gap = sm - s0
            mom = sm - sm_prev
            tau = (5 - m)
            seconds_to_close = tau * 60.0

            # Dynamic fair probability from trained model
            z = b0 + b1 * (gap / math.sqrt(tau)) + b2 * (mom / math.sqrt(tau))
            prob_yes = sigmoid(z)
            prob_no = 1.0 - prob_yes

            spread = 0.02
            half = spread / 2.0
            yes_ask = min(0.99, max(0.01, round(prob_yes + half, 2)))
            yes_bid = min(yes_ask - 0.01, max(0.01, round(prob_yes - half, 2)))
            no_ask = min(0.99, max(0.01, round(prob_no + half, 2)))
            no_bid = min(no_ask - 0.01, max(0.01, round(prob_no - half, 2)))

            # If holding, check exit logic
            if position is not None:
                side = position["outcome"]
                shares = position["shares"]
                cur_bid = yes_bid if side == "YES" else no_bid
                sell_gross = shares * cur_bid
                sell_fee = fees(cur_bid, shares)
                net_sell = sell_gross - sell_fee
                paid = position["paid"]
                cycle_pnl = net_sell - paid

                # Exit conditions:
                should_exit = False
                if cycle_pnl >= (take_profit * shares) - 1e-9:
                    should_exit = True
                elif cycle_pnl <= -(stop_loss * shares) + 1e-9:
                    should_exit = True
                elif (m - position["entry_m"]) * 60.0 >= max_hold_time:
                    should_exit = True
                elif seconds_to_close <= 60.0:
                    should_exit = True

                if should_exit:
                    cash += net_sell
                    eligible_volume += position["buy_gross"] + sell_gross
                    cycles += 1
                    position = None

            # If flat, check entry logic
            if position is None and seconds_to_close >= 60.0:
                if abs(gap) >= min_gap:
                    side = "YES" if gap > 0 else "NO"
                    ask_price = yes_ask if side == "YES" else no_ask

                    # Entry price band gate
                    if 0.30 <= ask_price <= 0.74:
                        # Adaptive sizing
                        alloc = 0.92 if cash >= 7.0 else (0.75 if cash >= 4.5 else 0.55)
                        spend = min(4.88, cash * alloc)
                        # Estimate shares
                        fee_per_s = (0.07 * ask_price * (1.0 - ask_price)) + (ask_price * 0.01)
                        shares = math.floor(spend / (ask_price + fee_per_s))

                        if shares >= 5:
                            buy_gross = shares * ask_price
                            buy_fee = fees(ask_price, shares)
                            total_spent = buy_gross + buy_fee
                            if total_spent <= cash:
                                cash -= total_spent
                                position = {
                                    "outcome": side,
                                    "shares": shares,
                                    "paid": total_spent,
                                    "buy_gross": buy_gross,
                                    "entry_m": m,
                                }

        # End of 5m round: if still holding, force flat (simulate early sweep)
        if position is not None:
            side = position["outcome"]
            shares = position["shares"]
            cur_bid = yes_bid if side == "YES" else no_bid
            net_sell = (shares * cur_bid) - fees(cur_bid, shares)
            cash += net_sell
            eligible_volume += position["buy_gross"] + (shares * cur_bid)
            cycles += 1
            position = None

    return {
        "final_cash": cash,
        "eligible_volume": eligible_volume,
        "cycles": cycles,
    }


def optimize_hyperparameters(candles: list[dict[str, float]], b0: float, b1: float, b2: float) -> dict[str, float]:
    print("\n" + "=" * 70)
    print("STEP 2: GRID-SEARCH HYPERPARAMETER OPTIMIZATION ON REAL 600 MARKETS")
    print("=" * 70)

    gap_candidates = [15.0, 20.0, 25.0]
    tp_candidates = [0.00, 0.008, 0.015]
    sl_candidates = [0.08, 0.12, 0.15]
    hold_candidates = [60.0, 120.0]

    best_score = -1e9
    best_params = {}

    total_runs = len(gap_candidates) * len(tp_candidates) * len(sl_candidates) * len(hold_candidates)
    run_idx = 0

    for gap in gap_candidates:
        for tp in tp_candidates:
            for sl in sl_candidates:
                for hold in hold_candidates:
                    run_idx += 1
                    res = simulate_real_markets(
                        candles, b0, b1, b2, min_gap=gap, take_profit=tp, stop_loss=sl, max_hold_time=hold
                    )
                    # Objective: Maximize cash while generating strong volume
                    # Score = final_cash + (volume / 200.0)
                    score = res["final_cash"] * 10.0 + (res["eligible_volume"] / 50.0)
                    if score > best_score and res["cycles"] >= 20:
                        best_score = score
                        best_params = {
                            "min_gap": gap,
                            "take_profit": tp,
                            "stop_loss": sl,
                            "max_hold": hold,
                            "cash": res["final_cash"],
                            "volume": res["eligible_volume"],
                            "cycles": res["cycles"],
                        }

    print("\nOptimization Complete! Global Optimal Configuration:")
    print(f"  • Best Min Gap:          ${best_params['min_gap']:.1f}")
    print(f"  • Best Take Profit:      ${best_params['take_profit']:.3f}/share")
    print(f"  • Best Stop Loss:        ${best_params['stop_loss']:.3f}/share")
    print(f"  • Best Max Hold Time:    {best_params['max_hold']:.0f}s")
    print(f"  • Backtested Cash:       ${best_params['cash']:.2f}")
    print(f"  • Backtested Volume:     ${best_params['volume']:.2f}")
    print(f"  • Completed Cycles:      {best_params['cycles']}")
    print("=" * 70)

    return best_params


def main() -> None:
    candles = json.loads(Path("data_btc_historical.json").read_text())
    b0, b1, b2 = train_probability_model(candles)
    best_params = optimize_hyperparameters(candles, b0, b1, b2)

    # Save trained model weights and optimal config
    trained_artifacts = {
        "model_weights": {"b0": b0, "b1": b1, "b2": b2},
        "optimal_hyperparameters": best_params,
    }
    Path("trained_model_config.json").write_text(json.dumps(trained_artifacts, indent=2))
    print("\nSaved calibrated model weights to trained_model_config.json")


if __name__ == "__main__":
    main()
