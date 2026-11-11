# SPX double-calendar paper trader for Railway

A worker that trades SPX double-calendar spreads on the Tradier PAPER
account (sandbox) under one fixed rule set. It is paper-only by
construction: the host is hardcoded to `sandbox.tradier.com`, so it cannot
reach a live account.

## The rules it implements

- SPXW double calendars only: sell the nearer expiry put and call, buy the
  next expiry at the same strikes.
- Strikes $50 each side of the current SPX price, rounded to the nearest
  5-point strike.
- Short leg 3-5 DTE at entry, long leg the next expiry after it. Any
  weekday pair qualifies.
- Entry window 8:00 a.m.-12:30 p.m. America/Los_Angeles, weekdays only,
  only when flat. No day skip: it evaluates every poll (3 minutes).
- Walk-up entry: when the cheapest pair previews at or under $12.00, it
  bids $9.00 and walks up $0.10 every 30 seconds to $12.00, cancelling
  and replacing at each step; a fill at any step opens the position.
  An unfilled cycle cancels and waits 5 minutes before the next cycle.
  Pairs over $12.00 are logged with their actual price and retried
  next poll.
- Max 5 contracts per trade (see CONTRACTS), one position at a time.
- Exit at a 10% gain on the debit basis. As soon as an entry fills, it
  places a GTC credit order to close the position at 110% of the debit
  paid (e.g. $16.06/share on a $14.60 entry), so the exit triggers the
  moment the market touches it — even between polls or overnight. The
  poll-based 10% exit is the fallback if no take-profit order is
  working. The standing order caps the gain at exactly 10%: it will
  not capture an overshoot above the line. There is no end-of-day
  forced close: a position that never reaches 10% stays open and
  blocks new entries until it does.
- Loss exit: close at the mid when the position's value falls to 50% of
  the debit paid (STOP_LOSS_FRACTION, default 0.50). Time exit: close
  when the short leg has 1 day or less to expiry, so a position that
  never reaches 10% cannot ride through short-leg expiry.
- No preview step: when the rules say enter or exit, the order is placed
  directly. Every placement is logged immediately.

## Deploy on Railway

1. Push `paper_trader.py` and `Procfile` to a repo.
2. Create a Railway project from that repo. The Procfile runs a worker:
   `python paper_trader.py`. The worker loops forever, so Railway keeps
   it running, and its stdout logs are the record.
3. Set environment variables in Railway:
   - `TRADIER_API_KEY` (required): a Tradier SANDBOX (paper) API key.
     Never a live key. Never commit it to the repo.
   - `ENABLE_TRADING` (default `0`): set to `1` only when you want real
     paper orders placed. With any other value the worker runs in dry-run
     mode: it evaluates the rules, logs exactly what it would place, and
     places nothing.
   - `CONTRACTS` (default `5`): contracts per trade, clamped to 1-5.
   - `MAX_DEBIT` (default `12.00`): the debit ceiling per share and the
     top of the walk-up ladder.
   - `WALK_START` (default `9.00`): the opening bid of a walk-up cycle.
   - `WALK_STEP` (default `0.10`): the price bump every 30 seconds.
   - `POLL_SECONDS` (default `180`): loop interval.
4. Deploy with dry-run first. Check the logs around the entry window and
   confirm the pairs, strikes, and debits look right before setting
   `ENABLE_TRADING=1`.

## P&L output

The worker prints a heartbeat line every poll (flat/open, SPX, state), a
P&L line (equity, realized P&L for the day, unrealized P&L, open leg
count) on the first poll of the 6:00 a.m. and 8:00 a.m. PT hours on
weekdays, plus a line for every entry, walk-up step, exit, skip, and
error. Use `TRADIER_API_KEY=xxx python paper_trader.py --check` for a one-shot P&L
snapshot without starting the loop.

## Safety notes

- Paper only, always. There is no flag, variable, or code path that
  points this program at the live host.
- Sandbox quotes are delayed about 15 minutes, so computed mids are
  estimates, not live marks.
- Day orders left unfilled at the close expire with the session.
  Positions are closed only on the 10% gain exit, so a position may be
  carried overnight by design.
- This code was not run against the Tradier API with a real key. Verify
  in dry-run mode first, especially the expirations and chains endpoints.

---

# QQQ double-calendar paper trader (`qqq_paper_trader.py`)

A second worker, same shape as the SPX one but for QQQ double calendars.
It runs as its own Railway service from the same repo, using the
`qqq-worker` Procfile process type. Paper-only by construction: the host
is hardcoded to `sandbox.tradier.com`.

## The rules it implements

- QQQ double calendars only: sell the nearer expiry put and call, buy the
  next expiry at the same strikes.
- Strikes $15 each side of the current QQQ price, rounded to the nearest
  1-point strike.
- Short leg 6 DTE at entry (any expiry 5-7 DTE qualifies), long leg the
  next expiry after the short leg (a 6/7 DTE pair when the short is 6
  DTE). Any weekday pair qualifies.
- Entry window 8:00 a.m.-12:30 p.m. America/Los_Angeles, weekdays only,
  only when flat. No day skip: it evaluates every poll (3 minutes).
- Walk-up entry: when the cheapest pair previews at or under
  MAX_DEBIT, it bids from max(WALK_START, modeled debit minus
  WALK_START_OFFSET) and walks up WALK_STEP every 30 seconds to
  MAX_DEBIT, cancelling and replacing at each step; a fill at any step
  opens the position. An unfilled cycle cancels and waits 5 minutes
  before the next cycle. Pairs over MAX_DEBIT are logged with their
  actual price and retried next poll.
- While holding a position it polls every HOLD_POLL_SECONDS (60
  seconds) so the 10% exit fires close to the line; while flat it
  polls every POLL_SECONDS (3 minutes).
- 10 contracts per trade (see CONTRACTS), one position at a time.
- Exit at a 10% gain on the debit basis. As soon as an entry fills, it
  places a GTC credit order to close the position at 110% of the debit
  paid (e.g. $2.01/share on a $1.83 entry), so the exit triggers the
  moment the market touches it — even between polls or overnight. The
  poll-based 10% exit is the fallback if no take-profit order is
  working. The standing order caps the gain at exactly 10%: it will
  not capture an overshoot above the line. There is no end-of-day
  forced close: a position that never reaches 10% stays open and
  blocks new entries until it does.
- Loss exit: close at the mid when the position's value falls to 50% of
  the debit paid (STOP_LOSS_FRACTION, default 0.50). Time exit: close
  when the short leg has 1 day or less to expiry, so a position that
  never reaches 10% cannot ride through short-leg expiry.
- No preview step: when the rules say enter or exit, the order is placed
  directly. Every placement is logged immediately.

## Why 6/7 DTE, not 6/12

The original spec was sell 6 DTE / buy 12 DTE with a $0.80 ceiling.
Tested against the live sandbox chain on 2026-11-07 (QQQ $656.10): the
6/12 pair (short Nov 16 / long Nov 20, 641P/671C) modeled at a $6.12
debit, and strike width barely moves it ($5-wide $6.59, $10 $6.41,
$15 $6.12, $20 $5.79). The 6-day expiry gap is the driver — the SPX
worker's gap is 1 day. A 6/7 DTE pair (next expiry) is the only variant
that gets near $0.80: roughly $1.19 at the elevated post-crash vol of
Nov 7 (VIX 25.64, 9-day gauge 27.18), ~$0.66 at normal ~20% vol. The
user chose 6/7 on 2026-11-07.

## Deploy on Railway

1. Push `qqq_paper_trader.py` and the updated `Procfile` to the repo.
2. In the same Railway project, add a second service from the repo and
   set its start command to the `qqq-worker` process type
   (`python qqq_paper_trader.py`). It needs its own environment
   variables — do not share the SPX service's:
   - `TRADIER_API_KEY` (required): a Tradier SANDBOX (paper) API key.
   - `ENABLE_TRADING` (default `0`): set to `1` only when you want real
     paper orders placed. Deploy with dry-run first.
   - `CONTRACTS` (default `10`): contracts per trade, clamped to 1-10.
   - `MAX_DEBIT` (default `1.50`): the debit ceiling per share and the
     top of the walk-up ladder.
   - `WALK_START` (default `0.60`): the floor of the opening bid of a
     walk-up cycle; the actual start is max(WALK_START, modeled debit
     minus WALK_START_OFFSET).
   - `WALK_START_OFFSET` (default `0.30`): how far below the modeled
     debit a walk-up cycle starts.
   - `WALK_STEP` (default `0.05`): the price bump every 30 seconds.
   - `STRIKE_WIDTH` (default `15`): dollars each side of QQQ.
   - `POLL_SECONDS` (default `180`): loop interval while flat.
   - `HOLD_POLL_SECONDS` (default `60`): loop interval while holding
     a position, so the 10% exit fires close to the line.
3. Orders are tagged `muse-qqq`, so the QQQ worker never cancels or
   collides with the SPX worker's `muse-spx` orders. Both workers trade
   the same paper account: they each enforce one position at a time for
   themselves, but they can each hold a position at the same time (one
   SPX calendar and one QQQ calendar).

# Nasdaq-100 double-calendar paper trader (`nasdaq_paper_trader.py`)

A third worker, same shape as the QQQ one but for double calendars on
the 20 largest Nasdaq-100 names by market cap. It runs as its own
Railway service from the same repo, using the `nasdaq-worker` Procfile
process type. Paper-only by construction: the host is hardcoded to
`sandbox.tradier.com`.

## The rules it implements

- Double calendars on 20 tickers only (TICKERS, default):
  NVDA, AAPL, MSFT, AMZN, GOOGL, TSLA, AVGO, META, MU, AMD, ASML,
  COST, PLTR, CSCO, INTC, AZN, NFLX, AMAT, PANW, LRCX. One position
  per ticker at a time; each ticker is managed independently, so up to
  20 positions can be open at once. Each ticker's orders carry their
  own tag (`muse-nasdaq-<TICKER>`), so a pending order for one ticker
  never blocks another — and never collides with the SPX (`muse-spx`)
  or QQQ (`muse-qqq`) workers' orders.
- Sell the nearer expiry put and call, buy the next expiry at the same
  strikes.
- Strikes 2% each side of the current price (STRIKE_WIDTH_PCT,
  default 0.02), snapped to the nearest strike actually listed in the
  chain — a $1 grid does not exist for every name (ASML/COST list $5
  strikes), so the worker takes the closest listed strike to each
  target instead of assuming a grid.
- Short leg 5-7 DTE at entry (SHORT_DTE_MIN/MAX), long leg the next
  expiry after the short leg. Consequence: names with Friday-only
  weeklies (ASML, COST, PLTR, CSCO, INTC, NFLX, AMAT, PANW, LRCX)
  only qualify on Fridays, when the next Friday is 7 DTE out; AZN has
  monthlies only and almost never qualifies. The Mon/Wed/Fri names
  (NVDA, AAPL, MSFT, AMZN, GOOGL, TSLA, AVGO, META, MU, AMD) qualify
  most days.
- Entry window 8:00 a.m.-12:30 p.m. America/Los_Angeles, weekdays only,
  only when that ticker is flat. No day skip: it evaluates every poll.
- Walk-up entry is gone (replaced Nov 10): one bid per cycle, at the
  spread's bid — no ceiling, no walk-up. While a ticker is flat and in
  the window, it places a single debit limit order at the spread's bid
  (long-leg bids minus short-leg asks). The bid is the price, full
  stop. A bid that does not fill is cancelled next cycle and re-placed
  at the then-current bid; the price only ever moves once per poll
  cycle. A bid left working when its pair stops qualifying is
  cancelled as stale. A fill shows up as a position next poll, and the
  per-poll check then places the GTC take-profit.
- Liquidity gate, two-tier: the SHORT legs (front expiry) must have
  traded at least MIN_LEG_VOLUME contracts today (default 100), and
  the LONG legs (next expiry) at least MIN_LONG_LEG_VOLUME (default
  10). Volume concentrates in the front expiry — AAPL's Nov 18 legs
  traded 15 and 74 while the Nov 16 legs traded 200 and 2,791 — so one
  floor on all four legs would block liquid names outright. The skip
  line reports "low-volume" when a candidate fails the gate.
- 1 contract per trade (CONTRACTS), per ticker.
- Exit at a 10% gain on the debit basis. As soon as an entry fills, it
  places a GTC credit order to close the position at 110% of the debit
  paid, so the exit triggers the moment the market touches it — even
  between polls or overnight. The poll-based 10% exit is the fallback
  if no take-profit order is working. The standing order caps the gain
  at exactly 10%: it will not capture an overshoot above the line.
  There is no end-of-day forced close: a position that never reaches
  10% stays open and blocks new entries for that ticker until it does.
- Loss exit: close at the mid when the position's value falls to 50% of
  the debit paid (STOP_LOSS_FRACTION, default 0.50). Time exit: close
  when the short leg has 1 day or less to expiry, so a position that
  never reaches 10% cannot ride through short-leg expiry. A close with
  a negative value goes out as a DEBIT order for the absolute value —
  Tradier rejects negative-price credit orders, which left the Nov 10
  SPX position with no working order after its stop-loss fired.
- Exits are checked for every holding ticker before any entry walk-up
  starts in a cycle, so a walk-up can never delay a stop-loss.
- While holding any position it polls every HOLD_POLL_SECONDS (120
  seconds) so exits fire close to the line; while flat it polls every
  POLL_SECONDS (600 seconds / 10 minutes) — one bid per cycle, so a
  bid is refreshed at most every 10 minutes.
- No preview step: when the rules say enter or exit, the order is placed
  directly. Every placement is logged immediately.

## Deploy on Railway

1. Push `nasdaq_paper_trader.py` and the updated `Procfile` to the repo.
2. In the same Railway project, add a third service from the repo and
   set its start command to the `nasdaq-worker` process type
   (`python nasdaq_paper_trader.py`). It needs its own environment
   variables — do not share the SPX/QQQ services':
   - `TRADIER_API_KEY` (required): a Tradier SANDBOX (paper) API key.
     Reference the worker service's key (`${{worker.TRADIER_API_KEY}}`)
     rather than pasting a key.
   - `ENABLE_TRADING` (default `0`): set to `1` only when you want real
     paper orders placed. Deploy with dry-run first.
   - `TICKERS` (default the 20 above): comma-separated tickers.
   - `CONTRACTS` (default `1`): contracts per trade, clamped to 1-10.
   - `STRIKE_WIDTH_PCT` (default `0.02`): fraction each side of price.
   - `MIN_LEG_VOLUME` (default `100`): minimum contracts traded today
     for a SHORT (front-expiry) leg; `MIN_LONG_LEG_VOLUME` (default
     `10`): the same floor for a LONG (next-expiry) leg.
   - `POLL_SECONDS` (default `600`): loop interval while flat — one
     bid per cycle, so a bid is refreshed at most every 10 minutes.
   - `HOLD_POLL_SECONDS` (default `120`): loop interval while holding
     any position, so exits fire close to the line.
3. Orders are tagged `muse-nasdaq-<TICKER>`, so this worker never
   cancels or collides with the SPX worker's `muse-spx` or the QQQ
   worker's `muse-qqq` orders. All three workers trade the same paper
   account: each enforces one position at a time for itself (per ticker
   for this worker), but they can each hold positions at the same time.

## SPX trend vertical spreads (`spx_trend_paper_trader.py`, service `spx-trend-worker`)

A fourth lane, added Nov 11 at the user's request: SPX 0DTE vertical
credit spreads whose direction follows the trend, with every position
closed 5 minutes before the market close.

- Trend: 9/21 EMA crossover on 15-minute SPX bars, seeded from the prior
  sessions' bars so the read is valid from the open. (VWAP is NOT used:
  SPX is an index and Tradier's timesales returns volume 0 for it, so a
  volume-weighted average cannot be computed from this feed.)
- Bullish (EMA 9 above EMA 21) -> bull put spread; bearish -> bear call
  spread. No trade when the EMAs are equal or there are too few bars.
- Spreads are $5 wide, 1 contract, short leg ~0.5% out of the money
  (snapped to the nearest listed $5 strike), long leg $5 further out.
  Both legs need a two-sided quote and at least 10 contracts traded;
  the mid-based credit must sit in [$0.15, $2.50].
- Entry window 7:00 a.m.-12:00 p.m. PT (10:00 a.m.-3:00 p.m. ET),
  weekdays. One position at a time. One limit credit order at the
  spread's mid; it stays working while the trend still matches and is
  cancelled if the trend flips, the strikes move, or the window ends.
  A trend flip while holding does NOT close and reverse.
- Exits (poll-based, every 60 s): take profit when the buy-back cost
  falls to 50% of the credit received; stop when it reaches 2x the
  credit. No standing GTC take-profit.
- HARD EXIT at 12:55 p.m. PT (3:55 p.m. ET): any open position is
  closed at the mid, whatever its P&L. The check runs every poll, so a
  failed close is retried. No position is ever carried overnight.
- Polls every 60 s. Orders are tagged `muse-spx-trend`. A credit order
  never blocks a debit placement (the stale-GTC fix from commit
  170eda59), so a stale working entry cannot block a close.
- Deploy dry-run first (`ENABLE_TRADING` unset/0); flip to `1` only on
  the user's instruction.

## Cross-worker position isolation (Nov 11 fix)

All four workers trade the same paper account, and Tradier positions
carry no tag — so a worker that reads "all SPXW positions" sees the
other lanes' spreads too. On Nov 11 the SPX worker's time exit (close
when a short leg expires within 1 day) fired on the trend worker's
first 0DTE spread and closed it 68 seconds after its fill, at a $0.10
loss, because a 0DTE short leg expires "within 1 day". The trend
worker then re-entered and the same thing was about to happen again.

Fix (commit, Nov 11): each worker only manages its own legs.
- SPX worker (`paper_trader.py`): `position_value_and_basis` returns
  only legs expiring after today (its double calendar: short 3-5 DTE,
  long next expiry). A 0DTE vertical expires today and is invisible
  to it. Its stale-take-profit cleanup likewise only cancels credit
  orders whose legs all expire after today, so it never cancels the
  trend worker's working entry.
- Trend worker (`spx_trend_paper_trader.py`): `position_value_and_basis`
  returns only legs expiring today (its 0DTE vertical). A multi-day
  calendar is invisible to it.
- QQQ (QQQ root) and Nasdaq (single names) never shared a symbol
  prefix with SPXW, so they needed no change.

## Leg-collision avoidance (Nov 11)

The trend worker checks the paper account's open SPXW positions
before entering. If either leg's strike is already held by another
strategy, the whole spread shifts one $5 step further out of the
money — down for a bull put spread, up for a bear call spread — and
is re-checked, up to `MAX_SHIFT_STEPS` (default 3). If every nearby
strike is held, the entry is skipped rather than doubling up on a
leg. Example: it wants 7580/7575 but 7580 is already open, so it
trades 7575/7570 instead. The credit and volume checks still apply
to the shifted legs.

## Trend worker minimum credit (Nov 11)

The trend worker's minimum credit is now $1.00 per share (was
$0.15). The credit window is [$1.00, $2.50]. A spread whose
mid-based credit is below $1.00 is skipped. This is the code
default (`MIN_CREDIT`, overridable by the env var of the same name).
