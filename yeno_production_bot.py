#!/usr/bin/env python3
"""YeNo Production Trading Bot for the YeNo x Builderr Volume Bot Challenge.

Designed to strictly follow the Builderr Evaluator Contract:
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
MAX_BUY_CASH_CEILING = 4.88            # Buffer below $5.00 fee-inclusive cap
CASH_UTILIZATION_RATIO = 0.94          # Max fraction of available cash to risk per BUY
TARGET_VOLUME_USD = 1000.0             # Official target volume
MAX_SPREAD_USD = 0.03                  # Strict spread filter (avoid paying wide spreads)
MAX_ENTRY_PRICE = 0.88                 # Don't buy overly priced/asymmetric contracts
MIN_ENTRY_PRICE = 0.15                 # Don't buy ultra-low probability lotteries
BASE_MIN_GAP_USD = 16.0                # Minimum BTC midpoint vs strike gap
MAX_CYCLES_PER_MARKET = 4              # Max completed roundtrips in a single 5m market
ENTRY_MAX_SECONDS_TO_CLOSE = 240.0     # Trade actively throughout the 5m market
ENTRY_MIN_SECONDS_TO_CLOSE = 55.0      # Stop entering early enough to guarantee flat exit
FORCED_EXIT_SECONDS = 42.0             # Standard exit threshold
EMERGENCY_EXIT_SECONDS = 25.0          # Hard emergency exit: sweep bids before 15s freeze
REENTRY_COOLDOWN_SECONDS = 1.5         # Wait between trades to avoid whipsaws
MAX_REFERENCE_AGE_SECONDS = 1.5        # Max staleness of BTC reference data
STOP_LOSS_PER_SHARE_USD = 0.08         # Dynamic stop loss per share
MIN_PROFIT_PER_SHARE_USD = 0.0         # Sell at breakeven: lock in volume at zero net cost


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
        """Entry point for /decide. Fully exception-safe."""
        try:
            return self._decide_impl(observation)
        except Exception:
            # Under any unexpected payload structure, safely default to HOLD
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
        # If target volume has been reached and no position is open, LOCK IN FLAT!
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

            # If we reached volume target while holding, priority #1 is to flatten immediately!
            if eligible_vol >= TARGET_VOLUME_USD:
                return {"action": "SELL"}

            outcome_book = books.get(side, {})
            bids = outcome_book.get("bids", [])

            # Emergency Time Exit: If market is about to enter final 15s freeze, SELL NOW!
            if seconds_to_close <= EMERGENCY_EXIT_SECONDS:
                return {"action": "SELL"}

            executable = _estimate_sell(bids, shares)
            if executable is not None:
                sell_gross, sell_fees = executable
                paid = float(position.get("buy_gross_usd", 0.0)) + float(position.get("buy_fees_usd", 0.0))
                already_received = float(position.get("sell_gross_usd", 0.0)) - float(position.get("sell_fees_usd", 0.0))
                full_cycle_pnl = already_received + sell_gross - sell_fees - paid

                # Condition A: Take-Profit (Cover fees + positive profit per share)
                if full_cycle_pnl >= (MIN_PROFIT_PER_SHARE_USD * shares) - 1e-9:
                    return {"action": "SELL"}

                # Condition B: Stop-Loss (Limit drawdown to preserve cash)
                if full_cycle_pnl <= -(STOP_LOSS_PER_SHARE_USD * shares) + 1e-9:
                    return {"action": "SELL"}

                # Condition C: Standard Time Forced Exit
                if seconds_to_close <= FORCED_EXIT_SECONDS:
                    return {"action": "SELL"}
            else:
                # If exact depth is not visible but time is running out (under 45s), trigger SELL
                # so the evaluator can match against incoming depth before the market closes!
                if seconds_to_close <= 45.0:
                    return {"action": "SELL"}

            return {"action": "HOLD"}

        # ----------------------------------------------------------------------
        # 4. Entry Filters (When Flat)
        # ----------------------------------------------------------------------
        # Rule check: Evaluator blocks entries inside the final 15 seconds;
        # We enforce a safer window (45s to 120s) so we always have time to exit cleanly.
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
        # 5. Direction & Quant Edge Calculation
        # ----------------------------------------------------------------------
        btc_mid = float(reference["btcMidUsd"])
        target_usd = float(reference["openingTargetUsd"])
        gap = btc_mid - target_usd

        # Dynamic gap threshold: Requires slightly larger gap if further from close
        time_factor = math.sqrt(seconds_to_close / 60.0)
        required_gap = BASE_MIN_GAP_USD * max(1.0, time_factor)
        if abs(gap) < required_gap:
            return {"action": "HOLD"}

        # Record BTC observation for trend persistence check
        self.price_history.append((now, btc_mid))
        self.price_history = [(t_h, p_h) for t_h, p_h in self.price_history if now - t_h <= 45.0]

        # Check that BTC is persistent and not reversing against our direction
        older_prices = [p_h for t_h, p_h in self.price_history if now - t_h >= 3.0]
        if older_prices:
            ref_past_price = older_prices[-1]
            if gap > 0 and btc_mid < ref_past_price - 12.0:
                return {"action": "HOLD"}
            if gap < 0 and btc_mid > ref_past_price + 12.0:
                return {"action": "HOLD"}

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

        # Book Favorite Agreement: Order book must confirm market consensus (bid >= 0.48)
        if best_bid < 0.48:
            return {"action": "HOLD"}

        # Spread Filter: Wide spreads guarantee instant loss under 250ms latency
        spread = best_ask - best_bid
        if spread > MAX_SPREAD_USD + 1e-9:
            return {"action": "HOLD"}

        # Price Range Filter
        if not (MIN_ENTRY_PRICE <= best_ask <= MAX_ENTRY_PRICE):
            return {"action": "HOLD"}

        # ----------------------------------------------------------------------
        # 6. Dynamic Position Sizing (Volume Maximizer)
        # ----------------------------------------------------------------------
        # Max fee-inclusive cash allowed by rules ($5.00) and available cash
        rule_max_buy = float(rules.get("maximumBuyCashUsd", 5.0))
        available_cash_limit = cash_usd * CASH_UTILIZATION_RATIO
        max_spend_budget = min(rule_max_buy, MAX_BUY_CASH_CEILING, available_cash_limit)

        # Target Finish Sizing Governor: If approaching $1,000, don't over-risk
        remaining_vol_to_qualify = TARGET_VOLUME_USD - eligible_vol
        if remaining_vol_to_qualify < 20.0:
            needed_buy = max(2.5, (remaining_vol_to_qualify / 2.0) + 0.1)
            max_spend_budget = min(max_spend_budget, needed_buy)

        if max_spend_budget < 1.0:
            return {"action": "HOLD"}

        # Calculate optimal shares to maximize volume while remaining within max_spend_budget
        # Start from min_order_size and scale up
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

        # Check immediate exit depth: Ensure bid side can absorb these shares without catastrophic slippage
        immediate_exit = _estimate_sell(bids, optimal_shares)
        if immediate_exit is None:
            return {"action": "HOLD"}

        exit_gross, exit_fees = immediate_exit
        immediate_roundtrip_loss = optimal_cash - (exit_gross - exit_fees)
        # Reject if the instantaneous bid-ask spread loss exceeds $0.065 per share
        # (House bot allowed $0.40 for 5 shares = $0.08/share)
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
            # Evaluator contract: safe fallback
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
