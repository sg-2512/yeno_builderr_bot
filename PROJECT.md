# YeNo × Builderr Quantitative Trading Bot: Project Architecture & System Specification

**Project**: YeNo × Builderr Volume Bot Challenge  
**Target Repository**: `c:/Users/Sanchit Gupta/yeno`  
**Primary Core Implementation**: `yeno_production_bot.py`  
**Evaluation Standard**: `yeno-evaluator-contract.md`  
**Document Version**: 1.0.0 (October 19, 2026)

---

## 1. System Overview

The **YeNo × Builderr Quantitative Trading Bot** is a high-frequency algorithmic trading system engineered for the official 24-hour YeNo × Builderr paper trading competition. The bot autonomously trades 288 consecutive 5-minute binary outcome markets on Bitcoin (Up/Down) via an event-driven JSON-over-HTTP evaluator protocol (`POST /decide`).

### Core Strategic Mandates
1. **$10.00 Starting Capital**: Trade within strict bankroll preservation constraints.
2. **Hard $5.00 BUY Cash Limit**: Sizing inclusive of all Polymarket dynamic taker fees ($0.07 \cdot p(1-p)$) and YeNo overlay fees ($0.01 \cdot p$).
3. **Single Open Position**: Zero pyramiding, zero hedging; strictly one outcome contract or pending BUY at any given instant.
4. **$1,000.00 Eligible Volume Target**: Accumulate completed roundtrip volume where volume is recognized only on fully closed BUY-then-SELL cycles.
5. **Terminal Flat Guarantee**: Liquidate all open positions flat before the market expiry freeze ($t \le 15$s), eliminating zero-volume expiry settlement forfeiture.
6. **Clean Crossing Terminal Halt**: Irreversibly lock into `ACTION_HOLD` upon reaching $\ge \$1,000$ volume while flat, preserving settled paper cash for Rank 1 competition standing.
7. **Sub-200ms Latency SLA**: Execute all decision logic in sub-millisecond timeframes (production benchmark: ~0.010 ms).

---

## 2. Repository Layout & Component Taxonomy

```
yeno/
├── yeno_production_bot.py             # PRODUCTION BOT ENGINE (HTTP Server, Microstructure, Sizing, State)
├── yeno-evaluator-contract.md         # AUTHORITATIVE COMPETITION SPECIFICATION
├── yeno-sample-observation.json       # CANONICAL OBSERVATION PAYLOAD SCHEMA
├── yeno-house-bot-v1.py               # Starter Kit Baseline House Bot
├── yeno-house-bot-benchmark.md        # Starter Kit Diagnostic Benchmark Results (30 Paths)
├── yeno-house-bot-configs.json        # House Bot Configuration Variants
├── yeno_simulator.py                  # Synthetic 24-Hour Market Replay Simulator
├── yeno-smoke-test.py                 # HTTP Schema & API Contract Plumbing Smoke Test
│
├── test_local_suite.py                # Comprehensive Local Unit & Contract Battery (22 Checks)
├── run_master_test.py                 # Multi-Tier Master Verification Script (Tiers 1–4)
├── harness_real_bot_evaluation.py     # True Causal Historical Backtest Harness (Direct Bot Invocation)
├── auto_tuning_engine.py              # Walk-Forward Hyperparameter Optimizer
├── test_new_features.py               # Feature Regression Checks (OBI, Micro-Price, Volatility)
├── test_technical_patterns.py         # Technical Analysis Verification (EMA, Slope, RSI)
│
├── evaluate_profitability.py          # Decoupled Heuristic Historical Script (Toy Simulation)
├── test_multi_datasets.py             # Decoupled Heuristic Multi-Dataset Script
├── run_multi_set_verification.py      # Decoupled Multi-Set Script + Sim Replay
├── download_and_evaluate_*.py         # Multi-Year Historical Downloader & Heuristic Scripts
│
├── bot_hyperparameters.json           # Tuned Production Hyperparameter Configuration
├── yeno_bot_state.json                # Runtime Atomic State Persistence Snapshot
├── TEST_READY.md                      # Test Readiness, Reproduction Commands & Empirical Metrics
├── PROJECT.md                         # This Document: System Architecture & File Layout
├── YENO_BOT_DEEP_AUDIT_REPORT.md      # Authoritative Quantitative Audit & Robustness Report
│
├── data_btc_historical.json           # 3,000 1-min BTC candles (50 hours / 600 rounds)
├── data_btc_unseen_set1.json          # 3,000 1-min BTC candles (Out-of-sample Set 1)
├── data_btc_unseen_set2.json          # 3,000 1-min BTC candles (Out-of-sample Set 2)
├── data_btc_unseen_set3.json          # 3,000 1-min BTC candles (Out-of-sample Set 3)
├── data_24h_day1.json                 # 1,440 1-min BTC candles (24h Benchmark Day 1)
├── ...                                # data_24h_day2.json through day7.json
└── .agents/teamwork/                  # Internal multi-agent teamwork metadata
```

---

## 3. High-Level Architecture & Execution Flow

```
                     ┌──────────────────────────────────────────────┐
                     │ Builderr / YeNo Evaluation Engine (POST)    │
                     └──────────────────────┬───────────────────────┘
                                            │ HTTP /decide
                                            ▼
                     ┌──────────────────────────────────────────────┐
                     │ DecideRequestHandler (yeno_production_bot.py)│
                     │  - Sets TCP_NODELAY                          │
                     │  - Suppresses stdout logging                 │
                     │  - Sub-15 microsecond dispatch               │
                     └──────────────────────┬───────────────────────┘
                                            │
                                            ▼
                     ┌──────────────────────────────────────────────┐
                     │ ProductionTradingBot.decide(observation)     │
                     └──────────────────────┬───────────────────────┘
                                            │
                     ┌──────────────────────┴───────────────────────┐
                     │                                              │
                     ▼                                              ▼
           [Position is Open]                             [Position is Flat]
                     │                                              │
       ┌─────────────┴─────────────┐                  ┌─────────────┴─────────────┐
       │ Latched Exit (exiting)    │                  │ Market Window (60s < t <= 240s)
       │ Goal Reached Exit         │                  │ Consecutive Loss Cooldown │
       │ Emergency Sweep (t <= 25s)│                  │ Re-entry Cooldown (2.0s)  │
       │ Reverse Signal Cut        │                  │ Reference Freshness (<1.5s)
       │ High-Gain Harvest (b>=0.94)                  │ Volatility Gap Threshold  │
       │ Forced Time Exit (t <= 50s)                  │ Spike Shield Filter       │
       │ Avellaneda-Stoikov Decay  │                  │ Momentum / EMA / RSI      │
       │ Stop-Loss Protection      │                  │ Book Consensus (b >= 0.48)│
       └─────────────┬─────────────┘                  │ Spread Filter (s <= 0.03) │
                     │                                │ Micro-Price & OBI Gate    │
                     │                                └─────────────┬─────────────┘
                     ▼                                              │
               [ACTION_SELL]                                        ▼
                     │                                ┌───────────────────────────┐
                     │                                │ Dynamic Position Sizing   │
                     │                                │  - Drawdown Floor (<$5)   │
                     │                                │  - Tiered Budget Scaling  │
                     │                                │  - Discrete Buy Sim       │
                     │                                │  - 250ms Headroom Buffer  │
                     │                                │  - Hard Cap <= $4.88      │
                     │                                └─────────────┬─────────────┘
                     │                                              │
                     │                                              ▼
                     │                                        [ACTION_BUY]
                     │                                              │
                     └──────────────────────┬───────────────────────┘
                                            │
                                            ▼
                     ┌──────────────────────────────────────────────┐
                     │ Asynchronous State Writer (_state_writer)   │
                     │  - Atomic write to yeno_bot_state.json.tmp   │
                     │  - os.replace() atomic rename                │
                     │  - RFC 8259 JSON compliance                  │
                     └──────────────────────────────────────────────┘
```

---

## 4. Key Component Specifications

### 4.1 Quantitative Microstructure Engine
- **Stoikov Volume-Weighted Micro-Price**: Evaluates top-2 L2 queue weight to detect quote tick direction before adverse execution:
  $$\hat{P}_{micro} = \frac{v_{bid} \cdot P_{ask} + v_{ask} \cdot P_{bid}}{v_{bid} + v_{ask}}$$
  Orders gated if $\hat{P}_{micro} < P_{mid} - 0.008$.
- **Cont Order Book Imbalance (OBI)**:
  $$OBI = \frac{v_{bid} - v_{ask}}{v_{bid} + v_{ask}} \in [-1, +1]$$
  Orders gated if $OBI < -0.30$.
- **Volatility Adaptation & Spike Shield**: Rolling sample standard deviation $\sigma_{dyn} \in [28, 120] \cdot p_{scale}$. Rejects flash wicks where $\max(P) - \min(P) \ge 2.5 \sigma_{dyn}$.
- **Spread & Immediate Exit Gate**: Rejects spreads $> \$0.03$. Simulates instant bid sweep to confirm roundtrip loss $\le \$0.075$/share.

### 4.2 Sizing & Fee Architecture
- **Hard Software Ceiling**: `MAX_BUY_CASH_CEILING = 4.88` USD.
- **Dynamic Fee Pre-Deduction**: Simulates exact Polymarket quadratic fee ($0.07 \cdot p(1-p)$) and YeNo overlay ($0.01 \cdot p$).
- **Headroom Buffer**: Allocates up to $\$0.015$/share (`BUY_HEADROOM_PER_SHARE`) to absorb 250ms execution latency quote jumps without dropping below `minOrderSize`.
- **Mathematical Guarantee**: `funded_cash = min(max_spend_budget, optimal_cash + headroom) <= 4.88 < 5.00`. Cash sizing is mathematically incapable of exceeding $5.00 under any condition.

### 4.3 Liquidation & Pre-Expiry Ladder
- **$t \le 60$s**: Entry cutoff (`ENTRY_MIN_SECONDS_TO_CLOSE`).
- **$t \le 50$s**: Forced time exit (`FORCED_EXIT_SECONDS`), dynamic reservation price decay.
- **$t \le 25$s**: Emergency market sweep (`EMERGENCY_EXIT_SECONDS`).
- **$t \le 15$s**: Evaluator entry freeze. Bot latched into `ACTION_SELL` on every tick until flat.

---

## 5. Verification Standards & Commands

```powershell
# 1. Local Unit & Contract Battery (22 checks, memory-isolated)
python test_local_suite.py

# 2. Master Verification Battery (Tiers 1-4)
python run_master_test.py

# 3. True Causal Historical Backtest Harness
python harness_real_bot_evaluation.py

# 4. HTTP Protocol Smoke Test (requires running bot server)
python yeno-smoke-test.py
```

---

## 6. Agent Workspace Governance

- Deliverable files (`YENO_BOT_DEEP_AUDIT_REPORT.md`, `TEST_READY.md`, `PROJECT.md`) reside exclusively at the repository root.
- The `.agents/teamwork/` directory contains strictly metadata (briefings, plans, progress logs, analysis, handoffs). No application code or test data is placed within `.agents/`.
