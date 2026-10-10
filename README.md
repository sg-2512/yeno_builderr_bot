# YeNo × Builderr Volume Bot — Production Trading Agent

Production trading agent designed for the **YeNo × Builderr Volume Bot Challenge** ([builderr.ai/challenges/yeno-volume-bots](https://builderr.ai/challenges/yeno-volume-bots)).

## Strategy Highlights

1. **Dynamic Capital Sizing ($4.88 Cap)**:
   - Sizing dynamically adjusts up to $\min(\text{cash} \times 0.94, \$4.88)$, generating $\approx \$9.50$ volume per cycle.
   - Reaches the $\$1,000$ volume target in $\approx 105$ cycles instead of 200+ cycles.

2. **Trend Persistence & Book Consensus Filtering**:
   - Compares BTC midpoint against the opening strike target while tracking real-time price persistence across consecutive ticks to filter out choppy reversals.
   - Enforces Order Book Consensus: requires `best_bid >= 0.48` for the predicted outcome to avoid adverse selection.

3. **Two-Layer Dynamic Fee Accounting**:
   - Accurately computes the Polymarket dynamic crypto taker fee ($0.07 \times \text{shares} \times p \times (1-p)$) plus the YeNo 1.0% overlay.
   - Evaluates net cycle PnL in real-time.

4. **Tiered Fail-Safe Exit Engine**:
   - **Take-Profit**: Locks in breakeven/profitable cycles immediately to accumulate eligible volume.
   - **Stop-Loss**: Protects bankroll against sudden reversals (cuts losses before severe drawdown).
   - **Forced Time Exit**: Initiates exits at $t \le 42\text{s}$.
   - **Emergency Sweep**: At $t \le 25\text{s}$, forcefully requests `SELL` to guarantee 0 positions held before the 15-second pre-expiry freeze.

5. **Clean Crossing Lock**:
   - Tracks cumulative eligible volume. As volume nears $\$1,000$, sizes the final trade to cross the target cleanly.
   - Once total eligible volume $\ge \$1,000$ and the position closes, permanently locks into `HOLD` to ensure terminal-flat qualification.

---

## Quickstart

### Prerequisites
- Python 3.8+ (Standard Library only; zero external pip dependencies).

### 1. Start the Bot Server
```bash
python yeno_production_bot.py 8080
```
Listens on `POST /decide` at `http://127.0.0.1:8080/decide`.

### 2. Verify with Local Smoke Test
In a separate terminal:
```bash
python yeno-smoke-test.py
```
Output:
```json
{
  "contractSmokePassed": true,
  "decision": {
    "action": "BUY",
    "outcome": "YES",
    "maxCashUsd": 4.7025
  }
}
```

---

## File Structure

- `yeno_production_bot.py`: Production trading bot and HTTP server.
- `yeno-smoke-test.py`: Official contract smoke test runner.
- `yeno-sample-observation.json`: Sample evaluator observation payload.
- `yeno-evaluator-contract.md`: Exact Builderr competition rules and API contract.
- `yeno_simulator.py`: Local Monte Carlo 24-hour evaluation and stress-test harness.
