# YeNo × Builderr starter kit

Build a trading agent that returns `HOLD`, `BUY`, or `SELL`. YeNo is looking for bots that can generate trading volume while controlling fees, spreads, and losses. This is a free global bot-building competition sponsored by [YeNo](https://yeno.market). You submit code; you do not deposit money, receive a trading account, or place real trades. Builderr supplies the market data, simulated cash, execution, fees, and scoring.

## The challenge

Your bot starts with $10 in simulated cash. It has 24 hours to produce at least $1,000 in completed BUY plus SELL volume. Each BUY may spend at most $5 including fees. Hold at most one position or pending BUY. A bot qualifies when a completed cycle first reaches $1,000 and the account is then flat with no pending action; the evaluator stops at that clean crossing.

## Files

- `yeno_competition_starter_bot.py`: minimal working server that always returns `HOLD`.
- `yeno-house-bot-v1.py`: strongest validated development baseline.
- `yeno-house-bot-configs.json`: the runnable House Bot v1 settings plus two clearly labeled development-only reference configurations. Those references are not switches for House Bot v1.
- `yeno-sample-observation.json`: one valid `/decide` request.
- `yeno-smoke-test.py`: checks your local endpoint's response shape and $5 BUY cap.
- `yeno-evaluator-contract.md`: exact input, output, execution, scoring, and reward rules.
- `yeno-house-bot-benchmark.md`: results, limits, and improvement opportunities.

## Start here: three steps

You need Python 3. The starter uses only the Python standard library. You do not need a YeNo account, API key, wallet, or deposit.

1. Open a terminal in this folder and start the minimal bot:

```bash
python3 yeno_competition_starter_bot.py
```

It starts a local API at `http://127.0.0.1:8080/decide`.

2. Leave that terminal running. In a second terminal in this folder, send the included sample market-data request:

```bash
python3 yeno-smoke-test.py
```

You should see `"contractSmokePassed": true`. This proves only that your endpoint can receive a request and return a valid decision; it is not a competition score.

3. Change the `decide` function in `yeno_competition_starter_bot.py`. Keep the endpoint at `POST /decide`, then rerun the local test.

If your computer uses `python` instead of `python3`, use `python` in both commands.

## What the test API does

`POST /decide` is the only API your bot needs. Locally, `yeno-smoke-test.py` sends `yeno-sample-observation.json` to it. In the official paper run, Builderr sends the same request shape with unseen market data. There is no separate YeNo sandbox account or API key to request.

Your bot returns one decision:

```json
{"action":"HOLD"}
```

Use `yeno-evaluator-contract.md` when you are ready to return `BUY` or `SELL`: it defines the full request, allowed responses, fees, fills, scoring, and prize rules.

## Build and submit

Change the strategy logic. Do not change evaluator-owned cash, fills, fees, volume, or position accounting.

The kit includes a local API smoke test, not a replay harness or the 30 development paths behind the benchmark. You can test your own logic locally, but the official score comes only from Builderr's unseen forward 24-hour paper run. The published benchmark is development evidence, not a reproducible entrant test or an expected result.

The first-place qualifying bot wins $500. YeNo sponsors the prize and may offer additional rewards or pilot opportunities to selected bots under separate written terms.

## Submit

Email `submit@builderr.ai` with your bot name, contact, repository or HTTPS `/decide` endpoint, and immutable commit or container digest.
