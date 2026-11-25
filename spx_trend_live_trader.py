#!/usr/bin/env python3
"""SPX trend vertical-spread LIVE trader for Railway.

Implements one fixed rule set, nothing else. THIS TRADES REAL MONEY
on the live Tradier account. It is a copy of the paper trend worker
(spx_trend_paper_trader.py) with two changes: the host is the LIVE
Tradier API (api.tradier.com) instead of the sandbox, and entries stop
after MAX_ROUND_TRIPS_PER_DAY completed round-trips per day (default
2). Everything else — trend, spreads, exits, hard exit — is identical
to the paper worker.

- Tradier LIVE host only (api.tradier.com). The host is hardcoded and
  the program refuses to run against any other host, so it cannot
  touch the paper account.
- SPX 0DTE (same-day expiry) vertical credit spreads, direction from the
  trend: bullish -> bull put spread, bearish -> bear call spread.
- Trend is the 9/21 EMA crossover on BOTH the 15-minute and 5-minute
  SPX bars: an entry needs both timeframes to agree, seeded from the
  prior sessions' bars so it is valid from the open. (VWAP is NOT used:
  SPX is an index and Tradier's timesales returns volume 0 for it, so a
  volume-weighted average cannot be computed from this feed.)
- Spreads are $5 wide, short leg ~0.5% out of the money
  (snapped to the nearest listed $5 strike), long leg $5 further out.
  Each entry order is 1 contract (ENTRY_CONTRACTS, Nov 25, 2026:
  the worker places TWO 1-contract entry orders per trade — see
  below — so CONTRACTS is no longer the entry size).
- Leg-collision avoidance: before entering, the worker reads every
  open SPXW position in the live account. If either leg's strike is
  already held by another strategy (e.g. a live iron condor), the
  whole spread shifts one $5 step further out of the money (down for
  puts, up for calls) and is re-checked, up to 3 steps; if every
  nearby strike is held, the entry is skipped rather than doubling
  up on a leg.
- Entry window 7:00 a.m.-12:00 p.m. America/Los_Angeles (10:00 a.m.-
  3:00 p.m. ET), weekdays only. One position at a time. An entry is
  TWO 1-contract limit credit orders for the same legs, placed
  together (Nov 25, 2026): the primary at the spread's mid, floored
  at $1.00 (MIN_CREDIT), and a stretch order at a fixed $3.50 credit
  (STRETCH_CREDIT). The legs are chosen so the spread pays at least
  $1.00: when the ~0.5% out-of-the-money spread pays less, the
  strikes move toward the money until the credit reaches $1.00 (up
  to 4 steps, $20, never letting the short strike go in the money).
  If no nearby strikes reach $1.00, the limit is placed AT $1.00 and
  sits working until the spread's value rises to it. If the primary
  fills first, the stretch order STAYS working (Nov 25, 2026: it is
  NOT cancelled on the primary fill) and the single contract is
  managed normally (50% GTC). The stretch is cancelled only when
  the primary position closes — normally when its GTC take-profit
  fills — or at the 12:00 window end. If the stretch order
  also fills while the primary is still open — both contracts filled
  — the worker cancels any take-profit GTC and places ONE GTC close
  for both contracts together at the average entry credit
  (breakeven: no profit, no loss). If only the stretch fills, the
  primary stays working (it normally fills within a poll or two at
  ~$3.50 by price improvement); no GTC is placed until the primary
  resolves, and the poll fallback and hard exit still cover the
  position. Both orders stay working while the trend still matches,
  and are cancelled if the trend flips or the window ends. A trend
  flip while holding does NOT close and reverse: the open position
  is held until an exit fires.
- NO ADOPTION: the worker tracks and manages ONLY the trades it opened
  itself in the current process run (its in-memory entry). It never
  picks up, manages, places a GTC on, or counts a round-trip for any
  open spread it did not open — including a manual spread and its own
  spread from before a restart. After a restart it starts flat and
  manages nothing until it opens a new trade itself. A spread orphaned
  by a restart is left alone except for the GTC already working on it
  in Tradier, which the worker does not cancel.
- DAILY CAP: at most MAX_ROUND_TRIPS_PER_DAY completed round-trips
  per calendar day (default 2). A round-trip is one entry plus its
  close (profit target, hard exit, or GTC fill) of the
  worker's OWN spread only — other positions vanishing (an iron
  condor expiring, a manual spread closing) do not count, and a
  single empty positions read cannot count either. Once the cap
  is reached, no new entries are placed for the rest of the day; the
  worker keeps polling and managing any open position. The count
  resets at midnight PT. It is tracked in memory and reset on
  restart, so a redeploy mid-day can undercount by the round-trips
  completed before the restart — the cap is a guardrail, not a
  compliance control.
- Exits: a standing GTC take-profit is placed when a position opens
  (buy back at 50% of the credit received), and the poll-based
  take-profit remains as a fallback every 60 seconds: take profit when
  the buy-back cost falls to 50% of the credit received. The fallback
  re-verifies the target on a fresh read and never fires while the
  take-profit GTC is working (Nov 19, 2026: a one-poll phantom print
  tripped it and cancelled the good GTC). Exception: when BOTH entry
  orders filled, the take-profit is replaced by ONE GTC close for
  both contracts at the average entry credit (breakeven) — see the
  entry rule above. The breakeven GTC fills on a retrace to the
  average; the poll fallback and the 12:55 p.m. PT hard exit remain
  as backstops. There is no
  stop-loss: a position that moves against the worker is held until
  the 50% take-profit or the 12:55 p.m. PT hard exit. Any close
  (profit or hard exit) cancels the working GTC first, and a new GTC
  is placed when the next position opens.
- NICKEL RULE (Nov 25, 2026): SPX options trade in 5-cent
  increments, so every order price is rounded to the nearest $0.05
  (half a nickel rounds up) — entry limits, take-profit and breakeven
  GTCs, and mid closes — with a backstop in place_order so no path
  can place a non-nickel price.
- HARD EXIT: every position is closed at 12:55 p.m. PT (3:55 p.m. ET),
  5 minutes before the 4:00 p.m. ET market close, whatever its P&L.
  The check runs every poll, so a failed close is retried until it
  fills. No position is ever carried overnight.
- Every order carries a tag ("muse-spx-trend-live" by default) so the
  worker's own orders are identifiable.
- At most one pending order at a time, except the deliberate pair:
  the worker places TWO entry orders (primary + stretch) for the
  same legs. The stretch placement explicitly excludes the primary
  order id from the collision check, so the pair is never blocked
  by its own sibling; any other colliding order still blocks a
  placement. Before placing, the worker checks open orders and
  skips if a colliding order is already pending. A credit order
  never blocks a debit placement (a credit order is a take-profit
  or an entry for a position that may be gone — Tradier may refuse
  to cancel it, so a debit close must not wait on it); a debit
  order never blocks a credit placement (a debit order is the GTC
  take-profit or a close for a position that may be gone, so a new
  entry must not wait on it). Credit placements keep the old rule, so
  no duplicate entry is ever placed, and a duplicate GTC is never
  placed either — the worker tracks the GTC it placed by order id in
  memory and never picks up an order it did not place.

Safety:

- Live orders are placed ONLY when the environment variable
  ENABLE_TRADING is set to "1". Otherwise the program runs in dry-run
  mode: it evaluates the rules, logs exactly what it would place, and
  places nothing. Deploy with dry-run first and flip the flag only
  after the logs look right. THIS IS REAL MONEY — do not enable
  trading until the dry-run logs are verified.
- The Tradier API key comes from the TRADIER_API_KEY environment
  variable (set it in Railway, never in this file or in git). It must
  be a LIVE Tradier API key, not a sandbox key.

Railway setup:

- Push this file plus Procfile to a repo, create a Railway service
  from it, and set TRADIER_API_KEY to a Tradier LIVE key and
  CONTRACTS=1 (entries are 1 contract per order since Nov 25, 2026).
- Procfile runs: spx-trend-live-worker: python spx_trend_live_trader.py
- The worker loops forever, so Railway keeps it running. Logs are the
  P&L record: the program prints a heartbeat line every poll (so
  silence never means "is it running?"), a P&L line on the first poll
  of the 6:00 a.m. and 8:00 a.m. PT hours on weekdays, plus a line for
  every entry, exit, skip, and error.

Notes:

- Tradier live quotes are real-time (not delayed like the sandbox).
  Mids computed here are estimates, not live brokerage marks.
- Entry orders are day orders: an order left unfilled at the close
  expires with the session. Positions are closed on the 50% profit
  target or the 12:55 p.m. PT hard exit.

Usage:

  TRADIER_API_KEY=xxx ENABLE_TRADING=0 python spx_trend_live_trader.py
  TRADIER_API_KEY=xxx python spx_trend_live_trader.py --check   # one P&L snapshot, then exit
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")
HOST = "api.tradier.com"
BASE = "https://%s" % HOST

API_KEY = os.environ.get("TRADIER_API_KEY", "")
ENABLE_TRADING = os.environ.get("ENABLE_TRADING", "0") == "1"
CONTRACTS = max(1, min(5, int(os.environ.get("CONTRACTS", "1"))))
# Dual-order entry (Nov 25, 2026): each entry order is 1 contract,
# whatever CONTRACTS says — CONTRACTS is kept only for the startup
# log line, and Railway should carry CONTRACTS=1 to match.
ENTRY_CONTRACTS = 1
# Fixed credit for the second ("stretch") entry order: same legs as
# the primary, fills only if the spread's value rises to it.
STRETCH_CREDIT = float(os.environ.get("STRETCH_CREDIT", "3.50"))
SPREAD_WIDTH = int(os.environ.get("SPREAD_WIDTH", "5"))
OTM_PCT = float(os.environ.get("OTM_PCT", "0.005"))
ENTRY_START = dtime(7, 0)
ENTRY_END = dtime(12, 0)
HARD_EXIT = dtime(12, 55)
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
PROFIT_TARGET_FRACTION = float(os.environ.get("PROFIT_TARGET_FRACTION", "0.50"))
MIN_CREDIT = float(os.environ.get("MIN_CREDIT", "1.00"))
MAX_CREDIT = float(os.environ.get("MAX_CREDIT", "2.50"))
MIN_LEG_VOLUME = int(os.environ.get("MIN_LEG_VOLUME", "10"))
MAX_SHIFT_STEPS = int(os.environ.get("MAX_SHIFT_STEPS", "3"))
MAX_CREDIT_STEPS = int(os.environ.get("MAX_CREDIT_STEPS", "4"))
EMA_FAST = int(os.environ.get("EMA_FAST", "9"))
EMA_SLOW = int(os.environ.get("EMA_SLOW", "21"))
ORDER_TAG = os.environ.get("ORDER_TAG", "muse-spx-trend-live")
MAX_ROUND_TRIPS_PER_DAY = max(1, int(os.environ.get("MAX_ROUND_TRIPS_PER_DAY", "2")))


def log(msg):
    print("[%s] %s" % (datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z"), msg), flush=True)


def api_get(path):
    req = urllib.request.Request(BASE + path)
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", "Bearer %s" % API_KEY)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api_post_order(account, fields):
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/v1/accounts/%s/orders" % account, data=body, method="POST"
    )
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Authorization", "Bearer %s" % API_KEY)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_account():
    profile = api_get("/v1/user/profile")
    account = profile["profile"]["account"]
    if isinstance(account, list):
        account = account[0]
    return account["account_number"]


def get_balances(account):
    return api_get("/v1/accounts/%s/balances" % account)["balances"]


def get_positions(account):
    data = api_get("/v1/accounts/%s/positions" % account)
    positions = data.get("positions")
    # Tradier returns the string "null" (not an object) when the account
    # is flat; treat any non-dict value as "no positions" instead of
    # crashing on .get().
    if not isinstance(positions, dict):
        return []
    pos = positions.get("position")
    if pos is None:
        return []
    if isinstance(pos, dict):
        return [pos]
    return pos


def get_spx_price():
    data = api_get("/v1/markets/quotes?symbols=SPX&greeks=false")
    quotes_obj = data.get("quotes")
    if not isinstance(quotes_obj, dict):
        raise ValueError("SPX quote unavailable (null quotes)")
    quote = quotes_obj.get("quote")
    if isinstance(quote, list):
        quote = quote[0] if quote else None
    if not isinstance(quote, dict) or quote.get("last") is None:
        raise ValueError("SPX quote unavailable (null quote)")
    return float(quote["last"])


def round_strike(price):
    return int(round(price / 5.0) * 5)


def option_symbol(expiry, strike, kind):
    return "SPXW%s%s%08d" % (expiry.strftime("%y%m%d"), kind, strike * 1000)


def get_expirations():
    """Return sorted expiry dates. Falls back to weekday-derived dates.
    NOTE: Tradier's expirations endpoint takes the underlying SPX, not
    the weekly root SPXW — SPXW returns HTTP 200 with JSON null. And
    includeAllRoots=true is required, otherwise only monthly expiries
    come back."""
    try:
        data = api_get("/v1/markets/options/expirations?symbol=SPX&includeAllRoots=true")
        dates = data["expirations"]["date"]
        if isinstance(dates, str):
            dates = [dates]
        out = sorted(date.fromisoformat(d) for d in dates)
        if out:
            return out
    except Exception as exc:  # noqa: BLE001 - fallback is intentional
        log("expirations endpoint failed (%s), using weekday calendar" % exc)
    out = []
    day = date.today()
    while len(out) < 12:
        day += timedelta(days=1)
        if day.weekday() < 5:
            out.append(day)
    return out


def get_chain(expiry):
    """Return {(strike, kind): option dict} for one expiry, or {} when
    the endpoint returns no usable chain data. The dicts carry bid, ask
    and volume, which the spread selector needs. Chains take the
    underlying SPX; SPXW returns JSON null. The returned option symbols
    are still SPXW, which is what legs use."""
    data = api_get(
        "/v1/markets/options/chains?symbol=SPX&expiration=%s&greeks=false"
        % expiry.isoformat()
    )
    options_obj = data.get("options")
    if not isinstance(options_obj, dict):
        return {}
    options = options_obj.get("option")
    if options is None:
        return {}
    if isinstance(options, dict):
        options = [options]
    out = {}
    for opt in options:
        try:
            out[(float(opt["strike"]), opt["option_type"])] = opt
        except (KeyError, TypeError, ValueError):
            continue
    return out


def chain_mid(opt):
    return (float(opt["bid"]) + float(opt["ask"])) / 2.0


def get_bars(interval):
    """Return the closes of SPX bars at the given timesales interval
    ("15min" or "5min"), chronological, regular session only
    (09:30-16:00 ET). Fetches the last 4 calendar days so the EMA is
    seeded even on a Monday (prior Friday's bars). Timesales times are
    ET; volume is 0 for the index, so only closes are used. Returns []
    when the endpoint gives nothing usable. Tradier treats `end` as
    exclusive, so request one day past today to include today's bars."""
    today = datetime.now(TZ).date()
    start = today - timedelta(days=4)
    end = today + timedelta(days=1)
    try:
        data = api_get(
            "/v1/markets/timesales?symbol=SPX&interval=%s&start=%s&end=%s&session_filter=all"
            % (interval, start.isoformat(), end.isoformat())
        )
    except Exception as exc:  # noqa: BLE001 - no bars, no trend
        log("timesales failed (%s), no trend this poll" % exc)
        return []
    series = data.get("series", {})
    rows = series.get("data")
    if rows is None:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    closes = []
    for row in rows:
        try:
            t = row.get("time", "")
            # "2026-11-09T09:30:00" in ET; keep regular session only.
            hhmm = t[11:16]
            if not ("09:30" <= hhmm <= "16:00"):
                continue
            closes.append(float(row["close"]))
        except (KeyError, TypeError, ValueError):
            continue
    return closes


def get_bars_15m():
    """15-minute SPX closes; see get_bars()."""
    return get_bars("15min")


def get_bars_5m():
    """5-minute SPX closes; see get_bars()."""
    return get_bars("5min")


def ema(values, period):
    """EMA series, seeded with the SMA of the first `period` values.
    Returns [] when there are not enough values."""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _ema_direction(closes, label):
    """'bull'/'bear' from the 9/21 EMA crossover on the given closes,
    or (None, fast, slow) when there is no clear read (too few bars
    or the two EMAs are equal). Logs the insufficient-bars case for
    the named timeframe."""
    if len(closes) < EMA_SLOW:
        log("trend: only %d %s bars, need %d — no trend"
            % (len(closes), label, EMA_SLOW))
        return None, None, None
    fast = ema(closes, EMA_FAST)
    slow = ema(closes, EMA_SLOW)
    if not fast or not slow:
        return None, None, None
    f, s = fast[-1], slow[-1]
    if f > s:
        return "bull", f, s
    if f < s:
        return "bear", f, s
    return None, f, s


def trend_direction():
    """'bull' only when the 9/21 EMA crossover is bullish on BOTH the
    15-min and the 5-min SPX bars, 'bear' only when both are bearish.
    If the two timeframes disagree, or either has too few bars, there
    is no trend and no entry is placed that poll. The EMAs are seeded
    from prior sessions, so the read is valid from the open. Returns
    (direction, 15-min fast EMA, 15-min slow EMA)."""
    dir15, f15, s15 = _ema_direction(get_bars_15m(), "15-min")
    if dir15 is None:
        return None, f15, s15
    dir5, f5, s5 = _ema_direction(get_bars_5m(), "5-min")
    if dir5 is None:
        return None, f15, s15
    if dir15 != dir5:
        log("trend: 15m %s / 5m %s — no entry" % (dir15, dir5))
        return None, f15, s15
    return dir15, f15, s15


def held_spxw_strikes(account):
    """Strikes currently held in any open SPXW position in the live
    account, whatever lane opened them. Used to avoid opening a spread
    on a strike another strategy already holds."""
    strikes = set()
    for p in get_positions(account):
        sym = str(p.get("symbol", ""))
        if not sym.startswith("SPXW"):
            continue
        # Symbol: SPXW + YYMMDD + P/C + 8-digit strike (strike x 1000).
        m = re.search(r"[PC](\d{8})$", sym)
        if m:
            strikes.add(float(m.group(1)) / 1000.0)
    return strikes


def _credit_for(chain, short_strike, long_strike, kind):
    """Mid-based credit for a $5-wide spread, or (None, reason) when a
    leg is missing, has no two-sided quote, or is below the volume
    floor."""
    short_opt = chain.get((float(short_strike), kind))
    long_opt = chain.get((float(long_strike), kind))
    if short_opt is None or long_opt is None:
        return None, "missing %s leg at %d/%d" % (kind, short_strike, long_strike)
    for opt, label in ((short_opt, "short"), (long_opt, "long")):
        try:
            if float(opt["bid"]) <= 0 or float(opt["ask"]) <= 0:
                return None, "%s %s leg has no two-sided quote" % (label, kind)
            if int(opt.get("volume") or 0) < MIN_LEG_VOLUME:
                return None, "%s %s leg volume %s below %d" % (
                    label, kind, opt.get("volume"), MIN_LEG_VOLUME)
        except (KeyError, TypeError, ValueError):
            return None, "%s %s leg quote unreadable" % (label, kind)
    return round(chain_mid(short_opt) - chain_mid(long_opt), 2), None


def select_spread(spx, direction, chain, expiry, held_strikes):
    """Pick the 0DTE vertical for the trend direction. Returns
    (short_strike, long_strike, kind, limit, legs) or (None, reason).
    Bull: sell the put ~OTM_PCT below SPX, buy the put WIDTH lower.
    Bear: sell the call ~OTM_PCT above SPX, buy the call WIDTH higher.
    Strikes snap to the nearest listed $5 strike. If either leg's
    strike is already held in an open SPXW position (any lane), the
    whole spread shifts one $5 step further out of the money — down
    for puts, up for calls — and is re-checked, up to
    MAX_SHIFT_STEPS times. If every nearby strike is held, the entry
    is skipped. The legs are then chosen so the spread pays at least
    MIN_CREDIT: when the mid credit is below the $1.00 floor, the
    whole spread shifts one $5 step TOWARD the money — up for puts,
    down for calls — and is re-checked, up to MAX_CREDIT_STEPS
    times, skipping strikes that are held and never letting the short
    strike go in the money. If no nearby strikes reach the floor,
    the limit is placed AT the floor on the out-of-the-money strikes
    and sits working until the spread's value rises to it. A spread
    above MAX_CREDIT is still rejected."""
    if direction == "bull":
        kind = "put"
        target = spx * (1 - OTM_PCT)
        strikes = sorted(s for (s, k) in chain if k == "put")
        below = [s for s in strikes if s <= target]
        if not below:
            return None, "no put strike at or below %.2f" % target
        short_strike = int(max(below))
        long_strike = short_strike - SPREAD_WIDTH
        toward = SPREAD_WIDTH    # up, closer to SPX
        away = -SPREAD_WIDTH     # down, further out of the money
        def otm_ok(s):
            return s < spx
    else:
        kind = "call"
        target = spx * (1 + OTM_PCT)
        strikes = sorted(s for (s, k) in chain if k == "call")
        above = [s for s in strikes if s >= target]
        if not above:
            return None, "no call strike at or above %.2f" % target
        short_strike = int(min(above))
        long_strike = short_strike + SPREAD_WIDTH
        toward = -SPREAD_WIDTH   # down, closer to SPX
        away = SPREAD_WIDTH      # up, further out of the money
        def otm_ok(s):
            return s > spx
    # Leg-collision avoidance: if either leg's strike is already held
    # in an open SPXW position (any lane), shift the whole spread one
    # $5 step further out of the money and re-check, up to
    # MAX_SHIFT_STEPS times. If every nearby strike is held, skip the
    # entry instead of doubling up on a leg.
    shifts = 0
    while (float(short_strike) in held_strikes
           or float(long_strike) in held_strikes):
        if shifts >= MAX_SHIFT_STEPS:
            return None, "strikes near %d/%d held after %d shifts, skipping" % (
                short_strike, long_strike, shifts)
        short_strike += away
        long_strike += away
        shifts += 1
    if shifts:
        log("spread shifted %d step(s) to %d/%d to avoid held strikes"
            % (shifts, short_strike, long_strike))
    base_short, base_long = short_strike, long_strike
    # Credit search: choose legs that pay at least MIN_CREDIT. When
    # the mid credit is below the floor, shift the whole spread one
    # $5 step toward the money and re-check, up to MAX_CREDIT_STEPS
    # times, skipping strikes that are held and never letting the
    # short strike go in the money.
    credit = None
    credit_steps = 0
    while credit_steps <= MAX_CREDIT_STEPS:
        if (float(short_strike) in held_strikes
                or float(long_strike) in held_strikes):
            # These strikes are held: move one step toward the money
            # instead of using them.
            if credit_steps >= MAX_CREDIT_STEPS:
                break
            new_short = short_strike + toward
            if not otm_ok(new_short):
                break
            short_strike += toward
            long_strike += toward
            credit_steps += 1
            continue
        credit, reason = _credit_for(chain, short_strike, long_strike, kind)
        if credit is None:
            return None, reason
        if credit > MAX_CREDIT:
            return None, "credit %.2f above %.2f" % (credit, MAX_CREDIT)
        if credit >= MIN_CREDIT:
            break
        if credit_steps >= MAX_CREDIT_STEPS:
            break
        new_short = short_strike + toward
        if not otm_ok(new_short):
            break
        short_strike += toward
        long_strike += toward
        credit_steps += 1
    if credit is not None and credit >= MIN_CREDIT:
        if credit_steps:
            log("spread moved %d step(s) toward the money to %d/%d "
                "for $%.2f credit"
                % (credit_steps, short_strike, long_strike, credit))
        # Nickel rule (Nov 25, 2026): the mid credit can land between
        # 5-cent ticks (e.g. $1.73); the limit is the nearest nickel.
        limit = round_nickel(credit)
    else:
        # No nearby strikes reach the floor: fall back to the limit
        # floor on the out-of-the-money base strikes.
        short_strike, long_strike = base_short, base_long
        credit, reason = _credit_for(chain, short_strike, long_strike, kind)
        if credit is None:
            return None, reason
        limit = MIN_CREDIT
        log("mid credit %.2f below $%.2f floor after %d step(s) — "
            "limit placed at $%.2f"
            % (credit, MIN_CREDIT, credit_steps, limit))
    legs = [
        ("sell_to_open", option_symbol(expiry, short_strike, "P" if kind == "put" else "C"), ENTRY_CONTRACTS),
        ("buy_to_open", option_symbol(expiry, long_strike, "P" if kind == "put" else "C"), ENTRY_CONTRACTS),
    ]
    return (short_strike, long_strike, kind, limit, legs), None


def get_pending_orders(account):
    """Return working orders (status pending/open) with leg symbols,
    tag and type."""
    data = api_get("/v1/accounts/%s/orders" % account)
    orders_obj = data.get("orders")
    # Same empty-response quirk as positions: a non-dict value means no
    # orders, not a crash.
    if not isinstance(orders_obj, dict):
        return []
    orders = orders_obj.get("order")
    if orders is None:
        return []
    if isinstance(orders, dict):
        orders = [orders]
    pending = []
    for order in orders:
        if order.get("status") not in ("pending", "open"):
            continue
        legs = order.get("leg")
        if isinstance(legs, dict):
            legs = [legs]
        symbols = {leg.get("option_symbol") for leg in (legs or [])}
        symbols.discard(None)
        pending.append({
            "id": order.get("id"),
            "tag": order.get("tag"),
            "type": order.get("type"),
            "symbols": symbols,
        })
    return pending


def has_blocking_pending_order(account, legs, exclude_id=None, ignore_credit=False, ignore_debit=False):
    """True when the worker already has a pending order that would
    collide with the given legs: same tag, or the same leg symbols.
    Matching on leg symbols means the check works even for orders
    placed before tagging, and even if the provider does not echo the
    tag back. exclude_id skips one order the caller just cancelled, so
    a stale feed listing it does not block the replacement. ignore_credit
    is True only for debit placements: a credit order is a take-profit
    or an entry for a position that may be gone, and Tradier may refuse
    to cancel it, so a debit close must not wait on it. ignore_debit
    is True only for credit placements: a debit order is the GTC
    take-profit or a close for a position that may be gone, so a new
    entry must not wait on it. Credit placements keep the old rule, so
    no duplicate entry is ever placed."""
    new_symbols = {symbol for _, symbol, _ in legs}
    for order in get_pending_orders(account):
        if exclude_id is not None and str(order["id"]) == str(exclude_id):
            continue
        if ignore_credit and order.get("type") == "credit":
            continue
        if ignore_debit and order.get("type") == "debit":
            continue
        if order["tag"] == ORDER_TAG or order["symbols"] == new_symbols:
            return True, order["id"]
    return False, None


def place_order(account, order_type, price, legs, duration="day", exclude_order_id=None):
    """legs: list of (side, symbol, qty). Returns the API response dict.
    Never creates a second pending order: if a colliding order is already
    pending, the placement is skipped and logged. Nickel backstop
    (Nov 25, 2026): every price is rounded to the nearest $0.05 here,
    so no path can place a non-nickel SPX order even if a caller
    forgets to round."""
    _nickel = round_nickel(price)
    if abs(_nickel - price) > 0.001:
        log("price %.2f rounded to nickel %.2f" % (price, _nickel))
        price = _nickel
    blocked, blocking_id = has_blocking_pending_order(
        account, legs, exclude_id=exclude_order_id,
        ignore_credit=(order_type == "debit"),
        ignore_debit=(order_type == "credit"))
    if blocked:
        log("SKIPPED %s order at %.2f: pending order %s already exists"
            % (order_type, price, blocking_id))
        return {"blocked": True, "blocking_order_id": blocking_id}
    fields = {
        "class": "multileg",
        "symbol": "SPX",
        "type": order_type,
        "duration": duration,
        "price": "%.2f" % price,
        "tag": ORDER_TAG,
    }
    for i, (side, symbol, qty) in enumerate(legs):
        fields["option_symbol[%d]" % i] = symbol
        fields["side[%d]" % i] = side
        fields["quantity[%d]" % i] = str(qty)
    if not ENABLE_TRADING:
        log("DRY RUN: would place %s order at %.2f: %s"
            % (order_type, price, legs))
        return {"dry_run": True}
    resp = api_post_order(account, fields)
    order = resp.get("order", resp)
    log("PLACED %s order id=%s price=%.2f legs=%s"
        % (order_type, order.get("id"), price, legs))
    return resp


def cancel_order(account, order_id):
    """Cancel one working order. Tradier cancels via DELETE on the order."""
    req = urllib.request.Request(
        BASE + "/v1/accounts/%s/orders/%s" % (account, order_id), method="DELETE"
    )
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", "Bearer %s" % API_KEY)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cancel_confirmed(account, order_id):
    """Attempt to cancel order_id. Return True only when the order is
    confirmed gone: the DELETE succeeded, or the order is no longer
    listed among pending orders (it filled or was cancelled on its
    own). Return False when the cancel was refused AND the order is
    still pending — the caller must keep tracking it and must NOT
    place a replacement order, or the old order can fill alongside
    the new one and open a second position. That is the duplicate
    entry that left the 7325/7330 bear call unmanaged on Nov 17, 2026:
    the old cancel was swallowed as "ignoring" and the worker forgot
    the order while it was still working."""
    try:
        cancel_order(account, order_id)
        return True
    except Exception as exc:  # noqa: BLE001 - check the feed, do not assume
        log("cancel of order %s refused (%s), checking if still pending"
            % (order_id, exc))
    try:
        for order in get_pending_orders(account):
            if str(order.get("id")) == str(order_id):
                return False
    except Exception as exc:  # noqa: BLE001 - cannot confirm, stay safe
        log("cancel check for order %s failed (%s), assuming still pending"
            % (order_id, exc))
        return False
    return True


def has_pending_credit_entry(account):
    """(True, order_id) when any pending CREDIT order has 0DTE SPXW
    legs — an entry order, the worker's own or a manual one. The
    worker holds one position at a time, so no new entry is placed
    while a credit order is working. This covers a restart, where the
    in-memory working entry is lost but the pre-restart entry order is
    still working. GTC take-profits and closes are debit orders and
    never block here. A failed pending-orders read is treated as
    blocking: a new entry must not be placed on an unread feed."""
    today = datetime.now(TZ).date()
    try:
        pending = get_pending_orders(account)
    except Exception as exc:  # noqa: BLE001 - stay safe, skip the entry
        log("pending credit-entry check failed (%s), entry skipped this poll"
            % exc)
        return True, None
    for order in pending:
        if order.get("type") != "credit":
            continue
        for sym in order.get("symbols", set()):
            if (str(sym).startswith("SPXW")
                    and _expiry_from_symbol(str(sym)) == today):
                return True, order.get("id")
    return False, None


def close_legs_for(legs):
    """Closing legs for open legs: buy back shorts, sell longs."""
    out = []
    for leg in legs:
        side = "buy_to_close" if float(leg["quantity"]) < 0 else "sell_to_close"
        out.append((side, leg["symbol"], abs(int(leg["quantity"]))))
    return out


def _expiry_from_symbol(symbol):
    """Expiry date parsed from an option symbol (the first 6-digit
    YYMMDD run). Returns None when no expiry parses."""
    match = re.search(r"(\d{6})", symbol)
    if not match:
        return None
    code = match.group(1)
    return date(2000 + int(code[0:2]), int(code[2:4]), int(code[4:6]))


def _leg_kind_strike(symbol):
    """(kind, strike) from an SPXW symbol, or (None, None)."""
    m = re.search(r"([PC])(\d{8})$", str(symbol))
    if not m:
        return None, None
    return ("put" if m.group(1) == "P" else "call", float(m.group(2)) / 1000.0)


def group_0dte_spreads(legs):
    """Group 0DTE position legs into vertical spreads. Each spread is a
    list of position dicts: one short + one long, same kind, strikes
    SPREAD_WIDTH apart, same |quantity|. Returns a list of spreads;
    unpaired legs are dropped. This matters when the account holds
    more than one 0DTE spread (e.g. the worker's own plus a manual
    one): the worker must manage only its own spread, never a
    mixture of legs from two spreads."""
    shorts = []
    longs = []
    for leg in legs:
        kind, strike = _leg_kind_strike(leg.get("symbol", ""))
        if kind is None:
            continue
        qty = float(leg.get("quantity", 0))
        if qty < 0:
            shorts.append((kind, strike, leg))
        elif qty > 0:
            longs.append((kind, strike, leg))
    spreads = []
    used_longs = set()
    for kind, s_strike, s_leg in shorts:
        # Long leg is WIDTH further OTM: puts lower, calls higher.
        want = s_strike - SPREAD_WIDTH if kind == "put" else s_strike + SPREAD_WIDTH
        for i, (l_kind, l_strike, l_leg) in enumerate(longs):
            if i in used_longs:
                continue
            if (l_kind == kind and l_strike == want
                    and abs(float(l_leg.get("quantity", 0))) == abs(float(s_leg.get("quantity", 0)))):
                spreads.append([s_leg, l_leg])
                used_longs.add(i)
                break
    return spreads


def position_value_and_basis(account, entry=None):
    """Return (buyback, credit, legs) for the open SPX trend spread —
    the worker's OWN spread only, tracked in memory via `entry` (the
    entry dict for a spread the worker placed itself in this process
    run). Only legs expiring today. A multi-day double calendar (the
    SPX worker's position) has legs expiring after today and is
    ignored, so this worker never manages another lane's position.
    With no entry the worker is flat, period: it never infers a
    position from the account, so a manual spread or its own
    pre-restart spread is never picked up. When an entry is given,
    only the spread whose strikes match it is returned; anything else
    (including a manual spread alongside the worker's) is ignored,
    and no match means flat. This prevents a GTC or close from being
    built out of legs from two different spreads — the bug that
    produced a rejected order with mixed legs. The worker shorts the
    spread, so the position's market value is negative (what it would
    cost to buy back) and its cost basis is negative (the credit
    received). buyback = -value, credit = -basis, P&L = credit -
    buyback. Returns (0.0, 0.0, []) when flat."""
    if entry is None:
        # No in-memory entry: the worker is flat. It never reads the
        # account to infer a position, so nothing can be picked up.
        return 0.0, 0.0, []
    today = datetime.now(TZ).date()
    legs = [p for p in get_positions(account)
            if str(p.get("symbol", "")).startswith("SPXW")
            and _expiry_from_symbol(str(p.get("symbol", ""))) == today]
    if not legs:
        return 0.0, 0.0, []
    spreads = group_0dte_spreads(legs)
    chosen = None
    for sp in spreads:
        strikes = {_leg_kind_strike(l.get("symbol", ""))[1] for l in sp}
        if (float(entry["short_strike"]) in strikes
                and float(entry["long_strike"]) in strikes):
            chosen = sp
            break
    if chosen is None:
        # The tracked spread is gone (closed, or never opened): flat.
        # Any other spread open in the account is not the worker's.
        return 0.0, 0.0, []
    symbols = ",".join(p["symbol"] for p in chosen)
    data = api_get("/v1/markets/quotes?symbols=%s&greeks=false" % symbols)
    quotes = data["quotes"]["quote"]
    if isinstance(quotes, dict):
        quotes = [quotes]
    mids = {q["symbol"]: (float(q["bid"]) + float(q["ask"])) / 2.0 for q in quotes}
    value = 0.0
    basis = 0.0
    for leg in chosen:
        mid = mids.get(leg["symbol"], 0.0)
        value += float(leg["quantity"]) * mid * 100.0
        basis += float(leg["cost_basis"])
    return -value, -basis, chosen


def gtc_order_working(account, order_id):
    """True when the given order id is still a working debit order.
    Order-id lookup only: the worker verifies the GTC it placed
    itself in this run, and never picks up an order by its legs."""
    if order_id is None:
        return False
    for order in get_pending_orders(account):
        if (str(order.get("id")) == str(order_id)
                and order.get("type") == "debit"):
            return True
    return False


def order_still_pending(account, order_id):
    """True when the given order id is still a working order of any
    type. Used to tell which of the two entry orders filled: an
    order the worker did not cancel that is no longer pending has
    filled."""
    if order_id is None:
        return False
    for order in get_pending_orders(account):
        if str(order.get("id")) == str(order_id):
            return True
    return False


def round_nickel(price):
    """Round a per-share price to the nearest $0.05 (Nov 25, 2026):
    SPX options trade in 5-cent increments, so every order price the
    worker places must be a multiple of $0.05. Half a nickel rounds
    up (0.875 -> 0.90, 0.88 -> 0.90, 2.625 -> 2.65). Computed in
    integer cents to dodge float error, and floored at $0.05 — a
    $0.00 price would be rejected."""
    price = float(price)
    sign = -1.0 if price < 0 else 1.0
    cents = int(abs(price) * 100.0 + 0.5)
    nickels = (cents + 2) // 5
    return sign * max(0.05, nickels * 5 / 100.0)


def place_gtc_close(account, legs, price, label):
    """Place a standing GTC debit close for the worker's OWN open
    spread at an explicit per-share price. Returns the order id, or
    None when the placement is blocked. No check for a pre-existing
    GTC is done: the worker tracks its GTC by order id in memory
    and never picks up an order it did not place."""
    price = round_nickel(max(price, 0.01))
    close_legs = close_legs_for(legs)
    resp = place_order(account, "debit", price, close_legs, duration="gtc")
    if resp.get("blocked") or resp.get("dry_run"):
        return None
    order = resp.get("order", resp)
    log("GTC %s placed id=%s buyback %.2f" % (label, order.get("id"), price))
    return order.get("id")


def place_gtc_take_profit(account, legs, credit):
    """Place the standing GTC take-profit for the worker's OWN open
    spread: a debit order to buy it back at 50% of the credit
    received. Only ever called for a spread the worker opened itself
    in this run. Returns the order id, or None when the placement is
    blocked. No check for a pre-existing GTC is done: the worker
    tracks its GTC by order id in memory and never picks up an order
    it did not place."""
    contracts = max(abs(int(leg["quantity"])) for leg in legs)
    if credit <= 0 or contracts <= 0:
        return None
    price = round_nickel(PROFIT_TARGET_FRACTION * credit / (contracts * 100.0))
    return place_gtc_close(account, legs, price, "take-profit")


def place_breakeven_gtc(account, legs, credit):
    """Place the standing GTC breakeven close for a position opened
    by BOTH entry orders (Nov 25, 2026): one debit order for all
    contracts together at the average entry credit — buy back at
    exactly what was received, no profit, no loss. `credit` is the
    position's TOTAL credit (both contracts), so the per-share price
    is the average of the two fill credits. Returns the order id,
    or None when the placement is blocked."""
    contracts = max(abs(int(leg["quantity"])) for leg in legs)
    if credit <= 0 or contracts <= 0:
        return None
    price = round_nickel(credit / (contracts * 100.0))
    return place_gtc_close(account, legs, price, "breakeven")


def verified_basis(account, entry):
    """Two consecutive position reads that must agree (same legs and
    credit within a cent) before the caller trusts the basis. A fill
    landing between the reads — or a torn snapshot — makes them
    disagree, and the caller retries next poll. Used for the
    breakeven close, whose price comes straight from the basis: a
    stale credit would place the close at the wrong average (a
    breakeven computed from one contract's credit over two
    contracts' legs locks in a loss). Same re-verify pattern as
    the poll-fallback guard (Nov 19, 2026). Returns
    (buyback, credit, legs), or (None, None, None) when the reads
    disagree or the spread is gone."""
    b1, c1, l1 = position_value_and_basis(account, entry=entry)
    if not l1:
        return None, None, None
    b2, c2, l2 = position_value_and_basis(account, entry=entry)
    if not l2:
        return None, None, None

    def _sig(legs):
        return sorted((l.get("symbol"), float(l.get("quantity", 0))) for l in legs)

    if _sig(l1) != _sig(l2) or abs(c1 - c2) > 0.01:
        log("breakeven basis re-read disagreed (credit %.2f vs %.2f), retrying next poll"
            % (c1, c2))
        return None, None, None
    return b2, c2, l2


def close_position(account, legs, reason, entry=None):
    """Buy back the open spread at the mid. Cancels a blocking order
    first — the working GTC take-profit, or an unfilled entry for the
    same strikes — so the close is the only order working on these
    legs. The close is a DEBIT order for the buy-back cost — the worker
    is short the spread, so closing costs money; a negative or zero
    buy-back is floored at $0.01, since Tradier rejects a zero/negative
    price. Returns early when the position is already gone. The
    `entry` hint keeps the re-read scoped to the worker's own spread
    when the account holds more than one 0DTE spread."""
    buyback, credit, fresh_legs = position_value_and_basis(account, entry=entry)
    if not fresh_legs:
        log("closing (%s): position already closed, nothing to do" % reason)
        return {"already_closed": True}
    legs = fresh_legs
    contracts = max(abs(int(leg["quantity"])) for leg in legs)
    price = round_nickel(buyback / (contracts * 100.0))
    close_legs = close_legs_for(legs)
    blocked, blocking_id = has_blocking_pending_order(account, close_legs)
    if blocked:
        try:
            cancel_order(account, blocking_id)
            log("closing (%s): cancelled working order %s to place the close"
                % (reason, blocking_id))
        except Exception as exc:  # noqa: BLE001 - retry next poll
            log("closing (%s): cancel of order %s failed (%s), close skipped, will retry"
                % (reason, blocking_id, exc))
            return {"blocked": True, "blocking_order_id": blocking_id}
    log("closing (%s): buyback=%.2f credit=%.2f pnl=%.2f debit=%.2f"
        % (reason, buyback, credit, credit - buyback, max(price, 0.01)))
    # exclude_order_id: the cancel above just removed this order, but a
    # stale feed may still list it — do not let it block its own close.
    return place_order(account, "debit", max(price, 0.01), close_legs,
                       exclude_order_id=blocking_id if blocked else None)


def fallback_exit_allowed(account, entry, gtc_working, buyback, credit, fraction, below):
    """Whether the poll-based fallback may close the spread now.

    Guards against one-poll phantom prints. Nov 19, 2026: a $150
    buyback print tripped the 50% target on the 7335/7340, cancelling
    the working $0.80 GTC to place a $1.15 close that then blocked
    re-placing the GTC — the same pattern as the Nov 18 QQQ exit.
    Two guards, as in the QQQ workers:
    (1) profit target only: when the take-profit GTC is working, let
        it fill — the fallback fires only when no GTC is working. A
        stop skips this guard: the 50% GTC is far out of the money
        when the stop triggers, so it cannot protect the position.
    (2) re-verify the target on a fresh read; skip the close when the
        trigger print did not hold, or when the re-read legs are not
        the tracked spread's legs.
    `below` True: exit triggers when buyback <= fraction * credit
    (profit target). False: buyback >= fraction * credit (stop).
    Returns True only when the close may proceed.
    """
    if below and gtc_working:
        log("poll exit: take-profit GTC working, letting it fill")
        return False
    _b2, _c2, _l2 = position_value_and_basis(account, entry=entry)
    if not _l2 or _c2 <= 0:
        log("poll exit: position gone on re-read, skipping close")
        return False
    if entry is not None:
        _strikes = {_leg_kind_strike(l.get("symbol", ""))[1] for l in _l2}
        if (float(entry["short_strike"]) not in _strikes
                or float(entry["long_strike"]) not in _strikes):
            log("poll exit: re-read legs do not match the tracked spread, skipping close")
            return False
    if (_b2 <= fraction * _c2) if below else (_b2 >= fraction * _c2):
        return True
    log("poll exit: trigger print %.2f did not hold on re-read (buyback %.2f), skipping close"
        % (buyback, _b2))
    return False


def pnl_snapshot(account, entry=None):
    bal = get_balances(account)
    buyback, credit, legs = position_value_and_basis(account, entry=entry)
    log("P&L equity=%.2f realized_today=%.2f unrealized=%.2f open_legs=%d"
        % (float(bal["equity"]), float(bal["close_pl"]),
           (credit - buyback) if legs else 0.0, len(legs)))


def heartbeat_line(now, legs, buyback, credit, spx_txt, flat_state):
    """One short status line per poll: flat/open, SPX, and state."""
    if legs:
        return ("heartbeat: open %d legs buyback %.2f on credit %.2f pnl %.2f SPX %s"
                % (len(legs), buyback, credit, credit - buyback, spx_txt))
    return "heartbeat: flat SPX %s %s" % (spx_txt, flat_state)


def _legs_are_own_spread(legs, open_spread):
    """True when `legs` are exactly the worker's tracked spread: two
    legs, same kind, $5 apart, strikes matching open_spread. Anything
    else — unpaired condor legs (the fallback 'all legs' read),
    another spread, a single leg — is not the worker's spread."""
    if not legs or len(legs) != 2 or open_spread is None:
        return False
    try:
        short = float(open_spread["short_strike"])
        long = float(open_spread["long_strike"])
    except (KeyError, TypeError, ValueError):
        return False
    if abs(short - long) != SPREAD_WIDTH:
        return False
    kind = open_spread.get("kind")
    strikes = set()
    for leg in legs:
        leg_kind, strike = _leg_kind_strike(leg.get("symbol", ""))
        if strike is None or (kind is not None and leg_kind != kind):
            return False
        strikes.add(strike)
    return strikes == {short, long}


class RoundTripCounter:
    """Count completed round-trips for the daily cap — the worker's
    OWN $5-wide vertical spreads only.

    The old code counted ANY open -> flat transition of whatever
    position_value_and_basis() returned. That misfired twice on
    2026-11-16: the Nov 16 iron condors closing pre-market counted
    (their unpaired legs fall back to 'all legs', so they read as a
    position), and a transient empty positions read counted too —
    3/3 by 07:36 PT on 1 real round-trip, sidelining the worker
    until a manual restart. Two guards now apply: (1) a poll only
    counts as 'holding' when the legs are exactly the tracked
    spread — condor legs and other spreads never arm the counter.
    (2) a round-trip completes only after 2 consecutive polls with
    the own spread gone, so one empty read cannot count. Only the
    worker's own spread can arm the counter: a manual spread closing,
    an iron condor expiring, or a spread orphaned by a restart never
    counts. Undercounting stays possible (a spread that opens and
    closes within one poll, or a close while the worker is down) —
    the safe direction for a cap.
    """

    def __init__(self):
        self.holding_own = False
        self.flat_polls = 0

    def reset(self):
        self.holding_own = False
        self.flat_polls = 0

    def poll(self, legs, open_spread):
        """Feed one poll's legs + tracked spread. Returns True when a
        round-trip just completed."""
        if _legs_are_own_spread(legs, open_spread):
            self.holding_own = True
            self.flat_polls = 0
            return False
        if not self.holding_own:
            return False
        self.flat_polls += 1
        if self.flat_polls < 2:
            return False
        self.holding_own = False
        self.flat_polls = 0
        return True


def run_once():
    if not API_KEY:
        log("TRADIER_API_KEY is not set, exiting")
        return 1
    account = resolve_account()
    log("account %s connected (LIVE host %s, trading %s, contracts %d/order, stretch %.2f, "
        "spread $%d, short %.1f%% OTM, EMA %d/%d 15-min, hard exit %s PT, "
        "max %d round-trip(s)/day)"
        % (account, HOST, "ENABLED" if ENABLE_TRADING else "dry-run",
           ENTRY_CONTRACTS, STRETCH_CREDIT, SPREAD_WIDTH, OTM_PCT * 100, EMA_FAST, EMA_SLOW,
           HARD_EXIT.strftime("%H:%M"), MAX_ROUND_TRIPS_PER_DAY))
    last_pnl_day = {}
    # Working entry orders: TWO 1-contract limit credit orders for the
    # same legs (Nov 25, 2026). The primary at the mid, the stretch
    # at a fixed $3.50 credit. Both stay working while the trend
    # still matches; both are cancelled when the trend flips, the
    # strikes move, or the entry window ends.
    working_entry = None
    stretch_entry = None
    # Sticky fill flags: set the first poll an entry order is seen
    # gone from pending without the worker cancelling it. A filled
    # order's tracker is kept (not cleared) until the pair resolves,
    # so a cancel confirming an already-filled order gone cannot
    # erase the both-filled transition.
    primary_filled = False
    stretch_filled = False
    # Breakeven mode: both entry orders filled, so the combined
    # position closes at the average entry credit (one GTC for both
    # contracts together) instead of the 50% take-profit.
    breakeven_active = False
    # Whether the working GTC is a breakeven close (True) or the
    # 50% take-profit (False) — decides what a re-place places.
    gtc_is_breakeven = False
    # Logged-once flag for the stretch-only wait (primary still
    # working after the stretch filled).
    stretch_waiting_logged = False
    # Working GTC take-profit: one debit GTC at 50% of the credit, placed
    # when a position opens. Cancelled on any close; a new one is placed
    # when the next position opens.
    working_gtc = None
    # Every entry order this run placed, in order. Normally the tracked
    # spread is the only one open; if a duplicate entry ever fills, the
    # second spread is in this list too, so the backstop below can find
    # it and manage it instead of leaving it unmanaged with no GTC.
    own_entries = []
    # GTC take-profit ids for open own spreads other than the tracked
    # one, keyed by (short_strike, long_strike, kind).
    orphan_gtcs = {}
    # The spread the worker is currently managing (the entry dict for
    # the open position). Kept while a position is open so later polls
    # keep identifying THIS spread — not a manual spread that may also
    # be open in the same account. Cleared when flat.
    open_spread = None
    # Consecutive polls with no tracked spread. A position is only
    # treated as gone after 2 flat polls in a row, so one empty
    # positions read cannot orphan a live spread (or cancel its GTC)
    # — the same 2-poll guard the RoundTripCounter uses.
    flat_polls = 0
    # Daily round-trip cap: completed round-trips (one entry + its
    # close) per calendar day. Counted in memory by RoundTripCounter
    # from the worker's OWN spread only; reset at midnight PT and on
    # restart the count restarts at 0 (see docstring).
    round_trips_today = 0
    round_trips_date = None
    rt_counter = RoundTripCounter()
    while True:
        try:
            now = datetime.now(TZ)
            today = now.date()
            if now.weekday() >= 5:
                time.sleep(POLL_SECONDS)
                continue
            # Daily P&L lines at 6:00 and 8:00 a.m. PT.
            for hour in (6, 8):
                if now.hour == hour and last_pnl_day.get(hour) != today:
                    pnl_snapshot(account, open_spread)
                    last_pnl_day[hour] = today
            # A duplicate entry can leave a second own spread open that
            # the tracked spread never sees. If the tracked spread is
            # gone but an entry this run placed still has an open
            # position, manage that spread instead of going flat and
            # placing a new entry on top of it. Entries whose spreads
            # are gone are dropped, so this list stays tiny.
            if open_spread is None:
                _kept = []
                for _entry in list(own_entries):
                    if _entry is working_entry:
                        _kept.append(_entry)
                        continue
                    _b, _c, _lgs = position_value_and_basis(account, entry=_entry)
                    if _lgs:
                        _kept.append(_entry)
                        if open_spread is None:
                            open_spread = _entry
                            log("managing untracked own spread %s/%s (duplicate entry)"
                                % (_entry["short_strike"], _entry["long_strike"]))
                own_entries = _kept
            buyback, credit, legs = position_value_and_basis(
                account, entry=open_spread or working_entry)
            # Heartbeat every poll: one short line so the log always shows
            # the worker is alive and what it is doing.
            try:
                spx_txt = "%.2f" % get_spx_price()
            except Exception:
                spx_txt = "?"
            if legs:
                flat_state = ""
            elif ENTRY_START <= now.time() < ENTRY_END:
                flat_state = "in window, will evaluate"
            elif now.time() >= HARD_EXIT:
                flat_state = "past hard exit"
            else:
                flat_state = "waiting for window"
            log(heartbeat_line(now, legs, buyback, credit, spx_txt, flat_state))
            # Daily round-trip cap bookkeeping: reset at midnight PT.
            # A round-trip completes only when the worker's OWN spread
            # (tracked in open_spread) goes away for 2 polls running
            # — see RoundTripCounter. Other positions vanishing (an
            # iron condor expiring, a manual spread closing) and a
            # single empty positions read do not count.
            if round_trips_date != today:
                round_trips_today = 0
                round_trips_date = today
                rt_counter.reset()
                log("round-trip count reset for %s (cap %d/day)"
                    % (today.isoformat(), MAX_ROUND_TRIPS_PER_DAY))
            if rt_counter.poll(legs, open_spread):
                round_trips_today += 1
                log("round-trip %d/%d completed today"
                    % (round_trips_today, MAX_ROUND_TRIPS_PER_DAY))
            if legs:
                # The tracked spread is open: reset the flat counter.
                flat_polls = 0
                # Which entry orders filled? An order the worker did
                # not cancel that is no longer pending has filled.
                # Fill flags are sticky: a tracker is kept after its
                # order fills (not cleared) until the pair resolves,
                # so a cancel confirming an already-filled order gone
                # cannot erase the both-filled transition.
                if working_entry is not None and not order_still_pending(account, working_entry["id"]):
                    primary_filled = True
                if stretch_entry is not None and not order_still_pending(account, stretch_entry["id"]):
                    stretch_filled = True
                position_contracts = max(abs(int(leg["quantity"])) for leg in legs)
                # Remember which spread is ours (the entry that just
                # filled) so later polls keep managing THIS spread.
                if open_spread is None:
                    open_spread = working_entry or stretch_entry
                # Window end with a position open: a still-working
                # entry order is a pending new entry, not part of
                # the open position — cancel it (confirmed). The
                # open position itself keeps being managed (GTC /
                # fallback / hard exit). Nov 25, 2026: the stretch
                # stays working after the primary fills, so it can
                # still be working at 12:00 — the flat branch's
                # window-end cancel never runs while a position is
                # open, so this covers it.
                if now.time() >= ENTRY_END:
                    if working_entry is not None and not primary_filled:
                        if cancel_confirmed(account, working_entry["id"]):
                            log("entry order %s cancelled, window ended (position open)"
                                % working_entry["id"])
                            working_entry = None
                        else:
                            log("entry order %s still working at window end (position open), keeping it tracked"
                                % working_entry["id"])
                    if stretch_entry is not None and not stretch_filled:
                        if cancel_confirmed(account, stretch_entry["id"]):
                            log("stretch entry order %s cancelled, window ended (position open)"
                                % stretch_entry["id"])
                            stretch_entry = None
                        else:
                            log("stretch entry order %s still working at window end (position open), keeping it tracked"
                                % stretch_entry["id"])
                # BOTH entry orders filled: the combined position
                # closes at breakeven, both contracts together.
                # Cancel any 50% take-profit GTC and place ONE
                # breakeven GTC at the average entry credit.
                if primary_filled and stretch_filled and position_contracts >= 2 and not breakeven_active:
                    _gtc_clear = True
                    if working_gtc is not None and not gtc_is_breakeven:
                        if cancel_confirmed(account, working_gtc):
                            log("take-profit GTC %s cancelled for breakeven close"
                                % working_gtc)
                            working_gtc = None
                        else:
                            log("take-profit GTC %s still working, breakeven close will retry next poll"
                                % working_gtc)
                            _gtc_clear = False
                    if _gtc_clear:
                        # Verify the basis on a fresh read first: the
                        # breakeven price comes straight from it, so
                        # a torn snapshot would lock in a loss.
                        _b, _c, _legs = verified_basis(account, open_spread or working_entry)
                        if _legs is None:
                            log("breakeven close skipped this poll (basis not verified)")
                            breakeven_gtc = None
                        else:
                            breakeven_gtc = place_breakeven_gtc(account, _legs, _c)
                        if breakeven_gtc is not None:
                            working_gtc = breakeven_gtc
                            gtc_is_breakeven = True
                            breakeven_active = True
                            log("breakeven mode: both entries filled, closing %d contracts at average credit %.2f"
                                % (position_contracts, _c / (position_contracts * 100.0)))
                        else:
                            log("breakeven GTC blocked, will retry next poll")
                # Only the stretch filled, primary still working:
                # leave the primary working (it normally fills within
                # a poll or two by price improvement); place no GTC
                # until it resolves — the poll fallback and hard exit
                # still cover the position.
                elif stretch_filled and not primary_filled:
                    if not stretch_waiting_logged:
                        log("only stretch entry filled, primary %s still working, waiting for it to resolve"
                            % (working_entry["id"] if working_entry is not None else "?"))
                        stretch_waiting_logged = True
                # Standing GTC take-profit: place one (buy back at 50%
                # of the credit) when a position opens. The tracked
                # GTC is verified by order id each poll: a GTC
                # canceled or rejected out from under the worker must
                # be re-placed, not trusted from memory. If the
                # tracked id is no longer a working order, clear it
                # so a fresh one is placed. In breakeven mode the
                # re-place is a breakeven close, not a take-profit.
                # While waiting on the primary after a stretch-only
                # fill, no GTC is placed at all.
                if working_gtc is not None and not gtc_order_working(account, working_gtc):
                    log("tracked GTC %s no longer working, will re-place"
                        % working_gtc)
                    working_gtc = None
                    gtc_is_breakeven = False
                if working_gtc is None:
                    if breakeven_active:
                        # Re-place the breakeven GTC from a verified
                        # basis (same torn-snapshot guard as the
                        # first placement).
                        _b, _c, _legs = verified_basis(account, open_spread or working_entry)
                        if _legs is not None:
                            working_gtc = place_breakeven_gtc(account, _legs, _c)
                        gtc_is_breakeven = working_gtc is not None
                    elif stretch_filled and not primary_filled:
                        pass
                    else:
                        working_gtc = place_gtc_take_profit(account, legs, credit)
                        gtc_is_breakeven = False
                # Backstop: any OTHER open own spread (a duplicate entry
                # filled alongside the tracked one) gets the same
                # treatment — a standing GTC, the 50% profit target,
                # and the hard exit — so no own spread is ever
                # unmanaged with no GTC. Only spreads matching an
                # entry this run placed are touched; a manual spread
                # is never adopted.
                for _entry in list(own_entries):
                    if (open_spread is not None
                            and _entry["short_strike"] == open_spread["short_strike"]
                            and _entry["long_strike"] == open_spread["long_strike"]
                            and _entry.get("kind") == open_spread.get("kind")):
                        continue
                    _b, _c, _legs = position_value_and_basis(account, entry=_entry)
                    if not _legs:
                        continue
                    _key = (_entry["short_strike"], _entry["long_strike"], _entry["kind"])
                    _gtc = orphan_gtcs.get(_key)
                    if _gtc is not None and not gtc_order_working(account, _gtc):
                        log("orphan GTC %s no longer working, will re-place" % _gtc)
                        _gtc = None
                    if _gtc is None:
                        _gtc = place_gtc_take_profit(account, _legs, _c)
                        orphan_gtcs[_key] = _gtc
                    # Nov 19, 2026: the poll fallback is guarded —
                    # it never fires while the orphan GTC is working,
                    # and it re-verifies the 50% target on a fresh
                    # read (see fallback_exit_allowed).
                    if _c > 0 and _b <= PROFIT_TARGET_FRACTION * _c:
                        if fallback_exit_allowed(
                                account, _entry,
                                _gtc is not None and gtc_order_working(account, _gtc),
                                _b, _c, PROFIT_TARGET_FRACTION, True):
                            log("profit target hit on duplicate spread %s/%s: buyback %.2f on credit %.2f"
                                % (_entry["short_strike"], _entry["long_strike"], _b, _c))
                            close_position(account, _legs, "50% profit target (duplicate spread)",
                                           entry=_entry)
                            orphan_gtcs.pop(_key, None)
                            continue
                    if now.time() >= HARD_EXIT:
                        log("hard exit 12:55 PT: closing duplicate spread %s/%s"
                            % (_entry["short_strike"], _entry["long_strike"]))
                        close_position(account, _legs, "hard exit 12:55 PT (duplicate spread)",
                                       entry=_entry)
                        orphan_gtcs.pop(_key, None)
                # HARD EXIT: 12:55 p.m. PT, 5 minutes before the close.
                # Closes whatever is open, at the mid, whatever its P&L.
                # The close cancels the working GTC first.
                if now.time() >= HARD_EXIT:
                    log("hard exit 12:55 PT: closing open spread")
                    close_position(account, legs, "hard exit 12:55 PT",
                                   entry=open_spread or working_entry)
                    working_gtc = None
                    time.sleep(POLL_SECONDS)
                    continue
                # Profit target: buy back at 50% of the credit
                # received. The GTC normally closes this first; the
                # poll is the fallback if the GTC has not filled.
                # Nov 19, 2026: the fallback is guarded — it never
                # fires while the take-profit GTC is working, and it
                # re-verifies the 50% target on a fresh read
                # (see fallback_exit_allowed).
                if credit > 0 and buyback <= PROFIT_TARGET_FRACTION * credit:
                    if fallback_exit_allowed(
                            account, open_spread or working_entry,
                            working_gtc is not None and gtc_order_working(account, working_gtc),
                            buyback, credit, PROFIT_TARGET_FRACTION, True):
                        log("profit target hit: buyback %.2f on credit %.2f"
                            % (buyback, credit))
                        close_position(account, legs, "50% profit target",
                                       entry=open_spread or working_entry)
                        working_gtc = None
                    time.sleep(POLL_SECONDS)
                    continue
            else:
                # Flat (no tracked spread): confirm for 2 consecutive
                # polls before treating the position as gone — a single
                # empty positions read must not orphan a live spread
                # or cancel its GTC. On the first flat poll, wait.
                flat_polls += 1
                if flat_polls < 2:
                    time.sleep(POLL_SECONDS)
                    continue
                # Confirmed flat: the tracked spread is gone. Clear
                # the identity so the next position is identified
                # fresh, and cancel the GTC this run placed for the
                # gone spread so it cannot close a future position. A
                # refused cancel (it already filled) is logged once
                # and ignored. Nothing else is touched: a GTC placed
                # before a restart belongs to a spread this process
                # never tracked, and is left working.
                # Whether a position just closed (a spread was
                # tracked, or an entry filled): captured BEFORE the
                # state below is cleared, because the entry cleanup
                # must run only in the just-closed case.
                _just_closed = open_spread is not None or primary_filled or stretch_filled
                open_spread = None
                if working_gtc is not None:
                    try:
                        cancel_order(account, working_gtc)
                        log("tracked GTC %s cancelled (spread gone)"
                            % working_gtc)
                    except Exception as exc:  # noqa: BLE001 - best effort
                        log("tracked GTC %s cancel failed (%s), ignoring"
                            % (working_gtc, exc))
                    working_gtc = None
                breakeven_active = False
                gtc_is_breakeven = False
                primary_filled = False
                stretch_filled = False
                stretch_waiting_logged = False
                # An entry order left working after its position
                # closed must not fill a new position later: cancel
                # it now (confirmed). This is what cancels the
                # stretch when the primary position closes (Nov 25,
                # 2026: the stretch is NOT cancelled on the primary
                # fill — it is cancelled here, when the position is
                # confirmed gone). Guarded to the just-closed case:
                # this block also runs on every flat poll, and a
                # worker flat with working entries must NOT have
                # them cancelled out from under it.
                if _just_closed:
                    if working_entry is not None:
                        if cancel_confirmed(account, working_entry["id"]):
                            log("entry order %s cancelled (spread gone)"
                                % working_entry["id"])
                            working_entry = None
                        else:
                            log("entry order %s still working (spread gone), keeping it tracked"
                                % working_entry["id"])
                    if stretch_entry is not None:
                        if cancel_confirmed(account, stretch_entry["id"]):
                            log("stretch entry order %s cancelled (spread gone)"
                                % stretch_entry["id"])
                            stretch_entry = None
                        else:
                            log("stretch entry order %s still working (spread gone), keeping it tracked"
                                % stretch_entry["id"])
                in_window = ENTRY_START <= now.time() < ENTRY_END
                # Cancel working entries when the window ends: no entries
                # after 12:00 p.m. PT, so a stale order must not fill late.
                if working_entry is not None and not in_window:
                    if cancel_confirmed(account, working_entry["id"]):
                        log("entry order %s cancelled, window ended"
                            % working_entry["id"])
                        working_entry = None
                    else:
                        # Still working: keep tracking it and retry the
                        # cancel next poll; it is a day order and dies
                        # at the close, but it must not be forgotten
                        # while it can still fill.
                        log("entry order %s still working at window end, keeping it tracked"
                            % working_entry["id"])
                if stretch_entry is not None and not in_window:
                    if cancel_confirmed(account, stretch_entry["id"]):
                        log("stretch entry order %s cancelled, window ended"
                            % stretch_entry["id"])
                        stretch_entry = None
                    else:
                        log("stretch entry order %s still working at window end, keeping it tracked"
                            % stretch_entry["id"])
                if not in_window:
                    time.sleep(POLL_SECONDS)
                    continue
                # Daily cap: no new entries once the day's round-trip
                # allowance is used up. The worker keeps polling and
                # managing any open position; it simply will not open
                # another spread today.
                if round_trips_today >= MAX_ROUND_TRIPS_PER_DAY:
                    log("daily cap reached (%d/%d round-trips), no new entries today"
                        % (round_trips_today, MAX_ROUND_TRIPS_PER_DAY))
                    time.sleep(POLL_SECONDS)
                    continue
                # Trend + spread selection for the entry.
                direction, ema_fast, ema_slow = trend_direction()
                if direction is None:
                    log("no trend (EMA %d %s / EMA %d %s), skipping"
                        % (EMA_FAST,
                           ("%.2f" % ema_fast) if ema_fast is not None else "?",
                           EMA_SLOW,
                           ("%.2f" % ema_slow) if ema_slow is not None else "?"))
                    time.sleep(POLL_SECONDS)
                    continue
                if today not in get_expirations():
                    log("no 0DTE expiry today, skipping")
                    time.sleep(POLL_SECONDS)
                    continue
                try:
                    spx = get_spx_price()
                except Exception as exc:  # noqa: BLE001 - retry next poll
                    log("SPX price unavailable (%s), skipping" % exc)
                    time.sleep(POLL_SECONDS)
                    continue
                chain = get_chain(today)
                if not chain:
                    log("no 0DTE chain data, skipping")
                    time.sleep(POLL_SECONDS)
                    continue
                held = held_spxw_strikes(account)
                selected, reason = select_spread(spx, direction, chain, today, held)
                if selected is None:
                    log("no %s spread: %s (SPX %.2f)" % (direction, reason, spx))
                    time.sleep(POLL_SECONDS)
                    continue
                short_strike, long_strike, kind, spread_credit, spread_legs = selected
                # Working entries are kept only while they still match the
                # trend direction and the strikes. Otherwise cancel both
                # and re-place at the new strikes.
                _ref_entry = working_entry or stretch_entry
                if _ref_entry is not None:
                    if (_ref_entry["direction"] != direction
                            or _ref_entry["short_strike"] != short_strike
                            or _ref_entry["long_strike"] != long_strike):
                        _cancel_ok = True
                        if working_entry is not None:
                            if cancel_confirmed(account, working_entry["id"]):
                                log("entry order %s cancelled, trend/strikes moved"
                                    % working_entry["id"])
                                working_entry = None
                            else:
                                # Still working: do NOT place a
                                # replacement on top of it. Keep it
                                # tracked and retry the cancel next
                                # poll.
                                log("entry order %s still working, replacement skipped this poll"
                                    % working_entry["id"])
                                _cancel_ok = False
                        if stretch_entry is not None:
                            if cancel_confirmed(account, stretch_entry["id"]):
                                log("stretch entry order %s cancelled, trend/strikes moved"
                                    % stretch_entry["id"])
                                stretch_entry = None
                            else:
                                log("stretch entry order %s still working, replacement skipped this poll"
                                    % stretch_entry["id"])
                                _cancel_ok = False
                        if not _cancel_ok:
                            time.sleep(POLL_SECONDS)
                            continue
                        primary_filled = False
                        stretch_filled = False
                    else:
                        # Same trend, same strikes: leave both working.
                        time.sleep(POLL_SECONDS)
                        continue
                # No new entry while any 0DTE credit order is working
                # (a pre-restart entry the in-memory state lost, or a
                # manual order): placing on top of it opens a second
                # position. Skip this poll and retry.
                _blocked_credit, _blocking_id = has_pending_credit_entry(account)
                if _blocked_credit:
                    log("no entry: pending credit order %s still working, skipping this poll"
                        % _blocking_id)
                    time.sleep(POLL_SECONDS)
                    continue
                log("entry: SPX %.2f %s EMA %d %.2f / EMA %d %.2f, %s %d/%d %s "
                    "primary limit %.2f x %d + stretch limit %.2f x %d"
                    % (spx, direction, EMA_FAST, ema_fast, EMA_SLOW, ema_slow,
                       kind, short_strike, long_strike,
                       "bull put" if kind == "put" else "bear call",
                       spread_credit, ENTRY_CONTRACTS, STRETCH_CREDIT, ENTRY_CONTRACTS))
                resp = place_order(account, "credit", spread_credit, spread_legs)
                if resp.get("blocked"):
                    time.sleep(POLL_SECONDS)
                    continue
                if resp.get("dry_run"):
                    time.sleep(POLL_SECONDS)
                    continue
                order = resp.get("order", resp)
                working_entry = {
                    "id": order.get("id"),
                    "direction": direction,
                    "short_strike": short_strike,
                    "long_strike": long_strike,
                    "kind": kind,
                }
                # Remember every entry this run placed, so the backstop
                # can find a duplicate spread if one ever fills.
                own_entries.append(working_entry)
                # Second order: same legs, 1 contract, fixed $3.50
                # credit. Exclude the primary order id from the
                # collision check — the pair is deliberate, not a
                # duplicate.
                resp2 = place_order(account, "credit", STRETCH_CREDIT, spread_legs,
                                    exclude_order_id=working_entry["id"])
                if resp2.get("blocked"):
                    log("stretch entry blocked, continuing with primary only")
                    stretch_entry = None
                elif resp2.get("dry_run"):
                    stretch_entry = None
                else:
                    order2 = resp2.get("order", resp2)
                    stretch_entry = {
                        "id": order2.get("id"),
                        "direction": direction,
                        "short_strike": short_strike,
                        "long_strike": long_strike,
                        "kind": kind,
                    }
                    own_entries.append(stretch_entry)
                    log("stretch entry order %s placed at %.2f"
                        % (stretch_entry["id"], STRETCH_CREDIT))
        except Exception as exc:  # noqa: BLE001 - keep the worker alive
            log("error: %s" % exc)
        time.sleep(POLL_SECONDS)
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        if not API_KEY:
            log("TRADIER_API_KEY is not set, exiting")
            return 1
        account = resolve_account()
        pnl_snapshot(account)
        return 0
    return run_once()


if __name__ == "__main__":
    sys.exit(main())
