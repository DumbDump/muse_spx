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
