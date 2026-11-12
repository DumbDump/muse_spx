#!/usr/bin/env python3
"""SPX double-calendar paper trader for Railway.

Implements one fixed rule set, nothing else:

- Tradier PAPER host only (sandbox.tradier.com). The host is hardcoded and
  the program refuses to run against any other host, so it cannot touch a
  live account.
- SPXW double calendars only: sell the nearer expiry put and call, buy the
  next expiry at the same strikes.
- Strikes $50 each side of the current SPX price, rounded to the nearest
  5-point strike.
- Short leg 3-5 DTE at entry, long leg the next expiry after the short leg.
  Any weekday pair qualifies.
- Entry window 8:00 a.m.-12:30 p.m. America/Los_Angeles, weekdays only.
- Walk-up entry, no day skip: while flat and in the window, the worker
  evaluates every poll (3 minutes). When the cheapest pair previews at
  or under $12.00, it bids $9.00 and walks up $0.10 every 30 seconds to
  $12.00, cancelling and replacing at each step; a fill at any step
  opens the position. An unfilled cycle cancels and waits 5 minutes
  before the next cycle. Pairs over $12.00 are logged with their actual
  price and retried next poll.
- Max 5 contracts per trade, one position at a time — enforced by a
  hard gate, not just the per-poll read: before a walk-up cycle starts,
  and before every walk-up step, the worker re-reads open positions
  and aborts if any SPXW calendar legs expiring after today are held.
  A stale or empty positions read at poll time can never open a second
  calendar, because the gate is re-checked fresh at entry time.
- Leg-collision avoidance: before starting a walk-up cycle, the worker
  reads every open SPXW position in the account. If either of the
  calendar's strikes (put or call) is already held by another
  strategy (e.g. the trend worker's 0DTE spread), the pair is skipped
  and retried next poll — the strikes move with SPX, so a later poll
  offers clear strikes. This keeps the worker from doubling up on a
  leg another lane already holds.
- Exit at a 10% gain on the debit basis, per calendar: as soon as an
  entry fills, the worker places a GTC credit order to close that
  calendar at 110% of the debit actually paid, so the exit triggers
  the moment the market touches it — even between polls or overnight.
  The poll-based 10% check stays as a fallback for when no
  take-profit order is working. Exits are always per calendar (4
  legs), never one combined order across calendars: a combined close
  is rejected by Tradier (Nov 11: 43 rejections of a 12-leg order). The standing
  order caps the gain at exactly 10%: it will not capture an overshoot
  above the line the way a poll-time close at the mid could. There is no
  end-of-day forced close: a position that never reaches 10% stays open
  (and blocks new entries) until it does.
- Loss exit: close at the mid when the position's value falls to 50% of
  the debit paid (STOP_LOSS_FRACTION, default 0.50). Time exit: close
  when the short leg has 1 day or less to expiry, so a position that
  never reaches 10% cannot ride through short-leg expiry.
- No preview step: when the rules say enter or exit, the order is placed
  directly. Every placement and fill is logged immediately.
- Every order carries a tag ("muse-spx" by default, overridable with
  ORDER_TAG) so the worker's own orders are identifiable.
- At most one pending order at a time: before placing, the worker checks
  open orders and skips if an order with its tag, or with the same leg
  symbols, is already pending. Walk-up steps cancel the worker's own
  working order before replacing it; it never cancels anyone else's order.

Safety:

- Paper orders are placed ONLY when the environment variable
  ENABLE_TRADING is set to "1". Otherwise the program runs in dry-run mode:
  it evaluates the rules, logs exactly what it would place, and places
  nothing. Deploy with dry-run first and flip the flag only after the logs
  look right.
- The Tradier API key comes from the TRADIER_API_KEY environment variable
  (set it in Railway, never in this file or in git).

Railway setup:

- Push this file plus Procfile to a repo, create a Railway project from it,
  and set TRADIER_API_KEY to a Tradier SANDBOX (paper) key.
- Procfile runs: worker: python paper_trader.py
- The worker loops forever, so Railway keeps it running. Logs are the
  P&L record: the program prints a heartbeat line every poll (so silence
  never means "is it running?"), a P&L line on the first poll of the
  6:00 a.m. and 8:00 a.m. PT hours on weekdays, plus a line for every
  entry, exit, skip, and error.

Notes:

- Tradier sandbox quotes are delayed about 15 minutes. Mids computed here
  are estimates, not live brokerage marks.
- Entry orders are day orders: an order left unfilled at the close
  expires with the session. The take-profit order is GTC and stays
  working across sessions. Positions are closed on the 10% gain exit,
  the 50% stop-loss, or the day before short-leg expiry, so a position
  may be carried overnight by design.

Usage:

  TRADIER_API_KEY=xxx ENABLE_TRADING=0 python paper_trader.py
  TRADIER_API_KEY=xxx python paper_trader.py --check   # one P&L snapshot, then exit
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
HOST = "sandbox.tradier.com"
BASE = "https://%s" % HOST

API_KEY = os.environ.get("TRADIER_API_KEY", "")
ENABLE_TRADING = os.environ.get("ENABLE_TRADING", "0") == "1"
CONTRACTS = max(1, min(5, int(os.environ.get("CONTRACTS", "5"))))
MAX_DEBIT = float(os.environ.get("MAX_DEBIT", "12.00"))
WALK_START = float(os.environ.get("WALK_START", "9.00"))
WALK_STEP = float(os.environ.get("WALK_STEP", "0.10"))
ENTRY_START = dtime(8, 0)
ENTRY_END = dtime(12, 30)
COOLDOWN_SECONDS = 300
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "180"))
STOP_LOSS_FRACTION = float(os.environ.get("STOP_LOSS_FRACTION", "0.50"))
ORDER_TAG = os.environ.get("ORDER_TAG", "muse-spx")


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
    # crashing on .get(). This bit us on 2026-11-03: every poll errored
    # from the moment the account went flat until this guard was added.
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
    # Sandbox sometimes returns JSON null here; raise a clean error so the
    # poll retries instead of dying on a NoneType subscript.
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
    NOTE: Tradier's expirations endpoint takes the underlying SPX, not the
    weekly root SPXW — SPXW returns HTTP 200 with JSON null. And
    includeAllRoots=true is required, otherwise only monthly expiries come
    back and the 3-5 DTE rule can never match."""
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


def get_chain_mids(expiry):
    """Return {(strike, kind): mid} for one expiry, or {} when the endpoint
    returns no usable chain data (JSON null). Callers treat {} as 'this
    expiry skipped', never as a crash.
    NOTE: chains take the underlying SPX; SPXW returns JSON null. The
    returned option symbols are still SPXW, which is what legs use."""
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
    mids = {}
    for opt in options:
        try:
            mids[(float(opt["strike"]), opt["option_type"])] = (
                float(opt["bid"]) + float(opt["ask"])
            ) / 2.0
        except (KeyError, TypeError, ValueError):
            continue
    return mids


def find_best_pair(spx_price, expirations):
    """Return (qualifying, cheapest): the cheapest pair at or under
    MAX_DEBIT (or None), plus the cheapest pair at any debit (or None
    when no candidate had full chain data). The cheapest pair is what
    the skip line prints, so the log shows the actual price even on a
    skipped day."""
    put_strike = round_strike(spx_price - 50)
    call_strike = round_strike(spx_price + 50)
    today = datetime.now(TZ).date()
    best = None
    cheapest = None
    saw_chains = False
    for i, short_exp in enumerate(expirations):
        dte = (short_exp - today).days
        if dte < 3 or dte > 5:
            continue
        if i + 1 >= len(expirations):
            continue
        long_exp = expirations[i + 1]
        short_mids = get_chain_mids(short_exp)
        long_mids = get_chain_mids(long_exp)
        if short_mids or long_mids:
            saw_chains = True
        try:
            debit = (
                long_mids[(put_strike, "put")]
                + long_mids[(call_strike, "call")]
                - short_mids[(put_strike, "put")]
                - short_mids[(call_strike, "call")]
            )
        except KeyError:
            continue
        candidate = (short_exp, long_exp, put_strike, call_strike, round(debit, 2))
        if cheapest is None or candidate[4] < cheapest[4]:
            cheapest = candidate
        if debit <= MAX_DEBIT and (best is None or debit < best[4]):
            best = candidate
    if not saw_chains:
        # No chain data at all: a transient endpoint outage, not a "no
        # qualifying pair" day. Raise so the poll retries instead of
        # marking today skipped.
        raise ValueError("no option chains data from Tradier")
    return best, cheapest


def get_pending_orders(account):
    """Return working orders (status pending/open) with leg symbols and tag."""
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
    """True when the worker already has a pending order that would collide
    with the given legs: same tag, or the same leg symbols. Matching on leg
    symbols means the check works even for orders placed before tagging, and
    even if the provider does not echo the tag back. exclude_id skips one
    order the caller just cancelled, so a stale feed listing it does not
    block the replacement close. ignore_credit is True only for debit
    placements: a credit order is a take-profit for a position, and when
    the worker is flat it is stale (the position closed another way) —
    Tradier may also refuse to cancel it (Nov 10: GTC 41563112, HTTP 400
    'order not available to be canceled'), so a debit entry must not wait
    on it. Credit placements (take-profit, credit close) keep the old
    rule, so no duplicate GTC is ever placed."""
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
        log("DRY RUN: would place %s order at %.2f: %s" % (order_type, price, legs))
        return {"dry_run": True}
    resp = api_post_order(account, fields)
    order = resp.get("order", resp)
    log("PLACED %s order id=%s price=%.2f legs=%s" % (order_type, order.get("id"), price, legs))
    return resp


def _expiry_from_symbol(symbol):
    """Expiry date parsed from an option symbol (the first 6-digit
    YYMMDD run). Returns None when no expiry parses."""
    match = re.search(r"(\d{6})", symbol)
    if not match:
        return None
    code = match.group(1)
    return date(2000 + int(code[0:2]), int(code[2:4]), int(code[4:6]))


def _strike_from_symbol(symbol):
    """Strike parsed from an option symbol (SPXW261116P07555000 -> 7555).
    Returns None when no strike parses."""
    match = re.search(r"(\d{6})([PC])(\d{8})", symbol)
    if not match:
        return None
    return int(match.group(3)) // 1000


def group_calendar_legs(legs):
    """Group open legs into double calendars.

    One calendar = short put + short call (same short expiry) + long put
    + long call (next expiry, same strikes). Returns a list of
    ((short_exp, long_exp, put_strike, call_strike), legs) in expiry
    order. A leg that matches no calendar (e.g. a stray single leg) is
    returned as its own one-leg group so it is never silently merged
    into another calendar's close."""
    today = datetime.now(TZ).date()
    shorts = [l for l in legs if float(l["quantity"]) < 0]
    longs = [l for l in legs if float(l["quantity"]) > 0]
    groups = []
    used = set()
    # Calendars are keyed off their short legs. All calendars opened
    # in one week share the same short expiry, so expiry alone cannot
    # separate them — pair by strike instead: this worker always sets
    # strikes $50 each side of SPX, so a calendar's call strike is its
    # put strike + 100 (7555/7655, 7560/7660, 7575/7675). Each short
    # put anchors one calendar; its short call is the call at the same
    # expiry with strike put+100, and its long legs are the same
    # strikes at the next expiry.
    short_puts = [l for l in shorts
                  if re.search(r"\d{6}P\d{8}", l["symbol"])]
    for sp in short_puts:
        short_exp = _expiry_from_symbol(sp["symbol"])
        put = _strike_from_symbol(sp["symbol"])
        if short_exp is None or put is None:
            continue
        call = put + 100
        sc = next((l for l in shorts
                   if _expiry_from_symbol(l["symbol"]) == short_exp
                   and _strike_from_symbol(l["symbol"]) == call
                   and re.search(r"\d{6}C\d{8}", l["symbol"])), None)
        if sc is None:
            continue
        long_exps = sorted({_expiry_from_symbol(l["symbol"]) for l in longs
                            if _expiry_from_symbol(l["symbol"]) is not None
                            and _expiry_from_symbol(l["symbol"]) > short_exp})
        if not long_exps:
            continue
        long_exp = long_exps[0]
        lp = next((l for l in longs
                   if _expiry_from_symbol(l["symbol"]) == long_exp
                   and _strike_from_symbol(l["symbol"]) == put), None)
        lc = next((l for l in longs
                   if _expiry_from_symbol(l["symbol"]) == long_exp
                   and _strike_from_symbol(l["symbol"]) == call), None)
        if lp is None or lc is None:
            continue
        cal = [sp, sc, lp, lc]
        for l in cal:
            used.add(l["symbol"])
        groups.append(((short_exp, long_exp, put, call), cal))
    for l in legs:
        if l["symbol"] not in used:
            groups.append(((None, None, None, None), [l]))
    return groups


def calendar_values(account):
    """Return one (value, basis, legs) tuple per open calendar, instead
    of one aggregate across all calendars. Each tuple is the 4 legs of
    a single double calendar, valued on mids, with its own cost basis.
    The aggregate position_value_and_basis() is kept for the P&L /
    heartbeat reporting, which wants the whole-book number."""
    today = datetime.now(TZ).date()
    legs = [p for p in get_positions(account)
            if p["symbol"].startswith("SPXW")
            and _expiry_from_symbol(p["symbol"]) is not None
            and _expiry_from_symbol(p["symbol"]) > today]
    if not legs:
        return []
    symbols = ",".join(p["symbol"] for p in legs)
    data = api_get("/v1/markets/quotes?symbols=%s&greeks=false" % symbols)
    quotes = data["quotes"]["quote"]
    if isinstance(quotes, dict):
        quotes = [quotes]
    mids = {q["symbol"]: (float(q["bid"]) + float(q["ask"])) / 2.0 for q in quotes}
    out = []
    for key, cal_legs in group_calendar_legs(legs):
        value = sum(float(l["quantity"]) * mids.get(l["symbol"], 0.0) * 100.0
                    for l in cal_legs)
        basis = sum(float(l["cost_basis"]) for l in cal_legs)
        out.append((value, basis, cal_legs))
    return out


def position_value_and_basis(account):
    """Return (net_value, net_basis, legs) for open SPXW legs.
    Only this worker's own double-calendar legs are returned: legs
    expiring after today. A 0DTE vertical (the trend worker's spread)
    expires today and is ignored, so this worker never closes another
    lane's position. (Nov 11: the trend worker's first spread was
    closed 68 seconds after its fill by this worker's time exit, which
    fires on any short leg expiring within 1 day — a 0DTE spread
    qualifies. This filter stops that cross-worker interference.) The
    worker closes its own calendar at short-leg DTE <= 1, so its legs
    never expire today in normal operation."""
    today = datetime.now(TZ).date()
    legs = [p for p in get_positions(account)
            if p["symbol"].startswith("SPXW")
            and _expiry_from_symbol(p["symbol"]) is not None
            and _expiry_from_symbol(p["symbol"]) > today]
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
    return value, basis, legs


def short_expiry_from_legs(legs):
    """Expiry date of the short leg, parsed from the option symbols.

    The first 6-digit run in a symbol is the YYMMDD expiry
    (SPXW261112... and QQQ261116... alike). The short leg is the
    earliest-expiring leg with a negative quantity. Returns None when
    no expiry parses."""
    expiries = []
    for leg in legs:
        if float(leg["quantity"]) >= 0:
            continue
        match = re.search(r"(\d{6})", leg["symbol"])
        if match:
            code = match.group(1)
            expiries.append(date(2000 + int(code[0:2]), int(code[2:4]), int(code[4:6])))
    return min(expiries) if expiries else None


def close_legs_for(legs):
    """Closing legs for open legs: buy back shorts, sell longs."""
    out = []
    for leg in legs:
        side = "buy_to_close" if float(leg["quantity"]) < 0 else "sell_to_close"
        out.append((side, leg["symbol"], abs(int(leg["quantity"]))))
    return out


def place_take_profit(account, legs, basis):
    """Place a GTC credit order to close the position at a 10% gain.

    The order sits at 110% of the debit basis, so the exit triggers the
    moment the market touches it — even between polls, overnight, or
    across a restart. Returns True when a take-profit order is now
    working (already pending or just placed), False when nothing was
    placed (dry-run, or placement failed and the caller should retry
    next poll). Silent when an order is already working, so the
    per-poll check does not spam the log."""
    contracts = max(abs(int(leg["quantity"])) for leg in legs)
    target = round(basis * 1.10 / (contracts * 100.0), 2)
    close_legs = close_legs_for(legs)
    blocked, _ = has_blocking_pending_order(account, close_legs)
    if blocked:
        return True
    resp = place_order(account, "credit", target, close_legs, duration="gtc")
    if resp.get("blocked"):
        return True
    if resp.get("dry_run"):
        return False
    order = resp.get("order", resp)
    log("take-profit: placed GTC credit order id=%s at %.2f (10%% on basis %.2f)"
        % (order.get("id"), target, basis))
    return True


def close_position(account, legs):
    """Build and place the closing order for the given open legs.

    Cancels the working take-profit order first: the one-pending rule
    would otherwise block the close, since the take-profit carries the
    same leg symbols. If the cancel fails, the close is skipped and the
    next poll retries. Returns early when the position is already gone
    (e.g. the take-profit filled just before this poll).

    A position that has lost more than its debit is worth less than
    zero: closing it costs money, so the close is a DEBIT order for the
    absolute value, not a credit order at a negative price — Tradier
    rejects negative prices, which left the Nov 10 SPX position with no
    working order after its stop-loss fired."""
    # Value ONLY the legs passed in: this is one calendar, not the
    # whole book. The old code re-read every open leg here, so with
    # three calendars open it built one 12-leg combined close order
    # and Tradier rejected it 43 times (Nov 11, 10:57 a.m.-1:03 p.m.).
    # A passed leg that is no longer held means this calendar already
    # closed (e.g. its take-profit filled just before this poll).
    symbols = {leg["symbol"] for leg in legs}
    fresh_legs = [p for p in get_positions(account)
                  if p["symbol"] in symbols]
    if not fresh_legs:
        log("closing: position already closed, nothing to do")
        return {"already_closed": True}
    legs = fresh_legs
    data = api_get("/v1/markets/quotes?symbols=%s&greeks=false"
                   % ",".join(leg["symbol"] for leg in legs))
    quotes = data["quotes"]["quote"]
    if isinstance(quotes, dict):
        quotes = [quotes]
    mids = {q["symbol"]: (float(q["bid"]) + float(q["ask"])) / 2.0 for q in quotes}
    value = sum(float(leg["quantity"]) * mids.get(leg["symbol"], 0.0) * 100.0
                for leg in legs)
    basis = sum(float(leg["cost_basis"]) for leg in legs)
    contracts = max(abs(int(leg["quantity"])) for leg in legs)
    credit = round(value / (contracts * 100.0), 2)
    close_legs = close_legs_for(legs)
    blocked, blocking_id = has_blocking_pending_order(account, close_legs)
    if blocked:
        try:
            cancel_order(account, blocking_id)
            log("closing: cancelled working order %s to place the close" % blocking_id)
        except Exception as exc:  # noqa: BLE001 - retry next poll
            log("closing: cancel of order %s failed (%s), close skipped, will retry"
                % (blocking_id, exc))
            return {"blocked": True, "blocking_order_id": blocking_id}
    if credit < 0:
        order_type, price = "debit", -credit
    else:
        order_type, price = "credit", max(credit, 0.01)
    log("closing: value=%.2f basis=%.2f unrealized=%.2f %s=%.2f"
        % (value, basis, value - basis, order_type, price))
    # exclude_order_id: the cancel above just removed this order, but a
    # stale feed may still list it — do not let it block its own close.
    return place_order(account, order_type, price, close_legs,
                       exclude_order_id=blocking_id if blocked else None)


def get_order_status(account, order_id):
    """Return the status string of one order ('filled', 'open', ...)."""
    data = api_get("/v1/accounts/%s/orders/%s" % (account, order_id))
    order = data.get("order", data)
    if isinstance(order, list):
        order = order[0] if order else {}
    return order.get("status")


def cancel_order(account, order_id):
    """Cancel one working order. Tradier cancels via DELETE on the order."""
    req = urllib.request.Request(
        BASE + "/v1/accounts/%s/orders/%s" % (account, order_id), method="DELETE"
    )
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", "Bearer %s" % API_KEY)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


_STALE_TAKE_PROFIT_CANCEL_TRIED = set()


def cleanup_stale_take_profit_orders(account):
    """Best-effort cancel of take-profit (credit) orders left working
    after their position closed another way (a manual close, or a fill
    the worker missed). Called while flat, so a pending credit order
    for this worker's underlying has no position behind it and is
    stale. Each order id is attempted once per process run: Tradier
    may refuse the cancel (Nov 10: GTC 41563112, HTTP 400 'order not
    available to be canceled'), and retrying a refused cancel every
    poll would spam the log — the entry path ignores credit orders
    anyway, so a refused cancel is logged once and the order is left
    working. Only credit orders whose legs are all this worker's
    underlying are touched."""
    try:
        pending = get_pending_orders(account)
    except Exception as exc:  # noqa: BLE001 - retry next poll
        log("stale-take-profit cleanup: pending orders read failed (%s)" % exc)
        return
    for order in pending:
        if order.get("type") != "credit":
            continue
        symbols = order.get("symbols") or set()
        if not symbols or not all(s.startswith("SPXW") for s in symbols):
            continue
        # Only this worker's own take-profit GTCs: their legs expire
        # after today. A 0DTE vertical (the trend worker's entry) has
        # legs expiring today and must never be cancelled here.
        today = datetime.now(TZ).date()
        if not all(_expiry_from_symbol(s) is not None
                   and _expiry_from_symbol(s) > today for s in symbols):
            continue
        order_id = str(order["id"])
        if order_id in _STALE_TAKE_PROFIT_CANCEL_TRIED:
            continue
        _STALE_TAKE_PROFIT_CANCEL_TRIED.add(order_id)
        try:
            cancel_order(account, order["id"])
            log("stale-take-profit cleanup: cancelled order %s" % order["id"])
        except Exception as exc:  # noqa: BLE001 - entry path ignores it
            log("stale-take-profit cleanup: cancel of order %s refused (%s), "
                "entry path ignores it" % (order["id"], exc))


def held_spxw_strikes(account):
    """Strikes currently held in any open SPXW position in the paper
    account, whatever lane opened them. Used to avoid opening a
    calendar on a strike another strategy already holds (e.g. the
    trend worker's 0DTE spread). Returns a set of strike prices."""
    strikes = set()
    try:
        for p in get_positions(account):
            sym = str(p.get("symbol", ""))
            if not sym.startswith("SPXW"):
                continue
            strike = _strike_from_symbol(sym)
            if strike is not None:
                strikes.add(float(strike))
    except Exception as exc:  # noqa: BLE001 - a failed read is not "no strikes held"
        log("held-strikes read failed (%s), treating as no strikes held" % exc)
    return strikes


def has_open_calendar(account):
    """True when the paper account holds any open SPXW calendar legs
    expiring after today — i.e. this worker already has a position.
    This is the hard one-position gate: it re-reads positions fresh
    from Tradier (never trusting a cached or poll-start read), so a
    stale or empty positions feed at poll time cannot let a second
    calendar open. The trend worker's 0DTE legs expire today and are
    excluded, so they never block this worker's entry."""
    today = datetime.now(TZ).date()
    try:
        for p in get_positions(account):
            sym = str(p.get("symbol", ""))
            if not sym.startswith("SPXW"):
                continue
            exp = _expiry_from_symbol(sym)
            if exp is not None and exp > today:
                return True
    except Exception as exc:  # noqa: BLE001 - a failed read is not "flat"
        log("one-position gate: positions read failed (%s), treating as holding" % exc)
        return True
    return False


def run_walkup_cycle(account, short_exp, long_exp, put, call):
    """Bid a pair from WALK_START up to MAX_DEBIT in WALK_STEP steps.

    Places a debit limit order, waits 30 seconds, and if it is still
    working, cancels it and re-places one step higher. Returns True when
    the order filled (or partially filled, leaving a position), False
    when the walk reached the ceiling unfilled or was blocked. A cycle
    started inside the entry window is allowed to finish even if the
    window ends mid-cycle. Only the worker's own tagged order is ever
    cancelled, and only after the one-pending check passes."""
    legs = [
        ("sell_to_open", option_symbol(short_exp, put, "P"), CONTRACTS),
        ("sell_to_open", option_symbol(short_exp, call, "C"), CONTRACTS),
        ("buy_to_open", option_symbol(long_exp, put, "P"), CONTRACTS),
        ("buy_to_open", option_symbol(long_exp, call, "C"), CONTRACTS),
    ]
    log("walk-up: cycle starting short %s / long %s %dP/%dC"
        % (short_exp, long_exp, put, call))
    price = WALK_START
    while price <= MAX_DEBIT + 1e-9:
        price = round(price, 2)
        # Hard one-position gate, re-checked before every step: if a
        # calendar opened since the cycle started (a fill the poll-start
        # read missed, or a position from another lane), abort the
        # cycle instead of opening a second calendar.
        if has_open_calendar(account):
            log("walk-up: a calendar is already open, cycle aborted at %.2f" % price)
            return False
        resp = place_order(account, "debit", price, legs)
        if resp.get("blocked"):
            log("walk-up: blocked by an existing pending order, cycle aborted")
            return False
        if resp.get("dry_run"):
            return False
        order = resp.get("order", resp)
        order_id = order.get("id")
        if not order_id:
            log("walk-up: order response without id, cycle aborted: %s" % resp)
            return False
        time.sleep(30)
        try:
            status = get_order_status(account, order_id)
        except Exception as exc:  # noqa: BLE001 - cancel anyway below
            log("walk-up: status check failed (%s), cancelling order %s"
                % (exc, order_id))
            status = None
        if status == "filled":
            log("walk-up: filled at %.2f (order %s)" % (price, order_id))
            return True
        if status in ("open", "pending", "partially_filled", None):
            try:
                cancel_order(account, order_id)
            except Exception as exc:  # noqa: BLE001 - re-check below
                log("walk-up: cancel of order %s failed (%s)" % (order_id, exc))
            if status == "partially_filled":
                log("walk-up: partially filled at %.2f (order %s), stopping cycle"
                    % (price, order_id))
                return True
            # Re-check: it may have filled while the cancel was in flight.
            try:
                if get_order_status(account, order_id) == "filled":
                    log("walk-up: filled at %.2f (order %s)" % (price, order_id))
                    return True
            except Exception:
                pass
            log("walk-up: %.2f unfilled, cancelled, walking up" % price)
        else:
            log("walk-up: order %s ended as %s at %.2f"
                % (order_id, status, price))
        price = round(price + WALK_STEP, 2)
    log("walk-up: reached %.2f unfilled, cycle done" % MAX_DEBIT)
    return False


def pnl_snapshot(account):
    bal = get_balances(account)
    value, basis, legs = position_value_and_basis(account)
    log("P&L equity=%.2f realized_today=%.2f unrealized=%.2f open_legs=%d"
        % (float(bal["equity"]), float(bal["close_pl"]), value - basis if legs else 0.0,
           len(legs)))


def heartbeat_line(now, legs, value, basis, spx_txt, flat_state):
    """One short status line per poll: flat/open, SPX, and state."""
    if legs:
        unrealized = value - basis
        return ("heartbeat: open %d legs unrealized %.2f on basis %.2f SPX %s"
                % (len(legs), unrealized, basis, spx_txt))
    return "heartbeat: flat SPX %s %s" % (spx_txt, flat_state)


def run_once():
    if not API_KEY:
        log("TRADIER_API_KEY is not set, exiting")
        return 1
    account = resolve_account()
    log("account %s connected (paper host %s, trading %s, contracts %d)"
        % (account, HOST, "ENABLED" if ENABLE_TRADING else "dry-run", CONTRACTS))
    last_pnl_day = {}
    next_cycle_time = None
    while True:
        try:
            now = datetime.now(TZ)
            today = now.date()
            if now.weekday() >= 5:
                time.sleep(POLL_SECONDS)
                continue
            # Daily P&L lines at 6:00 and 8:00 a.m. PT.
            for hour in (6, 8):
                key = (today, hour)
                # First poll of the hour, not minute < 2: with 5-minute
                # polling the 2-minute window was usually missed (Nov 4
                # printed neither P&L line).
                if now.hour == hour and last_pnl_day.get(hour) != today:
                    pnl_snapshot(account)
                    last_pnl_day[hour] = today
            value, basis, legs = position_value_and_basis(account)
            # Heartbeat every poll: one short line so the log always shows
            # the worker is alive and what it is doing.
            try:
                spx_txt = "%.2f" % get_spx_price()
            except Exception:
                spx_txt = "?"
            if legs:
                flat_state = ""
            elif ENTRY_START <= now.time() < ENTRY_END:
                if next_cycle_time is not None and now < next_cycle_time:
                    flat_state = ("cooling down until %s"
                                  % next_cycle_time.strftime("%H:%M"))
                else:
                    flat_state = "in window, will evaluate"
            else:
                flat_state = "waiting for window"
            log(heartbeat_line(now, legs, value, basis, spx_txt, flat_state))
            if legs:
                # One calendar at a time: each open calendar gets its
                # own stop / time / 10% exit and its own 4-leg GTC
                # take-profit at 110% of its own basis. The old code
                # ran all of this on the aggregate of every open
                # calendar, which built a single 12-leg combined close
                # that Tradier rejects — and left the 7555/7560
                # calendars (Nov 11) with no working take-profit at
                # all. This loop also heals those: on the first poll
                # after deploy, each calendar lacking a GTC gets one.
                for cal_value, cal_basis, cal_legs in calendar_values(account):
                    unrealized = cal_value - cal_basis
                    # Loss exit: close at the mid when the calendar has
                    # lost half its debit (value at or under
                    # STOP_LOSS_FRACTION of basis).
                    if cal_basis > 0 and cal_value <= STOP_LOSS_FRACTION * cal_basis:
                        log("stop-loss hit: value %.2f on basis %.2f" % (cal_value, cal_basis))
                        close_position(account, cal_legs)
                        continue
                    # Time exit: close when the short leg has 1 day or
                    # less to expiry.
                    short_exp = short_expiry_from_legs(cal_legs)
                    if short_exp is not None and (short_exp - today).days <= 1:
                        log("short-leg expiry close: short leg expires %s" % short_exp)
                        close_position(account, cal_legs)
                        continue
                    # 10% gain exit, any time of day: the fallback for
                    # when no take-profit order is working.
                    if cal_basis > 0 and unrealized >= 0.10 * cal_basis:
                        log("10%% exit hit: unrealized %.2f on basis %.2f" % (unrealized, cal_basis))
                        close_position(account, cal_legs)
                        continue
                    # Keep a standing take-profit in place per
                    # calendar, silent when one is already working.
                    if cal_basis > 0:
                        place_take_profit(account, cal_legs, cal_basis)
            else:
                # Clean up a stale take-profit first: a credit order left
                # working after its position closed another way must not
                # block the next entry (the entry path ignores credit
                # orders, but the stale order should still be cancelled
                # when Tradier allows it).
                cleanup_stale_take_profit_orders(account)
                # Walk-up entry, no day skip: while flat and in the
                # window, evaluate every poll; an unfilled cycle cools
                # down for 5 minutes before the next one.
                if (ENTRY_START <= now.time() < ENTRY_END
                        and (next_cycle_time is None or now >= next_cycle_time)):
                    spx = get_spx_price()
                    pair, cheapest = find_best_pair(spx, get_expirations())
                    if pair is None:
                        if cheapest is not None:
                            log("no pair at or under %.2f "
                                "(SPX %.2f, best pair %.2f short %s / long %s "
                                "%dP/%dC), will retry next poll"
                                % (MAX_DEBIT, spx, cheapest[4], cheapest[0],
                                   cheapest[1], cheapest[2], cheapest[3]))
                        else:
                            log("no pair at or under %.2f "
                                "(SPX %.2f, no candidate pair had full chain data), "
                                "will retry next poll"
                                % (MAX_DEBIT, spx))
                    else:
                        short_exp, long_exp, put, call, debit = pair
                        # Leg-collision avoidance: if either of this
                        # calendar's strikes is already held in an open
                        # SPXW position (any lane — e.g. the trend
                        # worker's 0DTE spread), skip this pair and
                        # retry next poll. The strikes move with SPX,
                        # so a later poll will offer clear strikes.
                        held = held_spxw_strikes(account)
                        if float(put) in held or float(call) in held:
                            log("entry: skipping pair %dP/%dC, strike%s held "
                                "(SPX %.2f), will retry next poll"
                                % (put, call,
                                   "s" if (float(put) in held
                                           and float(call) in held) else "",
                                   spx))
                            time.sleep(POLL_SECONDS)
                            continue
                        # Hard one-position gate, checked fresh right
                        # before the cycle: the poll-start positions read
                        # may be stale, so never start a walk-up on its
                        # word alone.
                        if has_open_calendar(account):
                            log("entry: a calendar is already open, skipping entry "
                                "(SPX %.2f)" % spx)
                            time.sleep(POLL_SECONDS)
                            continue
                        log("entry: SPX %.2f short %s / long %s %dP/%dC modeled debit %.2f x %d"
                            % (spx, short_exp, long_exp, put, call, debit, CONTRACTS))
                        filled = run_walkup_cycle(account, short_exp, long_exp, put, call)
                        if filled:
                            # Place the take-profit immediately: a GTC
                            # credit order at 110% of the debit actually
                            # paid. If the fill has not shown in
                            # positions yet, the per-poll check above
                            # places it on the next poll.
                            for _, fill_basis, fill_legs in calendar_values(account):
                                if fill_legs and fill_basis > 0:
                                    place_take_profit(account, fill_legs, fill_basis)
                        else:
                            next_cycle_time = datetime.now(TZ) + timedelta(minutes=5)
        except Exception as exc:  # noqa: BLE001 - keep the worker alive
            log("error: %s" % exc)
        time.sleep(POLL_SECONDS)
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        if not API_KEY:
            print("TRADIER_API_KEY is not set", file=sys.stderr)
            sys.exit(1)
        account = resolve_account()
        pnl_snapshot(account)
        sys.exit(0)
    sys.exit(run_once())
