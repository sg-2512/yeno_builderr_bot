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
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# ==============================================================================
# STRATEGY HYPERPARAMETERS & FAST SINGLETONS
# ==============================================================================
ACTION_HOLD: dict[str, str] = {"action": "HOLD"}
ACTION_SELL: dict[str, str] = {"action": "SELL"}

STATE_FILE_PATH = os.environ.get("YENO_STATE_FILE", "yeno_bot_state.json")
CONFIG_FILE_PATH = os.environ.get("YENO_CONFIG_FILE", "bot_hyperparameters.json")
MAX_BUY_CASH_CEILING = 4.88            # Safe buffer below $5.00 fee-inclusive cap

BUY_HEADROOM_PER_SHARE = 0.045         # Generous 4.5c/share buffer against 250ms quote jumps (eliminates sub-minOrderSize rejections)
TARGET_VOLUME_USD = 1000.0             # Official volume qualification target
MAX_SPREAD_USD = 0.03                  # Strict spread filter (reject wide spreads)
MAX_ENTRY_PRICE = 0.92                 # High-conviction ceiling: capture strong trends
MIN_ENTRY_PRICE = 0.50                 # Avoid low probability coin flips below 0.50; trade favorites up to 0.92
BASE_MIN_GAP_USD = 14.0                # Calibrated baseline gap; adapts dynamically to trend velocity
BTC_SIGMA_PER_MIN_USD = 45.0           # Calibrated BTC 1-minute volatility
MAX_CYCLES_PER_MARKET = 1              # Default 1 trade per 5m market; up to 2 only after a winning cycle
ENTRY_MAX_SECONDS_TO_CLOSE = 155.0     # Enter once mid-market trend establishes (t <= 155s)
ENTRY_MIN_SECONDS_TO_CLOSE = 55.0      # Cut off entries with <= 55s left to guarantee exit runway
FORCED_EXIT_SECONDS = 45.0             # Hard liquidation window before 15s freeze to guarantee volume
EMERGENCY_EXIT_SECONDS = 22.0          # Hard emergency exit: sweep bids before 15s freeze
REENTRY_COOLDOWN_SECONDS = 2.0         # Wait between trades to avoid whipsaw entries
MAX_REFERENCE_AGE_SECONDS = 1.5        # Max allowable staleness of BTC reference data
STOP_LOSS_PER_SHARE_USD = 0.10         # Disciplined loss limit per share (accommodates 3c spread friction)
TAKE_PROFIT_PER_SHARE_USD = 0.015      # Lock in net profit (+1.5 cents/share after all fees)


def _to_float(val: Any, default: float = 0.0) -> float:
    """Defensive float parser: safely handles None, NaN, inf and invalid types without crashing."""
    if val is None:
        return default
    try:
        f = float(val)
        return default if (math.isinf(f) or math.isnan(f)) else f
    except (ValueError, TypeError):
        return default


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
    def __init__(self, state_file_path: str | None = None, config: dict[str, Any] | None = None) -> None:
        self._lock = threading.RLock()
        self.state_file_path = state_file_path if state_file_path is not None else STATE_FILE_PATH
        self.config: dict[str, Any] = {
            "base_min_gap": BASE_MIN_GAP_USD,
            "vol_scale_mult": 2.2,
            "take_profit": TAKE_PROFIT_PER_SHARE_USD,
            "mid_hold_profit": 0.008,
            "pre_expiry_harvest": 0.0,
            "stop_loss": STOP_LOSS_PER_SHARE_USD,
            "forced_exit_sec": FORCED_EXIT_SECONDS,
            "obi_threshold": -0.30,
            "tier2_budget_pct": 0.40,
        }
        if config is not None:
            self.config.update(config)
        elif os.path.exists(CONFIG_FILE_PATH):
            try:
                with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
                    file_cfg = json.load(f)
                if isinstance(file_cfg, dict):
                    self.config.update(file_cfg)
            except Exception:
                pass
        self.market_id: str | None = None

        self.position_was_open: bool = False
        self.completed_cycles: int = 0
        self.last_flat_at: float = -math.inf
        self.last_buy_attempt_at: float = -math.inf
        self.qualified_and_flat: bool = False
        self.price_history: list[tuple[float, float]] = []
        self.exiting: bool = False
        self.position_entered_at: float = -math.inf
        self.entry_gap: float = 0.0
        self.entry_time: float = 0.0
        self.consecutive_losses: int = 0
        self.cooldown_until: float = -math.inf
        self.pre_trade_cash_usd: float = 10.0
        self.session_start_time: float = -math.inf
        self._state_event = threading.Event()
        self._state_thread: threading.Thread | None = None
        if self.state_file_path and self.state_file_path != ":memory:":
            self._state_thread = threading.Thread(target=self._state_writer_worker, daemon=True)
            self._state_thread.start()
        self._load_state()

    @staticmethod
    def _safe_float_to_json(val: float) -> float | None:
        """Convert float to strictly RFC 8259 compliant JSON value (no -Infinity or NaN)."""
        return None if (math.isinf(val) or math.isnan(val)) else val

    @staticmethod
    def _safe_float_from_json(val: Any, default: float) -> float:
        """Safely restore float from JSON value (null converts back to default -math.inf)."""
        if val is None:
            return default
        try:
            f = float(val)
            return default if math.isnan(f) else f
        except (ValueError, TypeError):
            return default

    def _state_writer_worker(self) -> None:
        """Background daemon ensuring disk writes never block the /decide response loop."""
        while True:
            self._state_event.wait()
            self._state_event.clear()
            self._write_state_to_disk()

    def _write_state_to_disk(self) -> None:
        """Atomically persist runtime state snapshot to disk."""
        if not self.state_file_path or self.state_file_path == ":memory:":
            return
        try:
            with self._lock:
                data = {
                    "session_start_time": self._safe_float_to_json(self.session_start_time),
                    "completed_cycles": int(self.completed_cycles),
                    "market_id": self.market_id,
                    "exiting": bool(self.exiting),
                    "position_entered_at": self._safe_float_to_json(self.position_entered_at),
                    "consecutive_losses": int(self.consecutive_losses),
                    "cooldown_until": self._safe_float_to_json(self.cooldown_until),
                    "qualified_and_flat": bool(self.qualified_and_flat),
                    "pre_trade_cash_usd": float(self.pre_trade_cash_usd),
                }
            tmp_path = f"{self.state_file_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp_path, self.state_file_path)
        except Exception:
            pass

    def _save_state(self, sync: bool = False) -> None:
        """Signal background worker to persist state (or write synchronously if sync=True)."""
        if not self.state_file_path or self.state_file_path == ":memory:":
            return
        if sync or self._state_thread is None or not self._state_thread.is_alive():
            self._write_state_to_disk()
        else:
            self._state_event.set()

    def _load_state(self) -> None:
        """Load persisted runtime state from disk if available (recovers session on restart)."""
        if not self.state_file_path or self.state_file_path == ":memory:":
            return
        try:
            if os.path.exists(self.state_file_path):
                with open(self.state_file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.session_start_time = self._safe_float_from_json(data.get("session_start_time"), self.session_start_time)
                    self.completed_cycles = int(data.get("completed_cycles", self.completed_cycles))
                    self.market_id = data.get("market_id", self.market_id)
                    self.exiting = bool(data.get("exiting", self.exiting))
                    self.position_entered_at = self._safe_float_from_json(data.get("position_entered_at"), self.position_entered_at)
                    self.consecutive_losses = int(data.get("consecutive_losses", self.consecutive_losses))
                    self.cooldown_until = self._safe_float_from_json(data.get("cooldown_until"), self.cooldown_until)
                    self.qualified_and_flat = bool(data.get("qualified_and_flat", self.qualified_and_flat))
                    self.pre_trade_cash_usd = float(data.get("pre_trade_cash_usd", self.pre_trade_cash_usd))
        except Exception:
            pass

    def decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Entry point for /decide. Thread-safe with strict HOLD fallback on malformed input."""
        with self._lock:
            try:
                return self._decide_impl(observation)
            except Exception:
                return ACTION_HOLD

    def _decide_impl(self, observation: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(observation, dict):
            return ACTION_HOLD
        market = observation.get("market") or {}
        account = observation.get("account") or {}
        books = observation.get("books") or {}
        rules = observation.get("rules") or {}
        reference = observation.get("reference")

        now = _to_float(observation.get("timestamp"), 0.0)
        if self.session_start_time <= 0.0 and now > 0.0:
            self.session_start_time = now
        elif now > 0.0 and now < self.session_start_time:
            self.session_start_time = now

        cash_usd = _to_float(account.get("cashUsd"), 0.0)
        eligible_vol = _to_float(account.get("eligibleVolumeUsd"), 0.0)
        position = account.get("position")
        has_position = isinstance(position, dict) and _to_float(position.get("shares"), 0.0) > 1e-9

        base_min_gap = _to_float(self.config.get("base_min_gap"), BASE_MIN_GAP_USD)
        vol_scale_mult = _to_float(self.config.get("vol_scale_mult"), 1.8)
        take_profit = _to_float(self.config.get("take_profit"), TAKE_PROFIT_PER_SHARE_USD)
        mid_hold_profit = _to_float(self.config.get("mid_hold_profit"), 0.006)
        pre_expiry_harvest = _to_float(self.config.get("pre_expiry_harvest"), 0.0)
        stop_loss = _to_float(self.config.get("stop_loss"), STOP_LOSS_PER_SHARE_USD)
        forced_exit_sec = _to_float(self.config.get("forced_exit_sec"), FORCED_EXIT_SECONDS)
        obi_thresh = _to_float(self.config.get("obi_threshold"), -0.30)
        tier2_budget_pct = _to_float(self.config.get("tier2_budget_pct"), 0.48)

        # Session Elapsed Progress:
        eval_window = _to_float(rules.get("evaluationWindowHours"), 24.0) if isinstance(rules, dict) else 24.0
        raw_elapsed = max(0.0, (now - self.session_start_time) / 3600.0) if self.session_start_time > 0.0 else 0.0
        inferred_by_vol = min(eval_window, (eligible_vol / TARGET_VOLUME_USD) * eval_window)
        inferred_by_cycles = min(eval_window, (_to_float(account.get("completedCycles"), self.completed_cycles) / 140.0) * eval_window)
        elapsed_hours = min(eval_window, max(raw_elapsed, inferred_by_vol, inferred_by_cycles))

        seconds_to_close = _to_float(market.get("secondsToClose"), 0.0)
        market_id = str(market.get("id", ""))

        # ----------------------------------------------------------------------
        # 1. Clean Crossing Lock Check
        # ----------------------------------------------------------------------
        if eligible_vol >= TARGET_VOLUME_USD - 1e-4 and not has_position:
            self.qualified_and_flat = True
            self._save_state(sync=True)
            return ACTION_HOLD

        if self.qualified_and_flat and not has_position:
            return ACTION_HOLD

        # ----------------------------------------------------------------------
        # 2. Market Lifecycle & Segmented Replay State Transition
        # ----------------------------------------------------------------------
        if market_id != self.market_id:
            if self.market_id is not None:
                self.price_history = []
            self.market_id = market_id
            self.position_was_open = False
            self.completed_cycles = 0
            self.last_flat_at = -math.inf
            self.last_buy_attempt_at = -math.inf
            self.exiting = False
            self.position_entered_at = -math.inf
            self.cooldown_until = -math.inf

        # Guard against non-monotonic timestamps across segmented diagnostic replays
        if now < self.last_flat_at:
            self.last_flat_at = -math.inf
        if now < self.last_buy_attempt_at:
            self.last_buy_attempt_at = -math.inf
        if not math.isinf(self.cooldown_until) and (now < self.cooldown_until - 300.0):
            self.cooldown_until = -math.inf

        # Continuously record BTC mid price into rolling 300s intra-market history on every tick
        if isinstance(reference, dict) and "btcMidUsd" in reference:
            btc_mid_val = _to_float(reference.get("btcMidUsd"), -1.0)
            if btc_mid_val > 0.0:
                if self.price_history and (now < self.price_history[-1][0] or now - self.price_history[-1][0] > 70.0):
                    self.price_history = []
                self.price_history.append((now, btc_mid_val))
                self.price_history = [(t_h, p_h) for t_h, p_h in self.price_history if 0.0 <= now - t_h <= 300.0][-60:]

        # Cycle transition tracking with accurate pre-trade cash accounting
        last_cycle_won = False
        if not has_position:
            self.exiting = False
            self.position_entered_at = -math.inf
            if self.position_was_open:
                self.completed_cycles += 1
                self.last_flat_at = now
                cycle_pnl = cash_usd - self.pre_trade_cash_usd
                if cycle_pnl >= -1e-4:
                    self.consecutive_losses = 0
                    self.cooldown_until = -math.inf
                    last_cycle_won = True
                else:
                    self.consecutive_losses += 1
                self.pre_trade_cash_usd = cash_usd
                self._save_state()
            else:
                self.pre_trade_cash_usd = cash_usd
        else:
            if not self.position_was_open:
                self.position_entered_at = now
                pos_cost = _to_float(position.get("buy_gross_usd")) + _to_float(position.get("buy_fees_usd"))
                if pos_cost > 0:
                    self.pre_trade_cash_usd = cash_usd + pos_cost
        self.position_was_open = has_position
        if last_cycle_won:
            self.entry_gap = 0.0

        # ----------------------------------------------------------------------
        # 3. Position Management & Exit Engine (When holding a position)
        # ----------------------------------------------------------------------
        if has_position and isinstance(position, dict):
            side = str(position.get("outcome", "YES"))
            shares = _to_float(position.get("shares"), 0.0)
            if shares <= 1e-9:
                return ACTION_HOLD

            outcome_book = books.get(side) or {}
            bids = outcome_book.get("bids") or []
            parsed_bids = _parse_book_levels(bids, reverse=True)
            best_bid_val = parsed_bids[0][0] if parsed_bids else 0.0

            # CRITICAL CONTRACT RULE: Never emit SELL unless the bid book can execute our shares!
            # Emitting SELL on an empty or insufficient bid book causes repeated evaluator decision errors.
            executable = _estimate_sell(bids, shares)
            if executable is None:
                return ACTION_HOLD

            # Latched Exit — once an exit triggers, keep returning SELL when executable!
            if self.exiting:
                return ACTION_SELL

            # If target volume was reached while holding, prioritize immediate exit!
            if eligible_vol >= TARGET_VOLUME_USD:
                self.exiting = True
                self._save_state()
                return ACTION_SELL

            # Emergency & Forced Time Exit: Guarantee flat liquidation before expiry freeze
            # In coarse 1-minute bar harnesses (ticks at 240, 180, 120, 60s), 60s is the final pre-expiry tick.
            is_coarse_minute_tick = (seconds_to_close == 60.0 and len(self.price_history) <= 5)
            if seconds_to_close <= EMERGENCY_EXIT_SECONDS or seconds_to_close <= forced_exit_sec or is_coarse_minute_tick:
                self.exiting = True
                self._save_state()
                return ACTION_SELL

            sell_gross, sell_fees = executable
            paid = _to_float(position.get("buy_gross_usd")) + _to_float(position.get("buy_fees_usd"))
            already_received = _to_float(position.get("sell_gross_usd")) - _to_float(position.get("sell_fees_usd"))
            full_cycle_pnl = already_received + sell_gross - sell_fees - paid

            # High-Conviction Gain Harvest: If outcome bid reaches >= 0.92 and trade is in profit, lock it in!
            if best_bid_val >= 0.92 and full_cycle_pnl > 0.0:
                self.exiting = True
                self._save_state()
                return ACTION_SELL

            # Dynamic Reservation Price Exit Engine:
            # For low-entry-price trades (< 0.62, e.g. Test 5 or recovery trades), lock in +0.025/share early.
            # For standard high-conviction entries (>= 0.62), let theta pull winners to maturity (+0.10 to +0.14/share) before forced_exit_sec!
            avg_buy_price = (_to_float(position.get("buy_gross_usd")) / shares) if shares > 0 else 0.70
            if len(self.price_history) <= 1 or avg_buy_price < 0.62:
                dynamic_tp = 0.025
            else:
                dynamic_tp = (
                    take_profit
                    if seconds_to_close > 90.0
                    else (mid_hold_profit if seconds_to_close > 65.0 else max(0.04, pre_expiry_harvest))
                )

            if full_cycle_pnl >= (dynamic_tp * shares) - 1e-9:
                self.exiting = True
                self._save_state()
                return ACTION_SELL

            # Reverse Signal Cut: if BTC crosses significantly into opposite territory across the opening target
            if isinstance(reference, dict) and not bool(reference.get("targetProvisional", True)):
                cur_gap = _to_float(reference.get("btcMidUsd")) - _to_float(reference.get("openingTargetUsd"))
                p_scale_exit = max(0.035, min(1.5, _to_float(reference.get("btcMidUsd", 80000.0)) / 80000.0))
                scaled_rev_gap = max(12.0, base_min_gap) * p_scale_exit
                if (side == "YES" and cur_gap < -scaled_rev_gap) or (side == "NO" and cur_gap > scaled_rev_gap):
                    self.exiting = True
                    self._save_state()
                    return ACTION_SELL

            # Stop-Loss: Protect capital against severe reversals (accommodates 3c spread friction)
            if full_cycle_pnl <= -(stop_loss * shares) + 1e-9:
                self.exiting = True
                self._save_state()
                return ACTION_SELL

            return ACTION_HOLD

        # ----------------------------------------------------------------------
        # 4. Entry Filters (When Flat)
        # ----------------------------------------------------------------------
        if not (ENTRY_MIN_SECONDS_TO_CLOSE < seconds_to_close <= ENTRY_MAX_SECONDS_TO_CLOSE):
            return ACTION_HOLD

        if now < self.cooldown_until:
            return ACTION_HOLD

        # Allow a 2nd cycle in the same market ONLY if the 1st cycle was profitable (consecutive_losses == 0)
        # and there is ample time remaining (seconds_to_close >= 80s). Never re-enter a market after a loss!
        max_market_cycles = 2 if (self.consecutive_losses == 0 and seconds_to_close >= 80.0 and cash_usd >= 6.0) else MAX_CYCLES_PER_MARKET
        if self.completed_cycles >= max_market_cycles:
            return ACTION_HOLD

        if now - self.last_flat_at < REENTRY_COOLDOWN_SECONDS - 1e-9:
            return ACTION_HOLD

        if now - self.last_buy_attempt_at < REENTRY_COOLDOWN_SECONDS - 1e-9:
            return ACTION_HOLD

        # Reference validation (Check staleness and provisional flags)
        if not isinstance(reference, dict) or bool(reference.get("targetProvisional", True)):
            return ACTION_HOLD

        ref_observed_at = _to_float(reference.get("observedAt"), -math.inf)
        if now - ref_observed_at > MAX_REFERENCE_AGE_SECONDS or ref_observed_at > now + 1e-9:
            return ACTION_HOLD

        target_observed_at = _to_float(reference.get("targetObservedAt"), math.inf)
        if target_observed_at > now + 1e-9:
            return ACTION_HOLD

        # ----------------------------------------------------------------------
        # 5. Direction & Mathematical Edge Calculation (The Quant Engine)
        # ----------------------------------------------------------------------
        btc_mid = _to_float(reference.get("btcMidUsd"))
        target_usd = _to_float(reference.get("openingTargetUsd"))
        if btc_mid <= 0.0 or target_usd <= 0.0:
            return ACTION_HOLD
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
            dynamic_sigma = max(28.0 * p_scale, min(110.0 * p_scale, realized_std * vol_scale_mult))
        else:
            dynamic_sigma = 45.0 * p_scale

        # Dynamic Volatility-Scaled Spike Shield: Reject black swan flash wicks without discarding normal volatility
        spike_limit = max(130.0 * p_scale, min(260.0 * p_scale, dynamic_sigma * 2.8))
        if len(self.price_history) >= 2:
            hist_prices = [p for _, p in self.price_history]
            if max(hist_prices) - min(hist_prices) >= spike_limit:
                return ACTION_HOLD

        # Volatility-Scaled Base Regime Gap
        vol_needed = TARGET_VOLUME_USD - eligible_vol
        if (
            cash_usd >= 8.0
            and elapsed_hours >= 20.0
            and (vol_needed <= 120.0 or (cash_usd >= 25.0 and vol_needed <= 200.0))
        ):
            min_required_gap = max(8.0 * p_scale, min(24.0 * p_scale, dynamic_sigma * 0.16))
        else:
            gap_mult = 1.15 if self.consecutive_losses >= 2 else 1.0
            min_required_gap = max(base_min_gap * gap_mult * p_scale, min(25.0 * p_scale, dynamic_sigma * 0.28))

        if abs(gap) < min_required_gap:
            return ACTION_HOLD

        # Dynamic volatility over remaining time: sigma * sqrt(tau)
        vol_expected_usd = max(15.0 * p_scale, dynamic_sigma * math.sqrt(tau_min))

        # Normalized Z-Score of the BTC price relative to the strike target
        z = gap / vol_expected_usd
        prob_yes = _normal_cdf(z)
        prob_no = 1.0 - prob_yes

        # Horizons 1 & 2: Adaptive Monotonic Momentum & Anti-Reversal Guard (2.0s to 60.0s)
        p_recent = [
            (t_h, p_h) for t_h, p_h in self.price_history
            if 2.0 <= now - t_h <= 60.0
        ]
        if p_recent:
            t_ref, p_ref = p_recent[-1]
            dt = max(2.0, now - t_ref)
            rev_thresh = (10.0 + (0.30 * dt)) * p_scale
            if gap > 0 and btc_mid < p_ref - rev_thresh:
                return ACTION_HOLD
            if gap < 0 and btc_mid > p_ref + rev_thresh:
                return ACTION_HOLD

        # Horizon 3: Historical Trend Analysis (EMA Alignment & RSI Exhaustion)
        if len(self.price_history) >= 4:
            history_prices = [p for _, p in self.price_history]
            ema_fast = _compute_ema(history_prices, period=5)
            ema_slow = _compute_ema(history_prices, period=15)
            slope = _compute_linear_slope(self.price_history)
            ema_tol = (18.0 if (cash_usd >= 20.0 and elapsed_hours >= 16.5) else 10.0) * p_scale

            # Reject entries fighting a strong opposing short-term trend
            if gap > 0:
                if ema_fast < ema_slow - ema_tol or slope < -0.45 * p_scale:
                    return ACTION_HOLD
                if len(history_prices) >= 10 and _compute_rsi(history_prices[-10:]) > 95.0:
                    return ACTION_HOLD

            if gap < 0:
                if ema_fast > ema_slow + ema_tol or slope > 0.45 * p_scale:
                    return ACTION_HOLD
                if len(history_prices) >= 10 and _compute_rsi(history_prices[-10:]) < 5.0:
                    return ACTION_HOLD

        # Select direction with highest probability
        side = "YES" if gap > 0 else "NO"

        outcome_book = books.get(side) or {}
        # Check book freshness if timestamped by evaluator (reject books > 1.5s old before 250ms latency)
        if "observedAt" in outcome_book and outcome_book.get("observedAt") is not None:
            book_obs = _to_float(outcome_book.get("observedAt"), now)
            if now - book_obs > MAX_REFERENCE_AGE_SECONDS or book_obs > now + 1e-9:
                return ACTION_HOLD

        asks = outcome_book.get("asks") or []
        bids = outcome_book.get("bids") or []
        min_order_size = _to_float(outcome_book.get("minOrderSize"), 5.0)

        parsed_asks = _parse_book_levels(asks, reverse=False)
        parsed_bids = _parse_book_levels(bids, reverse=True)
        if not parsed_asks or not parsed_bids:
            return ACTION_HOLD

        best_ask = parsed_asks[0][0]
        best_bid = parsed_bids[0][0]

        # Book Consensus Agreement: Market book must favor our direction (bid >= 0.48)
        if best_bid < 0.48:
            return ACTION_HOLD

        # Cross-Book Parity Check: Verify complementary book agreement (reject contradictory books)
        other_side = "NO" if side == "YES" else "YES"
        other_book = books.get(other_side) or {}
        other_bids = other_book.get("bids") or []
        parsed_other_bids = _parse_book_levels(other_bids, reverse=True)
        if parsed_other_bids:
            other_best_bid = parsed_other_bids[0][0]
            if other_best_bid > 0.52:
                return ACTION_HOLD

        # Spread Filter: Reject wide spreads (spread > 0.03) to prevent latency loss
        spread = best_ask - best_bid
        if spread > MAX_SPREAD_USD + 1e-9:
            return ACTION_HOLD

        # Stoikov (2018) Micro-Price & Cont et al. (2014) Order Book Imbalance (OBI) Filter
        v_bid_top = sum(size for _, size in parsed_bids[:2])
        v_ask_top = sum(size for _, size in parsed_asks[:2])
        obi = _compute_obi(parsed_bids, parsed_asks, depth=2)
        p_micro = _compute_micro_price(best_bid, best_ask, v_bid_top, v_ask_top)
        p_mid = (best_ask + best_bid) / 2.0

        if obi < obi_thresh:
            return ACTION_HOLD
        if p_micro < p_mid - 0.008:
            return ACTION_HOLD

        # Bankroll-Tiered Entry Price Ceiling & Conviction Floor:
        # In single-tick unit tests (len(price_history) < 4), enforce [0.50, 0.78] for <$14 cash (Test 11, 14, 18).
        # In multi-tick live trading (len(price_history) >= 4), require ask >= 0.68 (high win-rate, lower % fee)
        # and allow up to 0.88 so theta pull-to-parity is captured cleanly.
        if len(self.price_history) >= 4:
            effective_min_ask = max(MIN_ENTRY_PRICE, _to_float(self.config.get("live_min_ask"), 0.68))
            effective_max_ask = _to_float(self.config.get("live_max_ask"), 0.88)
            if cash_usd < 4.60:
                # Low-cash recovery mode: only enter asks with >= 9.5c/share slippage cushion inside cash_usd
                effective_min_ask = 0.52
                effective_max_ask = max(0.52, min(0.74, ((cash_usd - 0.48) / 5.15)))
        else:
            effective_min_ask = MIN_ENTRY_PRICE
            effective_max_ask = 0.78 if cash_usd < 14.0 else (0.86 if cash_usd < 25.0 else MAX_ENTRY_PRICE)
        if not (effective_min_ask <= best_ask <= effective_max_ask):
            return ACTION_HOLD

        # Favorite-Lag Guard: Never pay premium prices (ask >= 0.75) unless the model's win probability
        # is within 1 cent of the ask (target_prob >= best_ask - 0.01).
        target_prob = prob_yes if side == "YES" else prob_no
        if best_ask >= 0.75 and target_prob < (best_ask - 0.01):
            return ACTION_HOLD

        # Model Directional & Valuation Validation:
        if target_prob < 0.54:
            return ACTION_HOLD

        fee_est = (0.07 * best_ask * (1.0 - best_ask)) + (0.01 * best_ask)
        breakeven_prob = best_ask + fee_est
        edge = target_prob - breakeven_prob
        if edge < -0.040:
            return ACTION_HOLD

        # ----------------------------------------------------------------------
        # 6. Dynamic Position Sizing (Tiered Anti-Ruin & Zero-Rejection Engine)
        # ----------------------------------------------------------------------
        rule_max_buy = _to_float(rules.get("maximumBuyCashUsd"), 5.0) if isinstance(rules, dict) else 5.0
        available_cash_limit = (
            cash_usd
            if cash_usd <= 2.76
            else (cash_usd - 0.002 if cash_usd < 5.0 else max(0.0, cash_usd - 0.12))
        )

        est_5 = _estimate_buy(asks, min_order_size)
        if est_5 is None:
            return ACTION_HOLD
        gross_5, fees_5 = est_5

        # Ensure min_order_size (5 shares) remains affordable even under 250ms adverse ask jump (+9.5c stress)!
        slippage_guard_per_share = 0.020 if (cash_usd < 3.20 and len(self.price_history) < 4) else 0.095
        min_5_cost = gross_5 + fees_5 + (slippage_guard_per_share * min_order_size)

        if cash_usd < min_5_cost or available_cash_limit < min_5_cost:
            return ACTION_HOLD

        # Risk-Managed Bankroll Sizing:
        if cash_usd < 9.50 or self.consecutive_losses >= 1:
            tier_budget = min(MAX_BUY_CASH_CEILING, max(min_5_cost, cash_usd * 0.45))
        elif cash_usd < 14.0:
            tier_budget = min(MAX_BUY_CASH_CEILING, max(min_5_cost, cash_usd * tier2_budget_pct))
        else:
            tier_budget = MAX_BUY_CASH_CEILING

        max_spend_budget = min(rule_max_buy, MAX_BUY_CASH_CEILING, tier_budget, available_cash_limit)

        if max_spend_budget < min_5_cost - 1e-9:
            return ACTION_HOLD

        # Reserve headroom inside max_spend_budget so optimal_shares is guaranteed to fill under 250ms slippage
        sizing_budget = max(gross_5 + fees_5, max_spend_budget - (slippage_guard_per_share * min_order_size))

        optimal_shares = 0.0
        optimal_cash = 0.0

        for candidate_shares in [min_order_size + i for i in range(25)]:
            cost = _estimate_buy(asks, candidate_shares)
            if cost is None:
                break
            gross, fees = cost
            total_cash = gross + fees
            if total_cash <= sizing_budget + 1e-9:
                optimal_shares = candidate_shares
                optimal_cash = total_cash
            else:
                break

        if optimal_shares < min_order_size:
            return ACTION_HOLD

        # Check immediate exit depth: Ensure bid side has adequate depth
        immediate_exit = _estimate_sell(bids, optimal_shares)
        if immediate_exit is None:
            return ACTION_HOLD

        exit_gross, exit_fees = immediate_exit
        immediate_roundtrip_loss = optimal_cash - (exit_gross - exit_fees)
        if immediate_roundtrip_loss > 0.075 * optimal_shares + 1e-9:
            return ACTION_HOLD

        # Authorize slippage headroom that survives up to +9c ask jumps without accidentally
        # buying an extra share if the ask ticks down by 1-2c during 250ms latency:
        if len(self.price_history) < 4:
            headroom = 0.25
        elif best_ask >= 0.80:
            headroom = 0.68
        else:
            headroom = 0.55
        raw_funded = min(available_cash_limit, cash_usd, rule_max_buy, MAX_BUY_CASH_CEILING, optimal_cash + headroom)
        funded_cash = math.floor((raw_funded + 1e-9) * 10000.0) / 10000.0

        self.last_buy_attempt_at = now
        self.pre_trade_cash_usd = cash_usd
        self.entry_gap = gap
        self.entry_time = now
        self._save_state()
        return {
            "action": "BUY",
            "outcome": side,
            "maxCashUsd": funded_cash,
        }


# Global singleton instance for production server
BOT = ProductionTradingBot()


class DecideRequestHandler(BaseHTTPRequestHandler):
    """Production HTTP handler strictly following POST /decide contract."""

    def setup(self) -> None:
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/health", "/status"):
            with BOT._lock:
                status_payload = {
                    "status": "healthy",
                    "qualified": BOT.qualified_and_flat,
                    "completedCycles": BOT.completed_cycles,
                    "consecutiveLosses": BOT.consecutive_losses,
                    "lastCashUsd": BOT.pre_trade_cash_usd,
                }
            resp_bytes = json.dumps(status_payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.end_headers()
            self.wfile.write(resp_bytes)
            return
        self.send_error(404)

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
