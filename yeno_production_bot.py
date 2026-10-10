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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# ==============================================================================
# STRATEGY HYPERPARAMETERS
# ==============================================================================
MAX_BUY_CASH_CEILING = 4.88            # Safe buffer below $5.00 fee-inclusive cap
CASH_UTILIZATION_RATIO = 0.94          # Max fraction of available cash to risk per BUY
TARGET_VOLUME_USD = 1000.0             # Official volume qualification target
MAX_SPREAD_USD = 0.03                  # Strict spread filter (reject wide spreads)
MAX_ENTRY_PRICE = 0.74                 # Asymmetric risk/reward: buy before contract is overbought
MIN_ENTRY_PRICE = 0.30                 # Avoid low probability lotteries
BASE_MIN_GAP_USD = 15.0                # Base BTC price vs strike gap (in USD)
BTC_SIGMA_PER_MIN_USD = 45.0           # Estimated BTC 1-minute price volatility ($40-$50)
MIN_EDGE_MARGIN = 0.015                # Required mathematical probability edge above fees
MAX_CYCLES_PER_MARKET = 4              # Max completed roundtrips in a single 5m market
ENTRY_MAX_SECONDS_TO_CLOSE = 210.0     # Scan for entries once trend establishes
ENTRY_MIN_SECONDS_TO_CLOSE = 50.0      # Cut off new entries to guarantee time for exit
FORCED_EXIT_SECONDS = 42.0             # Standard exit threshold before expiry decay
EMERGENCY_EXIT_SECONDS = 26.0          # Hard emergency exit: sweep bids before 15s freeze
REENTRY_COOLDOWN_SECONDS = 1.5         # Wait between trades to avoid whipsaw entries
MAX_REFERENCE_AGE_SECONDS = 1.5        # Max allowable staleness of BTC reference data
STOP_LOSS_PER_SHARE_USD = 0.14         # Allow trade room to breathe beyond bid-ask spread
TAKE_PROFIT_PER_SHARE_USD = 0.00       # Sell at breakeven or profit: lock in volume at zero net cost


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
        self.market_id: str | None = None
        self.position_was_open: bool = False
        self.completed_cycles: int = 0
        self.last_flat_at: float = -math.inf
        self.last_buy_attempt_at: float = -math.inf
        self.qualified_and_flat: bool = False
        self.price_history: list[tuple[float, float]] = []

    def decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Entry point for /decide. Fully exception-safe with sub-millisecond execution."""
        try:
            return self._decide_impl(observation)
        except Exception:
            return {"action": "HOLD"}

    def _decide_impl(self, observation: dict[str, Any]) -> dict[str, Any]:
        market = observation.get("market", {})
        account = observation.get("account", {})
        books = observation.get("books", {})
        rules = observation.get("rules", {})
        reference = observation.get("reference")
        
        now = float(observation.get("timestamp", 0.0))
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
            self.price_history = []

        # Cycle transition tracking
        if position is None and self.position_was_open:
            self.completed_cycles += 1
            self.last_flat_at = now
        self.position_was_open = position is not None

        # ----------------------------------------------------------------------
        # 3. Position Management & Exit Engine (When holding a position)
        # ----------------------------------------------------------------------
        if position is not None:
            side = str(position.get("outcome", "YES"))
            shares = float(position.get("shares", 0.0))
            if shares <= 1e-9:
                return {"action": "HOLD"}

            # If target volume was reached while holding, prioritize immediate exit!
            if eligible_vol >= TARGET_VOLUME_USD:
                return {"action": "SELL"}

            outcome_book = books.get(side, {})
            bids = outcome_book.get("bids", [])

            # Emergency Time Exit: Force SELL before the 15-second pre-expiry lock!
            # Settling at expiry awards $0 volume; we must sell into the book.
            if seconds_to_close <= EMERGENCY_EXIT_SECONDS:
                return {"action": "SELL"}

            executable = _estimate_sell(bids, shares)
            if executable is not None:
                sell_gross, sell_fees = executable
                paid = float(position.get("buy_gross_usd", 0.0)) + float(position.get("buy_fees_usd", 0.0))
                already_received = float(position.get("sell_gross_usd", 0.0)) - float(position.get("sell_fees_usd", 0.0))
                full_cycle_pnl = already_received + sell_gross - sell_fees - paid

                # Condition A: Take-Profit (Lock in profit above all roundtrip fees)
                if full_cycle_pnl >= (TAKE_PROFIT_PER_SHARE_USD * shares) - 1e-9:
                    return {"action": "SELL"}

                # Condition B: Breakeven Exit when approaching expiry (t <= 55s)
                # Breakeven volume is 100% win in this competition!
                if seconds_to_close <= 55.0 and full_cycle_pnl >= -1e-9:
                    return {"action": "SELL"}

                # Condition C: Stop-Loss (Protect capital against trend reversals)
                if full_cycle_pnl <= -(STOP_LOSS_PER_SHARE_USD * shares) + 1e-9:
                    return {"action": "SELL"}

                # Condition D: Forced Time Exit
                if seconds_to_close <= FORCED_EXIT_SECONDS:
                    return {"action": "SELL"}
            else:
                # If exact depth is not visible on L2 but seconds <= 40, signal SELL
                # allowing evaluator to match partial bids before the 15s freeze.
                if seconds_to_close <= 40.0:
                    return {"action": "SELL"}

            return {"action": "HOLD"}

        # ----------------------------------------------------------------------
        # 4. Entry Filters (When Flat)
        # ----------------------------------------------------------------------
        # Safe entry window: Evaluator blocks entries <= 15s; we stop at 50s for exit safety
        if not (ENTRY_MIN_SECONDS_TO_CLOSE < seconds_to_close <= ENTRY_MAX_SECONDS_TO_CLOSE):
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
        # Dynamic volatility over remaining time: σ * sqrt(τ)
        vol_expected_usd = max(15.0, BTC_SIGMA_PER_MIN_USD * math.sqrt(tau_min))

        # Normalized Z-Score of the BTC price relative to the strike target
        z = gap / vol_expected_usd
        prob_yes = _normal_cdf(z)
        prob_no = 1.0 - prob_yes

        # Minimum required gap to trade
        if abs(gap) < BASE_MIN_GAP_USD:
            return {"action": "HOLD"}

        # Track BTC price history for trend momentum / anti-knife check
        self.price_history.append((now, btc_mid))
        self.price_history = [(t_h, p_h) for t_h, p_h in self.price_history if now - t_h <= 45.0]

        older_prices = [p_h for t_h, p_h in self.price_history if now - t_h >= 3.0]
        if older_prices:
            ref_past_price = older_prices[-1]
            # Don't buy YES if BTC is currently dumping
            if gap > 0 and btc_mid < ref_past_price - 12.0:
                return {"action": "HOLD"}
            # Don't buy NO if BTC is currently pumping
            if gap < 0 and btc_mid > ref_past_price + 12.0:
                return {"action": "HOLD"}

        # Select direction with highest probability
        side = "YES" if gap > 0 else "NO"

        outcome_book = books.get(side, {})
        asks = outcome_book.get("asks", [])
        bids = outcome_book.get("bids", [])
        min_order_size = float(outcome_book.get("minOrderSize", 5.0))

        parsed_asks = _parse_book_levels(asks, reverse=False)
        parsed_bids = _parse_book_levels(bids, reverse=True)
        if not parsed_asks or not parsed_bids:
            return {"action": "HOLD"}

        best_ask = parsed_asks[0][0]
        best_bid = parsed_bids[0][0]

        # Book Consensus Agreement: Market book must favor our direction (bid >= 0.48)
        if best_bid < 0.48:
            return {"action": "HOLD"}

        # Spread Filter: Reject wide spreads (spread > 0.03) to prevent latency loss
        spread = best_ask - best_bid
        if spread > MAX_SPREAD_USD + 1e-9:
            return {"action": "HOLD"}

        # Price Range Filter: Trade where fee convexity and time decay are optimal
        if not (MIN_ENTRY_PRICE <= best_ask <= MAX_ENTRY_PRICE):
            return {"action": "HOLD"}

        # ----------------------------------------------------------------------
        # 6. Dynamic Position Sizing (Volume Maximizer & Cash Preserver)
        # ----------------------------------------------------------------------
        # Adaptive Risk Scaling: Use higher budget when bankroll is strong; scale down under drawdown to ensure survival
        rule_max_buy = float(rules.get("maximumBuyCashUsd", 5.0))
        adaptive_ratio = 0.92 if cash_usd >= 7.0 else (0.75 if cash_usd >= 4.5 else 0.55)
        available_cash_limit = cash_usd * adaptive_ratio
        max_spend_budget = min(rule_max_buy, MAX_BUY_CASH_CEILING, available_cash_limit)

        # Target Finish Sizing Governor: If approaching $1,000, size only what's needed
        remaining_vol_to_qualify = TARGET_VOLUME_USD - eligible_vol
        if remaining_vol_to_qualify < 20.0:
            needed_buy = max(2.5, (remaining_vol_to_qualify / 2.0) + 0.1)
            max_spend_budget = min(max_spend_budget, needed_buy)

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
        # Reject if instantaneous spread loss exceeds $0.065 per share
        if immediate_roundtrip_loss > 0.065 * optimal_shares + 1e-9:
            return {"action": "HOLD"}

        # ----------------------------------------------------------------------
        # 7. Issue Valid BUY Decision
        # ----------------------------------------------------------------------
        self.last_buy_attempt_at = now
        return {
            "action": "BUY",
            "outcome": side,
            "maxCashUsd": round(optimal_cash, 6),
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
