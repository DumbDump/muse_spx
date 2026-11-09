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
- Entry window 6:45 a.m.-12:30 p.m. America/Los_Angeles, weekdays only,
  only when flat. No day skip: it evaluates every poll (3 minutes).
- Walk-up entry: when the cheapest pair previews at or under $11.00, it
  bids $9.00 and walks up $0.10 every 30 seconds to $11.00, cancelling
  and replacing at each step; a fill at any step opens the position.
  An unfilled cycle cancels and waits 5 minutes before the next cycle.
  Pairs over $11.00 are logged with their actual price and retried
  next poll.
- Max 5 contracts per trade (see CONTRACTS), one position at a time.
- Exit at a 10% gain on the debit basis. There is no end-of-day forced
  close: a position that never reaches 10% stays open and blocks new
  entries until it does.
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
   - `MAX_DEBIT` (default `11.00`): the debit ceiling per share and the
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
- Entry window 6:45 a.m.-12:30 p.m. America/Los_Angeles, weekdays only,
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
- Exit at a 10% gain on the debit basis. There is no end-of-day forced
  close: a position that never reaches 10% stays open and blocks new
  entries until it does.
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
   - `MAX_DEBIT` (default `0.80`): the debit ceiling per share and the
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
