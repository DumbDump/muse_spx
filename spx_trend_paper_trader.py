#!/usr/bin/env python3
"""SPX trend vertical-spread paper trader for Railway.

Implements one fixed rule set, nothing else:

- Tradier PAPER host only (sandbox.tradier.com). The host is hardcoded and
  the program refuses to run against any other host, so it cannot touch a
  live account.
- SPX 0DTE (same-day expiry) vertical credit spreads, direction from the
  trend: bullish -> bull put spread, bearish -> bear call spread.
- Trend is the 9/21 EMA crossover on 15-minute SPX bars, seeded from the
  prior sessions' bars so it is valid from the open. (VWAP is NOT used:
  SPX is an index and Tradier's timesales returns volume 0 for it, so a
  volume-weighted average cannot be computed from this feed.)
- Spreads are $5 wide, short leg ~0.5% out of the money
  (snapped to the nearest listed $5 strike), long leg $5 further out.
  Contracts per trade come from CONTRACTS (default 1).
- Leg-collision avoidance: before entering, the worker reads every
  open SPXW position in the paper account. If either leg's strike is
  already held by another strategy, the whole spread shifts one $5
  step further out of the money (down for puts, up for calls) and is
  re-checked, up to 3 steps; if every nearby strike is held, the
  entry is skipped rather than doubling up on a leg.
- Entry window 7:00 a.m.-12:00 p.m. America/Los_Angeles (10:00 a.m.-
  3:00 p.m. ET), weekdays only. One position at a time. An entry is one
  limit credit order at the spread's mid, floored at $1.00 (MIN_CREDIT).
  The legs are chosen so the spread pays at least $1.00: when the
  ~0.5% out-of-the-money spread pays less, the strikes move toward
  the money until the credit reaches $1.00 (up to 8 steps, $40,
  never letting the short strike go in the money). If no nearby
  strikes reach $1.00, the limit is placed AT $1.00 and sits working
  until the spread's value rises to it. It stays working while the
  trend still matches, and is cancelled if the trend flips or the
  window ends. A trend flip while holding does NOT close and reverse:
  the open position is held until an exit fires.
- Exits (poll-based, every 60 seconds): take profit when the buy-back
  cost falls to 50% of the credit received; stop when the buy-back cost
  reaches 2x the credit received. No standing GTC take-profit.
- HARD EXIT: every position is closed at 12:55 p.m. PT (3:55 p.m. ET),
  5 minutes before the 4:00 p.m. ET market close, whatever its P&L.
  The check runs every poll, so a failed close is retried until it
  fills. No position is ever carried overnight.
- Every order carries a tag ("muse-spx-trend" by default) so the
  worker's own orders are identifiable.
- At most one pending order at a time: before placing, the worker checks
  open orders and skips if a colliding order is already pending. A
  credit order never blocks a debit placement (a credit order is a
  take-profit or an entry for a position that may be gone — Tradier may
  refuse to cancel it, so a debit close must not wait on it); credit
  placements keep the old rule, so no duplicate entry is ever placed.

Safety:

- Paper orders are placed ONLY when the environment variable
  ENABLE_TRADING is set to "1". Otherwise the program runs in dry-run
  mode: it evaluates the rules, logs exactly what it would place, and
  places nothing. Deploy with dry-run first and flip the flag only
  after the logs look right.
- The Tradier API key comes from the TRADIER_API_KEY environment
  variable (set it in Railway, never in this file or in git).

Railway setup:

- Push this file plus Procfile to a repo, create a Railway project
  from it, and set TRADIER_API_KEY to a Tradier SANDBOX (paper) key.
- Procfile runs: spx-trend-worker: python spx_trend_paper_trader.py
- The worker loops forever, so Railway keeps it running. Logs are the
  P&L record: the program prints a heartbeat line every poll (so
  silence never means "is it running?"), a P&L line on the first poll
  of the 6:00 a.m. and 8:00 a.m. PT hours on weekdays, plus a line for
  every entry, exit, skip, and error.

Notes:

- Tradier sandbox quotes are delayed about 15 minutes. Mids computed
  here are estimates, not live brokerage marks.
- Entry orders are day orders: an order left unfilled at the close
  expires with the session. Positions are closed on the 50% profit
  target, the 2x-credit stop, or the 12:55 p.m. PT hard exit.

Usage:

  TRADIER_API_KEY=xxx ENABLE_TRADING=0 python spx_trend_paper_trader.py
  TRADIER_API_KEY=xxx python spx_trend_paper_trader.py --check   # one P&L snapshot, then exit
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
HOST = "sandbox.tradier.com"
BASE = "https://%s" % HOST

API_KEY = os.environ.get("TRADIER_API_KEY", "")
ENABLE_TRADING = os.environ.get("ENABLE_TRADING", "0") == "1"
CONTRACTS = max(1, min(5, int(os.environ.get("CONTRACTS", "1"))))
SPREAD_WIDTH = int(os.environ.get("SPREAD_WIDTH", "5"))
OTM_PCT = float(os.environ.get("OTM_PCT", "0.005"))
ENTRY_START = dtime(7, 0)
ENTRY_END = dtime(12, 0)
HARD_EXIT = dtime(12, 55)
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
PROFIT_TARGET_FRACTION = float(os.environ.get("PROFIT_TARGET_FRACTION", "0.50"))
STOP_MULTIPLE = float(os.environ.get("STOP_MULTIPLE", "2.0"))
MIN_CREDIT = float(os.environ.get("MIN_CREDIT", "1.00"))
MAX_CREDIT = float(os.environ.get("MAX_CREDIT", "2.50"))
MIN_LEG_VOLUME = int(os.environ.get("MIN_LEG_VOLUME", "10"))
MAX_SHIFT_STEPS = int(os.environ.get("MAX_SHIFT_STEPS", "3"))
MAX_CREDIT_STEPS = int(os.environ.get("MAX_CREDIT_STEPS", "8"))
EMA_FAST = int(os.environ.get("EMA_FAST", "9"))
EMA_SLOW = int(os.environ.get("EMA_SLOW", "21"))
ORDER_TAG = os.environ.get("ORDER_TAG", "muse-spx-trend")


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


def get_bars_15m():
    """Return the closes of 15-minute SPX bars, chronological, regular
    session only (09:30-16:00 ET). Fetches the last 4 calendar days so
    the EMA is seeded even on a Monday (prior Friday's bars). Timesales
    times are ET; volume is 0 for the index, so only closes are used.
    Returns [] when the endpoint gives nothing usable."""
    today = datetime.now(TZ).date()
    start = today - timedelta(days=4)
    try:
        data = api_get(
            "/v1/markets/timesales?symbol=SPX&interval=15min&start=%s&end=%s&session_filter=all"
            % (start.isoformat(), today.isoformat())
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


def trend_direction():
    """'bull' when the fast EMA is above the slow EMA on 15-min bars,
    'bear' when below, None when there is no clear read (too few bars
    or the two are equal). The EMAs are seeded from prior sessions, so
    the read is valid from the open."""
    closes = get_bars_15m()
    if len(closes) < EMA_SLOW:
        log("trend: only %d 15-min bars, need %d — no trend"
            % (len(closes), EMA_SLOW))
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


def held_spxw_strikes(account):
    """Strikes currently held in any open SPXW position in the paper
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
        limit = credit
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
        ("sell_to_open", option_symbol(expiry, short_strike, "P" if kind == "put" else "C"), CONTRACTS),
        ("buy_to_open", option_symbol(expiry, long_strike, "P" if kind == "put" else "C"), CONTRACTS),
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


def has_blocking_pending_order(account, legs, exclude_id=None, ignore_credit=False):
    """True when the worker already has a pending order that would
    collide with the given legs: same tag, or the same leg symbols.
    Matching on leg symbols means the check works even for orders
    placed before tagging, and even if the provider does not echo the
    tag back. exclude_id skips one order the caller just cancelled, so
    a stale feed listing it does not block the replacement. ignore_credit
    is True only for debit placements: a credit order is a take-profit
    or an entry for a position that may be gone, and Tradier may refuse
    to cancel it, so a debit close must not wait on it. Credit
    placements keep the old rule, so no duplicate entry is ever placed."""
    new_symbols = {symbol for _, symbol, _ in legs}
    for order in get_pending_orders(account):
        if exclude_id is not None and str(order["id"]) == str(exclude_id):
            continue
        if ignore_credit and order.get("type") == "credit":
            continue
        if order["tag"] == ORDER_TAG or order["symbols"] == new_symbols:
            return True, order["id"]
    return False, None


def place_order(account, order_type, price, legs, duration="day", exclude_order_id=None):
    """legs: list of (side, symbol, qty). Returns the API response dict.
    Never creates a second pending order: if a colliding order is already
    pending, the placement is skipped and logged."""
    blocked, blocking_id = has_blocking_pending_order(
        account, legs, exclude_id=exclude_order_id,
        ignore_credit=(order_type == "debit"))
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


def position_value_and_basis(account):
    """Return (buyback, credit, legs) for the open SPX trend spread.
    Only this worker's own 0DTE vertical is returned: legs expiring
    today. A multi-day double calendar (the SPX worker's position)
    has legs expiring after today and is ignored, so this worker never
    manages another lane's position. The worker shorts the spread, so
    the position's market value is negative (what it would cost to buy
    back) and its cost basis is negative (the credit received).
    buyback = -value, credit = -basis, P&L = credit - buyback.
    Returns (0.0, 0.0, []) when flat."""
    today = datetime.now(TZ).date()
    legs = [p for p in get_positions(account)
            if str(p.get("symbol", "")).startswith("SPXW")
            and _expiry_from_symbol(str(p.get("symbol", ""))) == today]
    if not legs:
        return 0.0, 0.0, []
    symbols = ",".join(p["symbol"] for p in legs)
    data = api_get("/v1/markets/quotes?symbols=%s&greeks=false" % symbols)
    quotes = data["quotes"]["quote"]
    if isinstance(quotes, dict):
        quotes = [quotes]
    mids = {q["symbol"]: (float(q["bid"]) + float(q["ask"])) / 2.0 for q in quotes}
    value = 0.0
    basis = 0.0
    for leg in legs:
        mid = mids.get(leg["symbol"], 0.0)
        value += float(leg["quantity"]) * mid * 100.0
        basis += float(leg["cost_basis"])
    return -value, -basis, legs


def close_position(account, legs, reason):
    """Buy back the open spread at the mid. Cancels a blocking order
    first (an unfilled entry for the same strikes). The close is a
    DEBIT order for the buy-back cost — the worker is short the spread,
    so closing costs money; a negative or zero buy-back is floored at
    $0.01, since Tradier rejects a zero/negative price. Returns early
    when the position is already gone."""
    buyback, credit, fresh_legs = position_value_and_basis(account)
    if not fresh_legs:
        log("closing (%s): position already closed, nothing to do" % reason)
        return {"already_closed": True}
    legs = fresh_legs
    contracts = max(abs(int(leg["quantity"])) for leg in legs)
    price = round(buyback / (contracts * 100.0), 2)
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


def pnl_snapshot(account):
    bal = get_balances(account)
    buyback, credit, legs = position_value_and_basis(account)
    log("P&L equity=%.2f realized_today=%.2f unrealized=%.2f open_legs=%d"
        % (float(bal["equity"]), float(bal["close_pl"]),
           (credit - buyback) if legs else 0.0, len(legs)))


def heartbeat_line(now, legs, buyback, credit, spx_txt, flat_state):
    """One short status line per poll: flat/open, SPX, and state."""
    if legs:
        return ("heartbeat: open %d legs buyback %.2f on credit %.2f pnl %.2f SPX %s"
                % (len(legs), buyback, credit, credit - buyback, spx_txt))
    return "heartbeat: flat SPX %s %s" % (spx_txt, flat_state)


def run_once():
    if not API_KEY:
        log("TRADIER_API_KEY is not set, exiting")
        return 1
    account = resolve_account()
    log("account %s connected (paper host %s, trading %s, contracts %d, "
        "spread $%d, short %.1f%% OTM, EMA %d/%d 15-min, hard exit %s PT)"
        % (account, HOST, "ENABLED" if ENABLE_TRADING else "dry-run",
           CONTRACTS, SPREAD_WIDTH, OTM_PCT * 100, EMA_FAST, EMA_SLOW,
           HARD_EXIT.strftime("%H:%M")))
    last_pnl_day = {}
    # Working entry order: one limit credit order at a time. It stays
    # working while the trend still matches; it is cancelled when the
    # trend flips, the strikes move, or the entry window ends.
    working_entry = None
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
                    pnl_snapshot(account)
                    last_pnl_day[hour] = today
            buyback, credit, legs = position_value_and_basis(account)
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
            if legs:
                # A position means any earlier entry order has filled;
                # cancel a leftover working entry so it cannot open a
                # second position after this one closes.
                if working_entry is not None:
                    try:
                        cancel_order(account, working_entry["id"])
                        log("entry order %s cancelled after fill"
                            % working_entry["id"])
                    except Exception as exc:  # noqa: BLE001 - best effort
                        log("entry order %s cancel failed (%s), ignoring"
                            % (working_entry["id"], exc))
                    working_entry = None
                # HARD EXIT: 12:55 p.m. PT, 5 minutes before the close.
                # Closes whatever is open, at the mid, whatever its P&L.
                if now.time() >= HARD_EXIT:
                    log("hard exit 12:55 PT: closing open spread")
                    close_position(account, legs, "hard exit 12:55 PT")
                    time.sleep(POLL_SECONDS)
                    continue
                # Profit target: buy back at 50% of the credit received.
                if credit > 0 and buyback <= PROFIT_TARGET_FRACTION * credit:
                    log("profit target hit: buyback %.2f on credit %.2f"
                        % (buyback, credit))
                    close_position(account, legs, "50% profit target")
                    time.sleep(POLL_SECONDS)
                    continue
                # Stop: buy-back cost reached 2x the credit received.
                if credit > 0 and buyback >= STOP_MULTIPLE * credit:
                    log("stop hit: buyback %.2f on credit %.2f"
                        % (buyback, credit))
                    close_position(account, legs, "stop 2x credit")
                    time.sleep(POLL_SECONDS)
                    continue
            else:
                in_window = ENTRY_START <= now.time() < ENTRY_END
                # Cancel a working entry when the window ends: no entries
                # after 12:00 p.m. PT, so a stale order must not fill late.
                if working_entry is not None and not in_window:
                    try:
                        cancel_order(account, working_entry["id"])
                        log("entry order %s cancelled, window ended"
                            % working_entry["id"])
                    except Exception as exc:  # noqa: BLE001 - best effort
                        log("entry order %s cancel failed (%s), ignoring"
                            % (working_entry["id"], exc))
                    working_entry = None
                if not in_window:
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
                # A working entry is kept only while it still matches the
                # trend direction and the strikes. Otherwise cancel it
                # and re-place at the new strikes.
                if working_entry is not None:
                    if (working_entry["direction"] != direction
                            or working_entry["short_strike"] != short_strike
                            or working_entry["long_strike"] != long_strike):
                        try:
                            cancel_order(account, working_entry["id"])
                            log("entry order %s cancelled, trend/strikes moved"
                                % working_entry["id"])
                        except Exception as exc:  # noqa: BLE001 - best effort
                            log("entry order %s cancel failed (%s), ignoring"
                                % (working_entry["id"], exc))
                        working_entry = None
                    else:
                        # Same trend, same strikes: leave it working.
                        time.sleep(POLL_SECONDS)
                        continue
                log("entry: SPX %.2f %s EMA %d %.2f / EMA %d %.2f, %s %d/%d %s "
                    "limit %.2f x %d"
                    % (spx, direction, EMA_FAST, ema_fast, EMA_SLOW, ema_slow,
                       kind, short_strike, long_strike,
                       "bull put" if kind == "put" else "bear call",
                       spread_credit, CONTRACTS))
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
