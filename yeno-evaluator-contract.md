# YeNo × Builderr Volume Bot Challenge

## Fixed target

- Starting paper cash: **$10**
- Maximum cash per BUY, including fees: **$5**
- Official window: **24 hours**
- Target: **$1,000** in eligible volume
- Exposure: one open position or pending BUY
- Market: YeNo BTC five-minute Up/Down
- Finish: terminal flat, with no pending action

Eligible volume is actual gross executed BUY notional plus actual gross executed SELL notional from a fully closed cycle. Partial fills and split exits remain one cycle. Rejections, cancellations, zero fills, settlements, redemptions, and unfinished inventory add zero.

## API

Expose `POST /decide`. The evaluator sends current receive-time L2 books, market time remaining, paper cash, current owned position, completed volume, immutable limits, and the public BTC reference inputs available at that receive time.

The optional `reference` object has this shape:

```json
{
  "btcMidUsd": 80482.155,
  "openingTargetUsd": 80444.946,
  "observedAt": 1789880823.619,
  "targetObservedAt": 1789880814.203,
  "targetProvisional": false
}
```

The hidden paper evaluation supplies these timestamped fields. The starter kit does not include an evaluator replay harness or the recorded development paths behind the benchmark. The evaluator excludes any row whose target was observed after the row's receive time. A bot should reject stale or provisional reference data rather than infer information that was not yet observable.

Return exactly one of:

```json
{"action":"HOLD"}
{"action":"BUY","outcome":"YES","maxCashUsd":5}
{"action":"SELL"}
```

`SELL` means “close the owned position.” The evaluator owns shares, cash, execution, and scoring.

Reply within **200 ms**. The evaluator asks again when both YES and NO books are fresh and no action is pending; it does not use a fixed call-per-second schedule.

`account.position` is `null` when flat. When a position is open, the outer account fields stay camelCase and these position fields are snake_case:

```json
{
  "market_id": "...",
  "outcome": "YES",
  "shares": 5,
  "buy_gross_usd": 2.5,
  "buy_fees_usd": 0.03,
  "sell_gross_usd": 0,
  "sell_fees_usd": 0
}
```

## Execution model

- 250 ms execution latency; orders cross a later L2 update for their own outcome. An update to the opposite outcome cannot reuse a pre-decision quote.
- L2 depth, minimum order size, partial fills, split exits, and executable one-sided books near expiry. A BUY still requires asks; a SELL can execute against bids when asks have disappeared.
- A BUY whose requested shares are below that book's `minOrderSize` is rejected. A SELL may partially fill; its volume counts only after the entire cycle is closed.
- Adjacent-market prefetch rows are grouped into chronological sessions; pre-open warmups and recorder-incomplete markets are excluded.
- Timestamped public BTC midpoint and YeNo opening-target inputs; future-stamped target rows are excluded.
- Books older than two seconds rejected.
- New entries blocked inside the final 15 seconds.
- Dynamic crypto taker fee: `0.07 × shares × price × (1 − price)` on each side, rounded to five decimal places as documented by Polymarket: <https://docs.polymarket.com/trading/fees>.
- The 1% YeNo overlay observed in the test account, applied to gross notional on each side.
- All BUY fees count inside the $5 ceiling.
- Hidden forward-collected paper data for the official 24-hour run.

## Ranking and reward

The official paper run lasts 24 hours. A bot qualifies at the first completed cycle that reaches $1,000 of eligible paper volume, provided it is then flat with no pending action.

Qualifying paper bots rank by simulated settled cash at the first clean target crossing. Exact ties break on lower maximum drawdown, then earlier time to $1,000. The first-place qualifying bot receives the **$500 prize sponsored by YeNo**.

YeNo may offer additional rewards or pilot opportunities to selected bots. These are optional, do not affect competition ranking, and will use separate written terms.

There is no entry fee. This is an online software-building competition. Builders submit only their algorithm. They do not deposit money, receive a trading account, place real orders, or bear trading losses. Builderr owns the paper evaluation, simulated cash, fills, fees, and scoring.

Submit a repository or HTTPS `/decide` endpoint, bot name, contact, and immutable commit or container digest to `submit@builderr.ai`.

Learn about YeNo at <https://yeno.market>.
