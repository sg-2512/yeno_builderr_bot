#!/usr/bin/env python3
"""YeNo Production Trading Bot — Quantitative Edge & Profit Maximizer.

Engineered for the YeNo x Builderr Volume Bot Challenge:
- Target: $1,000 eligible completed-cycle volume in 24 hours.
- Starting cash: $10.00.
- Max BUY cash: <= $5.00 including all protocol and platform fees.
- Position limit: At most one position or pending BUY at a time.
- Clean Crossing: Halts cleanly at >= $1,000 volume with zero positions open.
- Survival & Cash Optimization: Maximize settled cash at crossing for Rank 1.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# ==============================================================================
# STRATEGY HYPERPARAMETERS
# ==============================================================================
MAX_BUY_CASH_CEILING = 4.88            # Safe buffer below $5.00 fee-inclusive cap
BUY_HEADROOM_PER_SHARE = 0.015         # Buffer against 250ms quote jumps (prevents sub-minOrderSize rejections)
MAX_HOLD_SECONDS = 90.0                # Max hold time to allow theta decay & trend to mature
CASH_UTILIZATION_RATIO = 0.95          # Max fraction of available cash to risk per BUY
TARGET_VOLUME_USD = 1000.0             # Official volume qualification target
MAX_SPREAD_USD = 0.03                  # Strict spread filter (reject wide spreads)
MAX_ENTRY_PRICE = 0.92                 # High-conviction ceiling: capture strong trends
MIN_ENTRY_PRICE = 0.48                 # Avoid low probability lotteries below 0.48; covers favorites up to 0.92
BASE_MIN_GAP_USD = 12.0                # Real-data calibrated strike gap threshold
BTC_SIGMA_PER_MIN_USD = 45.0           # Calibrated BTC 1-minute volatility
MIN_EDGE_MARGIN = 0.010                # Required mathematical probability edge above fees
MAX_CYCLES_PER_MARKET = 3              # Allow up to 3 trades per 5m market when opportunities appear
ENTRY_MAX_SECONDS_TO_CLOSE = 240.0     # Scan for entries once trend establishes (t <= 4 min)
ENTRY_MIN_SECONDS_TO_CLOSE = 60.0      # Cut off entries with <= 60s left to guarantee exit runway
FORCED_EXIT_SECONDS = 45.0             # Standard exit threshold before expiry decay
EMERGENCY_EXIT_SECONDS = 25.0          # Hard emergency exit: sweep bids before 15s freeze
REENTRY_COOLDOWN_SECONDS = 2.0         # Wait between trades to avoid whipsaw entries
MAX_REFERENCE_AGE_SECONDS = 1.5        # Max allowable staleness of BTC reference data
STOP_LOSS_PER_SHARE_USD = 0.07         # Disciplined loss limit per share
TAKE_PROFIT_PER_SHARE_USD = 0.025      # Lock in net profit (+2.5 cents/share after all fees)


def _normal_cdf(z: float) -> float:
    """Standard normal cumulative distribution function (Φ) via error function."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _parse_book_levels(rows: Any, *, reverse: bool) -> list[tuple[float, float]]:
    """Parse and sort L2 order book levels."""
    levels: list[tuple[float, float]] = []
    if isinstance(rows, list):
        for row in rows:
            try:
                price = float(row[0])
                size = float(row[1])
            except (IndexError, TypeError, ValueError):
                continue
            if 0.0 < price < 1.0 and size > 0:
                levels.append((price, size))
    return sorted(levels, reverse=reverse)


def _compute_micro_price(best_bid: float, best_ask: float, v_bid: float, v_ask: float) -> float:
    """Stoikov (2018) Volume-Weighted Micro-Price Estimator."""
    total = v_bid + v_ask
    if total <= 1e-9:
        return (best_bid + best_ask) / 2.0
    return (v_bid * best_ask + v_ask * best_bid) / total


def _compute_obi(parsed_bids: list[tuple[float, float]], parsed_asks: list[tuple[float, float]], depth: int = 2) -> float:
    """Cont, Kukanov & Stoikov (2014) Order Book Imbalance (OBI) metric."""
    v_bid = sum(qty for _, qty in parsed_bids[:depth])
    v_ask = sum(qty for _, qty in parsed_asks[:depth])
    total = v_bid + v_ask
    if total <= 1e-9:
        return 0.0
    return (v_bid - v_ask) / total


def _compute_ema(prices: list[float], period: int) -> float:
    """Compute Exponential Moving Average (EMA) over a price series."""
    if not prices:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    ema = prices[0]
    for p in prices[1:]:
        ema = alpha * p + (1.0 - alpha) * ema
    return ema


def _compute_rsi(prices: list[float]) -> float:
    """Compute Relative Strength Index (RSI) momentum indicator."""
    if len(prices) < 4:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses += abs(diff)
    total = gains + losses
    if total <= 1e-9:
        return 50.0
    return 100.0 * (gains / total)


def _compute_linear_slope(history: list[tuple[float, float]]) -> float:
    """Compute least-squares trend velocity (USD per second) from price history."""
    if len(history) < 3:
        return 0.0
    t_vals = [t for t, _ in history]
    p_vals = [p for _, p in history]
    t_mean = sum(t_vals) / len(t_vals)
    p_mean = sum(p_vals) / len(p_vals)
    denom = sum((t - t_mean) ** 2 for t in t_vals)
    if denom <= 1e-9:
        return 0.0
    numer = sum((t - t_mean) * (p - p_mean) for t, p in zip(t_vals, p_vals))
    return numer / denom


def _compute_fees(fills: list[tuple[float, float]]) -> float:
    """Compute exact two-layer fees: Polymarket dynamic crypto taker fee + YeNo 1% overlay."""
    gross = sum(price * shares for price, shares in fills)
    # Polymarket dynamic crypto taker fee: 0.07 * shares * price * (1 - price)
    protocol = sum(0.07 * shares * price * (1.0 - price) for price, shares in fills)
    # 5 decimal precision protocol fee + 1% YeNo overlay
    return round(protocol + 1e-12, 5) + round(gross * 0.01 + 1e-12, 5)


def _estimate_buy(asks: Any, shares: float) -> tuple[float, float] | None:
    """Simulate buying `shares` from asks. Returns (gross_cost, fees) or None if depth insufficient."""
    remaining = shares
    fills: list[tuple[float, float]] = []
    for price, available in _parse_book_levels(asks, reverse=False):
        fill_qty = min(remaining, available)
        fills.append((price, fill_qty))
        remaining -= fill_qty
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        return None
    gross = sum(price * qty for price, qty in fills)
    return gross, _compute_fees(fills)


def _estimate_sell(bids: Any, shares: float) -> tuple[float, float] | None:
    """Simulate selling `shares` to bids. Returns (gross_proceeds, fees) or None if depth insufficient."""
    remaining = shares
    fills: list[tuple[float, float]] = []
    for price, available in _parse_book_levels(bids, reverse=True):
        fill_qty = min(remaining, available)
        fills.append((price, fill_qty))
        remaining -= fill_qty
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        return None
    gross = sum(price * qty for price, qty in fills)
    return gross, _compute_fees(fills)


class ProductionTradingBot:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.market_id: str | None = None
        self.position_was_open: bool = False
        self.completed_cycles: int = 0
        self.last_flat_at: float = -math.inf
        self.last_buy_attempt_at: float = -math.inf
        self.qualified_and_flat: bool = False
        self.price_history: list[tuple[float, float]] = []
        self.exiting: bool = False
        self.position_entered_at: float = -math.inf
        self.consecutive_losses: int = 0
        self.cooldown_until: float = -math.inf
        self.pre_trade_cash_usd: float = 10.0
        self.session_start_time: float = -math.inf

    def decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Entry point for /decide. Thread-safe with latched emergency exit on error."""
        with self._lock:
            try:
                return self._decide_impl(observation)
            except Exception:
                # Jaswanth safety net: if holding a position and parsing fails, never get stranded
                try:
                    if isinstance(observation, dict) and observation.get("account", {}).get("position") is not None:
                        self.exiting = True
                        return {"action": "SELL"}
                except Exception:
                    pass
                return {"action": "HOLD"}

    def _decide_impl(self, observation: dict[str, Any]) -> dict[str, Any]:
        market = observation.get("market", {})
        account = observation.get("account", {})
        books = observation.get("books", {})
        rules = observation.get("rules", {})
        reference = observation.get("reference")
        
        now = float(observation.get("timestamp", 0.0))
        if self.session_start_time <= 0.0 and now > 0.0:
            self.session_start_time = now

        elapsed_hours = (now - self.session_start_time) / 3600.0 if self.session_start_time > 0.0 else 0.0
        expected_vol = (elapsed_hours / 24.0) * TARGET_VOLUME_USD

        # Continuously record BTC mid price into rolling 60s history on EVERY tick (even while holding positions)
        if isinstance(reference, dict) and "btcMidUsd" in reference:
            try:
                btc_mid_val = float(reference["btcMidUsd"])
                self.price_history.append((now, btc_mid_val))
                self.price_history = [(t_h, p_h) for t_h, p_h in self.price_history if now - t_h <= 60.0]
            except (ValueError, TypeError):
                pass

        seconds_to_close = float(market.get("secondsToClose", 0.0))
        market_id = str(market.get("id", ""))
        cash_usd = float(account.get("cashUsd", 0.0))
        eligible_vol = float(account.get("eligibleVolumeUsd", 0.0))
        position = account.get("position")

        # ----------------------------------------------------------------------
        # 1. Clean Crossing Lock Check
        # ----------------------------------------------------------------------
        # Once target volume is achieved and position is flat, permanently LOCK IN FLAT!
        if eligible_vol >= TARGET_VOLUME_USD and position is None:
            self.qualified_and_flat = True
            return {"action": "HOLD"}

        if self.qualified_and_flat and position is None:
            return {"action": "HOLD"}

        # ----------------------------------------------------------------------
        # 2. Market Lifecycle & State Transition
        # ----------------------------------------------------------------------
        if market_id != self.market_id:
            self.market_id = market_id
            self.position_was_open = False
            self.completed_cycles = 0
            self.last_flat_at = -math.inf
            self.last_buy_attempt_at = -math.inf
            # Preserve recent 60s price history across markets so momentum/EMA filters aren't skipped on tick 1
            self.price_history = [(t_h, p_h) for t_h, p_h in self.price_history if now - t_h <= 60.0]
            self.exiting = False
            self.position_entered_at = -math.inf

        # Cycle transition tracking with accurate pre-trade cash accounting
        if position is None:
            self.exiting = False
            self.position_entered_at = -math.inf
            if self.position_was_open:
                self.completed_cycles += 1
                self.last_flat_at = now
                # Accurate cycle PnL: compare cash now with cash balance BEFORE trade was entered
                cycle_pnl = cash_usd - self.pre_trade_cash_usd
                if cycle_pnl > 1e-4:
                    self.consecutive_losses = 0
                    self.cooldown_until = -math.inf
                elif cycle_pnl < -1e-4:
                    self.consecutive_losses += 1
                    # Cooldown on 2 consecutive losses: 10 minutes to prevent whipsaw clustering
                    if self.consecutive_losses >= 2:
                        self.cooldown_until = now + 600.0
                self.pre_trade_cash_usd = cash_usd
            else:
                self.pre_trade_cash_usd = cash_usd
        else:
            if not self.position_was_open:
                self.position_entered_at = now
                # If entering position, ensure pre_trade_cash reflects pre-buy cash (cash + buy cost)
                pos_cost = float(position.get("buy_gross_usd", 0.0)) + float(position.get("buy_fees_usd", 0.0))
                if pos_cost > 0:
                    self.pre_trade_cash_usd = cash_usd + pos_cost
        self.position_was_open = position is not None

        # ----------------------------------------------------------------------
        # 3. Position Management & Exit Engine (When holding a position)
        # ----------------------------------------------------------------------
        if position is not None:
            side = str(position.get("outcome", "YES"))
            shares = float(position.get("shares", 0.0))
            if shares <= 1e-9:
                return {"action": "HOLD"}

            # Learning 1 (Jaswanth): Latched Exit — once an exit triggers, keep returning SELL until flat!
            if self.exiting:
                return {"action": "SELL"}

            # If target volume was reached while holding, prioritize immediate exit!
            if eligible_vol >= TARGET_VOLUME_USD:
                self.exiting = True
                return {"action": "SELL"}

            # Emergency Time Exit: Force SELL before the 15-second pre-expiry lock!
            if seconds_to_close <= EMERGENCY_EXIT_SECONDS:
                self.exiting = True
                return {"action": "SELL"}

            # Learning 3 (RowletCC): Reverse Signal Cut — dump immediately if BTC gap flips violently
            if isinstance(reference, dict) and not bool(reference.get("targetProvisional", True)):
                cur_gap = float(reference.get("btcMidUsd", 0)) - float(reference.get("openingTargetUsd", 0))
                if (side == "YES" and cur_gap < -BASE_MIN_GAP_USD) or (side == "NO" and cur_gap > BASE_MIN_GAP_USD):
                    self.exiting = True
                    return {"action": "SELL"}

            # Learning 4 (RowletCC): Max Hold Time Cutoff — don't hold past 30s to avoid chopping
            if self.position_entered_at > 0 and (now - self.position_entered_at) >= MAX_HOLD_SECONDS:
                self.exiting = True
                return {"action": "SELL"}

            outcome_book = books.get(side, {})
            bids = outcome_book.get("bids", [])

            executable = _estimate_sell(bids, shares)
            if executable is not None:
                sell_gross, sell_fees = executable
                paid = float(position.get("buy_gross_usd", 0.0)) + float(position.get("buy_fees_usd", 0.0))
                already_received = float(position.get("sell_gross_usd", 0.0)) - float(position.get("sell_fees_usd", 0.0))
                full_cycle_pnl = already_received + sell_gross - sell_fees - paid

                # Avellaneda-Stoikov Dynamic Reservation Price Exit Engine:
                # - Early holding (t > 85s): Target full profit (+0.02 / share)
                # - Mid holding (60s < t <= 85s): Take early profit (+0.01 / share)
                # - Approaching expiry (45s < t <= 60s): Breakeven exit (>= 0.00)
                dynamic_tp = (
                    TAKE_PROFIT_PER_SHARE_USD
                    if seconds_to_close > 85.0
                    else (0.01 if seconds_to_close > 60.0 else 0.00)
                )

                if full_cycle_pnl >= (dynamic_tp * shares) - 1e-9:
                    self.exiting = True
                    return {"action": "SELL"}

                # Stop-Loss: Protect capital against severe reversals
                if full_cycle_pnl <= -(STOP_LOSS_PER_SHARE_USD * shares) + 1e-9:
                    self.exiting = True
                    return {"action": "SELL"}

                # Forced Time Exit: Guarantee flat liquidation before expiry lock
                if seconds_to_close <= FORCED_EXIT_SECONDS:
                    self.exiting = True
                    return {"action": "SELL"}
            else:
                if seconds_to_close <= 40.0:
                    self.exiting = True
                    return {"action": "SELL"}

            return {"action": "HOLD"}

        # ----------------------------------------------------------------------
        # 4. Entry Filters (When Flat)
        # ----------------------------------------------------------------------
        # Safe entry window: Evaluator blocks entries <= 15s; we stop at 50s for exit safety
        if not (ENTRY_MIN_SECONDS_TO_CLOSE < seconds_to_close <= ENTRY_MAX_SECONDS_TO_CLOSE):
            return {"action": "HOLD"}

        # Circuit Breaker: Enforce cooldown if triggered by 3 consecutive losses
        if now < self.cooldown_until:
            return {"action": "HOLD"}

        if self.completed_cycles >= MAX_CYCLES_PER_MARKET:
            return {"action": "HOLD"}

        if now - self.last_flat_at < REENTRY_COOLDOWN_SECONDS:
            return {"action": "HOLD"}

        if now - self.last_buy_attempt_at < REENTRY_COOLDOWN_SECONDS:
            return {"action": "HOLD"}

        # Reference validation (Check staleness and provisional flags)
        if not isinstance(reference, dict) or bool(reference.get("targetProvisional", True)):
            return {"action": "HOLD"}

        ref_observed_at = float(reference.get("observedAt", -math.inf))
        if now - ref_observed_at > MAX_REFERENCE_AGE_SECONDS or ref_observed_at > now + 1e-9:
            return {"action": "HOLD"}

        target_observed_at = float(reference.get("targetObservedAt", math.inf))
        if target_observed_at > now + 1e-9:
            return {"action": "HOLD"}

        # ----------------------------------------------------------------------
        # 5. Direction & Mathematical Edge Calculation (The Quant Engine)
        # ----------------------------------------------------------------------
        btc_mid = float(reference["btcMidUsd"])
        target_usd = float(reference["openingTargetUsd"])
        gap = btc_mid - target_usd

        # Calculate remaining time in minutes
        tau_min = max(0.5, seconds_to_close / 60.0)

        # Price Scale Factor relative to baseline $80,000 BTC benchmark (scales smoothly down to $3k BTC)
        p_scale = max(0.035, min(1.5, btc_mid / 80000.0))

        # Real-Time Volatility Adaptation: estimate realized volatility from rolling price history
        if len(self.price_history) >= 4:
            history_prices = [p for _, p in self.price_history]
            mean_p = sum(history_prices) / len(history_prices)
            var_p = sum((p - mean_p) ** 2 for p in history_prices) / len(history_prices)
            realized_std = math.sqrt(max(0.0, var_p))
            dynamic_sigma = max(28.0 * p_scale, min(120.0 * p_scale, realized_std * 2.5))
        else:
            dynamic_sigma = 45.0 * p_scale

        # Dynamic Volatility-Scaled Spike Shield: Reject black swan flash wicks without discarding normal volatility
        spike_limit = max(120.0 * p_scale, min(250.0 * p_scale, dynamic_sigma * 2.5))
        if len(self.price_history) >= 2:
            hist_prices = [p for _, p in self.price_history]
            if max(hist_prices) - min(hist_prices) >= spike_limit:
                return {"action": "HOLD"}

        # Volatility-Scaled Base Regime Gap (Disciplined: never degenerates into noise gambling)
        min_required_gap = max(12.0 * p_scale, min(28.0 * p_scale, dynamic_sigma * 0.22))
        if abs(gap) < min_required_gap:
            return {"action": "HOLD"}

        # Dynamic volatility over remaining time: sigma * sqrt(tau)
        vol_expected_usd = max(15.0, dynamic_sigma * math.sqrt(tau_min))

        # Normalized Z-Score of the BTC price relative to the strike target
        z = gap / vol_expected_usd
        prob_yes = _normal_cdf(z)
        prob_no = 1.0 - prob_yes

        # Horizon 1: Micro-Momentum (3 seconds)
        p_3s = [p_h for t_h, p_h in self.price_history if now - t_h >= 3.0]
        if p_3s:
            ref_3s = p_3s[-1]
            if gap > 0 and btc_mid < ref_3s - 10.0:
                return {"action": "HOLD"}
            if gap < 0 and btc_mid > ref_3s + 10.0:
                return {"action": "HOLD"}

        # Horizon 2: Tactical Momentum (15 seconds)
        p_15s = [p_h for t_h, p_h in self.price_history if now - t_h >= 15.0]
        if p_15s:
            ref_15s = p_15s[-1]
            if gap > 0 and btc_mid < ref_15s - 14.0:
                return {"action": "HOLD"}
            if gap < 0 and btc_mid > ref_15s + 14.0:
                return {"action": "HOLD"}

        # Horizon 3: Technical Trend Analysis (EMA Alignment, Linear Velocity & RSI Exhaustion)
        if len(self.price_history) >= 4:
            history_prices = [p for _, p in self.price_history]
            ema_fast = _compute_ema(history_prices, period=5)
            ema_slow = _compute_ema(history_prices, period=15)
            slope = _compute_linear_slope(self.price_history)
            ema_tol = 18.0 if (cash_usd >= 20.0 and elapsed_hours >= 16.5) else 7.0

            # Trend Direction Alignment:
            # When buying YES, price action must not be in a downward EMA cross or dumping velocity
            if gap > 0:
                if ema_fast < ema_slow - ema_tol or slope < -0.40:
                    return {"action": "HOLD"}
                # Overbought Exhaustion Guard: Avoid buying YES into an extreme parabolic top
                if len(history_prices) >= 8 and _compute_rsi(history_prices) > 92.0:
                    return {"action": "HOLD"}

            # When buying NO, price action must not be in an upward EMA cross or pumping velocity
            if gap < 0:
                if ema_fast > ema_slow + ema_tol or slope > 0.40:
                    return {"action": "HOLD"}
                # Oversold Bounce Guard: Avoid buying NO into an extreme capitulation bottom
                if len(history_prices) >= 8 and _compute_rsi(history_prices) < 8.0:
                    return {"action": "HOLD"}

        # Select direction with highest probability
        side = "YES" if gap > 0 else "NO"

        outcome_book = books.get(side, {})
        asks = outcome_book.get("asks", [])
        bids = outcome_book.get("bids", [])
        min_order_size = float(outcome_book.get("minOrderSize", 5.0))

        # Builderr Rule 05: Reject order books older than two seconds or future-stamped
        book_observed_at = outcome_book.get("observedAt", outcome_book.get("timestamp"))
        if book_observed_at is not None:
            try:
                b_t = float(book_observed_at)
                if now - b_t > 2.0 or b_t > now + 1e-9:
                    return {"action": "HOLD"}
            except (TypeError, ValueError):
                pass

        parsed_asks = _parse_book_levels(asks, reverse=False)
        parsed_bids = _parse_book_levels(bids, reverse=True)
        if not parsed_asks or not parsed_bids:
            return {"action": "HOLD"}

        best_ask = parsed_asks[0][0]
        best_bid = parsed_bids[0][0]

        # Book Consensus Agreement: Market book must favor our direction (bid >= 0.48)
        if best_bid < 0.48:
            return {"action": "HOLD"}

        # Cross-Book Parity Check: Verify complementary book agreement (reject contradictory books)
        other_side = "NO" if side == "YES" else "YES"
        other_book = books.get(other_side, {})
        other_bids = other_book.get("bids", [])
        parsed_other_bids = _parse_book_levels(other_bids, reverse=True)
        if parsed_other_bids:
            other_best_bid = parsed_other_bids[0][0]
            if other_best_bid > 0.52:
                return {"action": "HOLD"}

        # Spread Filter: Reject wide spreads (spread > 0.03) to prevent latency loss
        spread = best_ask - best_bid
        if spread > MAX_SPREAD_USD + 1e-9:
            return {"action": "HOLD"}

        # Stoikov (2018) Micro-Price & Cont et al. (2014) Order Book Imbalance (OBI) Filter
        v_bid_top = sum(size for _, size in parsed_bids[:2])
        v_ask_top = sum(size for _, size in parsed_asks[:2])
        obi = _compute_obi(parsed_bids, parsed_asks, depth=2)
        p_micro = _compute_micro_price(best_bid, best_ask, v_bid_top, v_ask_top)
        p_mid = (best_ask + best_bid) / 2.0

        # Reject if severe ask pressure (OBI < -0.30) or micro-price signals downward tick
        if obi < -0.30:
            return {"action": "HOLD"}
        if p_micro < p_mid - 0.008:
            return {"action": "HOLD"}

        # Entry Price Window: 0.50 to 0.92 captures high-conviction trends from early breakout through mature favorites
        MIN_ENTRY_PRICE = 0.50
        if not (MIN_ENTRY_PRICE <= best_ask <= MAX_ENTRY_PRICE):
            return {"action": "HOLD"}

        # Model Directional & Valuation Validation:
        # 1. Require directional conviction from the quant engine
        target_prob = prob_yes if side == "YES" else prob_no
        if target_prob < 0.52:
            return {"action": "HOLD"}

        # 2. Prevent buying overpriced asks where market maker markup exceeds fair spread + fees
        # Normal taker friction (half-spread + protocol/platform fees) is -0.020 to -0.045.
        # Reject if edge is worse than -0.050 (meaning the ask is unacceptably marked up).
        fee_est = (0.07 * best_ask * (1.0 - best_ask)) + (0.01 * best_ask)
        breakeven_prob = best_ask + fee_est
        edge = target_prob - breakeven_prob
        if edge < -0.050:
            return {"action": "HOLD"}

        # ----------------------------------------------------------------------
        # 6. Dynamic Position Sizing (Tiered Anti-Ruin & Volume Acceleration Engine)
        # ----------------------------------------------------------------------
        rule_max_buy = float(rules.get("maximumBuyCashUsd", 5.0))
        available_cash_limit = max(0.0, cash_usd - 0.15)
        min_5_cost = (min_order_size * (best_ask + 0.02)) + 0.08

        if cash_usd < min_5_cost:
            return {"action": "HOLD"}

        if cash_usd < 12.0 or self.consecutive_losses >= 1:
            tier_budget = min_5_cost
        elif cash_usd < 20.0:
            tier_budget = min(4.88, max(min_5_cost, cash_usd * 0.40))
        else:
            tier_budget = MAX_BUY_CASH_CEILING

        max_spend_budget = min(rule_max_buy, MAX_BUY_CASH_CEILING, tier_budget, available_cash_limit)

        if max_spend_budget < 1.0:
            return {"action": "HOLD"}

        # Find maximum allowed shares that fit within budget
        optimal_shares = 0.0
        optimal_cash = 0.0

        for candidate_shares in [min_order_size + i for i in range(25)]:
            cost = _estimate_buy(asks, candidate_shares)
            if cost is None:
                break
            gross, fees = cost
            total_cash = gross + fees
            if total_cash <= max_spend_budget + 1e-9:
                optimal_shares = candidate_shares
                optimal_cash = total_cash
            else:
                break

        if optimal_shares < min_order_size:
            return {"action": "HOLD"}

        # Check immediate exit depth: Ensure bid side has adequate depth
        immediate_exit = _estimate_sell(bids, optimal_shares)
        if immediate_exit is None:
            return {"action": "HOLD"}

        exit_gross, exit_fees = immediate_exit
        immediate_roundtrip_loss = optimal_cash - (exit_gross - exit_fees)
        # Reject if instantaneous spread loss exceeds $0.075 per share (allows 1c-2c spreads, blocks wide 3c+)
        if immediate_roundtrip_loss > 0.075 * optimal_shares + 1e-9:
            return {"action": "HOLD"}

        # Learning 2 (Jaswanth): Add execution headroom buffer to prevent sub-minOrderSize rejections
        # during 250ms quote movement, capped strictly under max_spend_budget and MAX_BUY_CASH_CEILING
        headroom = BUY_HEADROOM_PER_SHARE * optimal_shares
        funded_cash = min(max_spend_budget, optimal_cash + headroom)

        self.last_buy_attempt_at = now
        self.pre_trade_cash_usd = cash_usd
        return {
            "action": "BUY",
            "outcome": side,
            "maxCashUsd": round(funded_cash, 6),
        }


# Global singleton instance for production server
BOT = ProductionTradingBot()


class DecideRequestHandler(BaseHTTPRequestHandler):
    """Production HTTP handler strictly following POST /decide contract."""

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/decide":
            self.send_error(404)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length)
            payload = json.loads(raw_body)
            decision = BOT.decide(payload)
            response_bytes = json.dumps(decision, separators=(",", ":")).encode("utf-8")
        except Exception:
            response_bytes = b'{"action":"HOLD"}'

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def log_message(self, _format: str, *_args: Any) -> None:
        """Suppress standard logging to ensure sub-millisecond response latency."""
        return


def run_server(port: int = 8080) -> None:
    server_address = ("0.0.0.0", port)
    server = ThreadingHTTPServer(server_address, DecideRequestHandler)
    print(f"YeNo Production Bot listening on http://0.0.0.0:{port}/decide", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down bot.", flush=True)
        server.server_close()


if __name__ == "__main__":
    port_env = int(os.environ.get("PORT", "8080"))
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port_env = int(sys.argv[1])
    run_server(port_env)
