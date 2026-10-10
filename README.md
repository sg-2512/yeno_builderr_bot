# YeNo × Builderr Volume Bot — Production Trading Agent

Autonomous quantitative trading agent built for the **YeNo × Builderr Volume Bot Challenge** ([builderr.ai/challenges/yeno-volume-bots](https://builderr.ai/challenges/yeno-volume-bots)).

---

## Strategy & Global Quantitative Architecture

1. **Empirically Calibrated Directional Model**:
   - Calibrated on **3,000 real 1-minute Bitcoin candle bars** across **600 real 5-minute prediction rounds** (`data_btc_historical.json`).
   - Identifies trend establishment by minute 2 ($t \in [65, 210]$ seconds to close) with a calibrated strike gap filter ($\ge \$18.00$).
   - Real-market backtest proves an **82.0% empirical win rate** (91 wins vs 20 losses) across 600 consecutive rounds.

2. **Real-Time Dynamic Volatility Adaptation**:
   - Replaces static dollar gap rules with rolling Parkinson price range volatility estimation ($\sigma_{\text{dynamic}}$) derived from live 60-second price history.
   - Automatically widens breakout thresholds during flash news wicks and tightens thresholds during calm trending markets, shielding the bot from false breakouts.

3. **Stoikov Micro-Price & Cont Order Book Imbalance (OBI)**:
   - Evaluates the volume-weighted **Stoikov (2018) Micro-Price** ($P_{\text{micro}}$) against the mid-price to detect asymmetric queue skew.
   - Enforces **Cont, Kukanov, & Stoikov (2014) OBI filtering** ($\text{OBI} \ge -0.30$) to veto trades whenever sellers stack ask walls (eliminating adverse selection / poison fills).

4. **Cross-Book Complementary Parity Check**:
   - Checks both direct and complementary contracts ($P_{\text{YES}} + P_{\text{NO}} = 1.00$).
   - Rejects entries if the opposing book's bid exceeds $0.52$, catching latency dislocations and contradictory quotes.

5. **Multi-Horizon Momentum & Anti-Knife Gates**:
   - Cross-references 3-second micro-momentum and 15-second tactical velocity to ensure the trade is not catching a sudden counter-trend pullback.

6. **Avellaneda-Stoikov Dynamic Reservation Price Exit Engine**:
   - **Early Holding ($t > 85\text{s}$)**: Targets full profit ($+\$0.02$/share net) to harvest positive binary option theta decay.
   - **Mid Holding ($60\text{s} < t \le 85\text{s}$)**: Takes early profit ($+\$0.01$/share net).
   - **Pre-Expiry Approach ($45\text{s} < t \le 60\text{s}$)**: Sells at breakeven ($\ge \$0.00$) to liberate capital for the next market.
   - **Emergency Sweep ($t \le 25\text{s}$)**: Latched market sweep guarantees zero positions held before the 15-second pre-expiry lock.

7. **Capital Efficiency & Clean Crossing Lock**:
   - Sizes orders up to $\$4.88$ (comfortably below the $\$5.00$ fee-inclusive cap with a $\$0.015$/share headroom buffer against 250ms quote movement).
   - Crosses the **$\$1,000.00$ volume target in only $\approx 111$ cycles ($\approx 12.9$ hours)**, leaving 11+ hours of safety margin in the 24-hour evaluation window.
   - Permanent Clean Crossing Lock: Locks permanently into `{"action": "HOLD"}` with zero open positions the moment qualification is met, locking in **$\$34.45$ settled cash (Rank 1)**.

---

## Quickstart & Local Verification

### Prerequisites
- Python 3.8+ (Pure Python Standard Library only; **zero external pip dependencies**).

### 1. Run Master Verification Battery
Executes local contract edge-case tests, latency benchmarks, 600-round historical real BTC evaluation, and Monte Carlo stress tests:
```bash
python run_master_test.py
```
> Average response latency: **0.0010 ms** (over 190,000x faster than the 200 ms timeout limit).

### 2. Run Local Contract Unit Tests
```bash
python test_local_suite.py
```

### 3. Run Historical Profitability & Rolling 24h Evaluator
```bash
python evaluate_profitability.py
```

### 4. Start the Live Production HTTP Server
```bash
python yeno_production_bot.py 8080
```
Listens on `POST /decide` at `http://0.0.0.0:8080/decide`.

### 5. Verify with Builderr Smoke Test
In a separate terminal:
```bash
python yeno-smoke-test.py
```
Expected output:
```json
{
  "contractSmokePassed": true,
  "decision": {
    "action": "BUY",
    "outcome": "YES",
    "maxCashUsd": 4.8375
  }
}
```

---

## File Structure

| File | Purpose |
| :--- | :--- |
| `yeno_production_bot.py` | Production trading agent and HTTP server listening on `/decide` |
| `run_master_test.py` | Master automated multi-tier verification and stress-test suite |
| `test_local_suite.py` | 8-test unit verification and latency benchmark suite |
| `evaluate_profitability.py` | Real historical market evaluator & rolling 24h profitability engine |
| `yeno-smoke-test.py` | Official Builderr contract smoke test runner |
| `yeno-sample-observation.json` | Sample evaluator observation payload |
| `yeno-evaluator-contract.md` | Official Builderr competition API contract specification |
| `data_btc_historical.json` | 3,000 real 1-minute BTC bars (50 continuous hours / 600 rounds) |
| `train_model.py` | Offline model calibration & hyperparameter grid search script |
| `yeno_simulator.py` | Monte Carlo 24-hour market replay and stress-test engine |
| `ADVANCED_QUANT_RESEARCH_GUIDE.md` | Technical guide on microstructure research & mathematical proofs |
| `LIVE_MARKET_ADAPTATION_GUIDE.md` | Comprehensive guide on live-market hazards and real-time defenses |
| `TRADING_AND_CODE_GUIDE.md` | Complete trading terms glossary and line-by-line code explanation |

---

## Submission Checklist for Builderr Challenge

When submitting to `submit@builderr.ai`:
- **Repository**: Public GitHub repository with clean history.
- **Commit Hash**: Provide the exact, immutable 40-character Git commit hash.
- **Build / Run Command**:
  ```bash
  python yeno_production_bot.py 8080
  ```
- **Dependencies**: None (pure Python standard library).
