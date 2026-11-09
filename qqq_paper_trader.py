#!/usr/bin/env python3
"""QQQ double-calendar paper trader for Railway.

Implements one fixed rule set, nothing else. It mirrors the SPX worker
(paper_trader.py) with QQQ parameters:

- Tradier PAPER host only (sandbox.tradier.com). The host is hardcoded and
  the program refuses to run against any other host, so it cannot touch a
  live account.
- QQQ double calendars only: sell the nearer expiry put and call, buy the
  later expiry at the same strikes.
- Strikes $15 each side of the current QQQ price, rounded to the nearest
  1-point strike. ($15 is roughly 2.3% of the underlying.)
- Short leg 6 DTE at entry (any expiry 5-7 DTE qualifies), long leg the
  next expiry after the short leg. Any weekday pair qualifies.
- Entry window 6:45 a.m.-12:30 p.m. America/Los_Angeles, weekdays only.
- Walk-up entry, no day skip: while flat and in the window, the worker
  evaluates every poll (3 minutes). When the cheapest pair previews at
  or under MAX_DEBIT, it bids from max(WALK_START, modeled debit minus
  WALK_START_OFFSET) and walks up WALK_STEP every 30 seconds to
  MAX_DEBIT, cancelling and replacing at each step; a fill at any step
  opens the position. Starting near the modeled debit (instead of a
  fixed low bid) skips steps that could never fill: on Mon Nov 9 the
  first cycle climbed 26 steps from $0.60 to a $1.85 fill against a
  $1.89 model. An unfilled cycle cancels and waits 5 minutes
  before the next cycle. Pairs over MAX_DEBIT are logged with their actual
  price and retried next poll.
- While holding a position, the worker polls every HOLD_POLL_SECONDS
  (60 seconds by default) so the 10% exit fires close to the line;
  while flat it polls every POLL_SECONDS (3 minutes).
- 10 contracts per trade, one position at a time.
- Exit at a 10% gain on the debit basis: as soon as an entry fills, the
  worker places a GTC credit order to close the position at 110% of the
  debit actually paid, so the exit triggers the moment the market touches
  it — even between polls or overnight. The poll-based 10% check stays
  as a fallback for when no take-profit order is working. The standing
  order caps the gain at exactly 10%: it will not capture an overshoot
  above the line the way a poll-time close at the mid could. There is no
  end-of-day forced close: a position that never reaches 10% stays open
  (and blocks new entries) until it does.
- No preview step: when the rules say enter or exit, the order is placed
  directly. Every placement and fill is logged immediately.
- Every order carries a tag ("muse-qqq" by default, overridable with
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

- Push this file plus Procfile to a repo, create a Railway service from it
  running the qqq-worker process type, and set TRADIER_API_KEY to a
  Tradier SANDBOX (paper) key.
- Procfile runs: qqq-worker: python qqq_paper_trader.py
- The worker loops forever, so Railway keeps it running. Logs are the
  P&L record: the program prints a heartbeat line every poll (so silence
  never means "is it running?"), a P&L line on the first poll of the
  6:00 a.m. and 8:00 a.m. PT hours on weekdays, plus a line for every
  entry, exit, skip, and error.

Notes:

- Tradier sandbox quotes are delayed about 15 minutes. Mids computed here
  are estimates, not live brokerage marks.
- This program places day orders. An order left unfilled at the close
  expires with the session. Positions are closed only on the 10% gain
  exit, so a position may be carried overnight by design.
- Unlike SPX (where the expirations/chains endpoints take the underlying
  SPX while legs are SPXW), QQQ is its own root: expirations, chains, and
  legs all use QQQ.

Usage:

  TRADIER_API_KEY=xxx ENABLE_TRADING=0 python qqq_paper_trader.py
  TRADIER_API_KEY=xxx python qqq_paper_trader.py --check   # one P&L snapshot, then exit
"""

import json
import os
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
CONTRACTS = max(1, min(10, int(os.environ.get("CONTRACTS", "10"))))
MAX_DEBIT = float(os.environ.get("MAX_DEBIT", "0.80"))
WALK_START = float(os.environ.get("WALK_START", "0.60"))
WALK_STEP = float(os.environ.get("WALK_STEP", "0.05"))
WALK_START_OFFSET = float(os.environ.get("WALK_START_OFFSET", "0.30"))
HOLD_POLL_SECONDS = int(os.environ.get("HOLD_POLL_SECONDS", "60"))
STRIKE_WIDTH = float(os.environ.get("STRIKE_WIDTH", "15"))
SHORT_DTE_MIN = int(os.environ.get("SHORT_DTE_MIN", "5"))
SHORT_DTE_MAX = int(os.environ.get("SHORT_DTE_MAX", "7"))
ENTRY_START = dtime(6, 45)
ENTRY_END = dtime(12, 30)
COOLDOWN_SECONDS = 300
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "180"))
ORDER_TAG = os.environ.get("ORDER_TAG", "muse-qqq")


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


def get_qqq_price():
    data = api_get("/v1/markets/quotes?symbols=QQQ&greeks=false")
    quotes_obj = data.get("quotes")
    # Sandbox sometimes returns JSON null here; raise a clean error so the
    # poll retries instead of dying on a NoneType subscript.
    if not isinstance(quotes_obj, dict):
        raise ValueError("QQQ quote unavailable (null quotes)")
    quote = quotes_obj.get("quote")
    if isinstance(quote, list):
        quote = quote[0] if quote else None
    if not isinstance(quote, dict) or quote.get("last") is None:
        raise ValueError("QQQ quote unavailable (null quote)")
    return float(quote["last"])


def round_strike(price):
    return int(round(price / 1.0) * 1)


def option_symbol(expiry, strike, kind):
    return "QQQ%s%s%08d" % (expiry.strftime("%y%m%d"), kind, strike * 1000)


def get_expirations():
    """Return sorted expiry dates. Falls back to weekday-derived dates.
    QQQ is its own root, so the expirations endpoint takes symbol=QQQ
    directly (unlike SPX, where SPXW returns JSON null)."""
    try:
        data = api_get("/v1/markets/options/expirations?symbol=QQQ&includeAllRoots=true")
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
    expiry skipped', never as a crash."""
    data = api_get(
        "/v1/markets/options/chains?symbol=QQQ&expiration=%s&greeks=false"
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


def find_best_pair(qqq_price, expirations):
    """Return (qualifying, cheapest): the cheapest pair at or under
    MAX_DEBIT (or None), plus the cheapest pair at any debit (or None
    when no candidate had full chain data). The cheapest pair is what
    the skip line prints, so the log shows the actual price even on a
    skipped day. Like the SPX worker, the long leg is the next expiry
    after the short leg (a 6/7 DTE pair when the short is 6 DTE)."""
    put_strike = round_strike(qqq_price - STRIKE_WIDTH)
    call_strike = round_strike(qqq_price + STRIKE_WIDTH)
    today = datetime.now(TZ).date()
    best = None
    cheapest = None
    saw_chains = False
    for i, short_exp in enumerate(expirations):
        dte = (short_exp - today).days
        if dte < SHORT_DTE_MIN or dte > SHORT_DTE_MAX:
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
            "symbols": symbols,
        })
    return pending


def has_blocking_pending_order(account, legs):
    """True when the worker already has a pending order that would collide
    with the given legs: same tag, or the same leg symbols. Matching on leg
    symbols means the check works even for orders placed before tagging, and
    even if the provider does not echo the tag back."""
    new_symbols = {symbol for _, symbol, _ in legs}
    for order in get_pending_orders(account):
        if order["tag"] == ORDER_TAG or order["symbols"] == new_symbols:
            return True, order["id"]
    return False, None


def place_order(account, order_type, price, legs, duration="day"):
    """legs: list of (side, symbol, qty). Returns the API response dict.
    Never creates a second pending order: if a colliding order is already
    pending, the placement is skipped and logged."""
    blocked, blocking_id = has_blocking_pending_order(account, legs)
    if blocked:
        log("SKIPPED %s order at %.2f: pending order %s already exists"
            % (order_type, price, blocking_id))
        return {"blocked": True, "blocking_order_id": blocking_id}
    fields = {
        "class": "multileg",
        "symbol": "QQQ",
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


def position_value_and_basis(account):
    """Return (net_value, net_basis, legs) for open QQQ legs."""
    legs = [p for p in get_positions(account) if p["symbol"].startswith("QQQ")]
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
    """Build and place the closing order for the given open legs."""
    value, basis, _ = position_value_and_basis(account)
    contracts = max(abs(int(leg["quantity"])) for leg in legs)
    credit = round(value / (contracts * 100.0), 2)
    close_legs = close_legs_for(legs)
    log("closing: value=%.2f basis=%.2f unrealized=%.2f credit=%.2f"
        % (value, basis, value - basis, credit))
    return place_order(account, "credit", credit, close_legs)


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


def run_walkup_cycle(account, short_exp, long_exp, put, call, modeled_debit):
    """Bid a pair from max(WALK_START, modeled debit - WALK_START_OFFSET)
    up to MAX_DEBIT in WALK_STEP steps.

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
    log("walk-up: cycle starting short %s / long %s %dP/%dC from %.2f "
        "(modeled %.2f)"
        % (short_exp, long_exp, put, call,
           max(WALK_START, round(modeled_debit - WALK_START_OFFSET, 2)),
           modeled_debit))
    price = max(WALK_START, round(modeled_debit - WALK_START_OFFSET, 2))
    while price <= MAX_DEBIT + 1e-9:
        price = round(price, 2)
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


def heartbeat_line(now, legs, value, basis, qqq_txt, flat_state):
    """One short status line per poll: flat/open, QQQ, and state."""
    if legs:
        unrealized = value - basis
        return ("heartbeat: open %d legs unrealized %.2f on basis %.2f QQQ %s"
                % (len(legs), unrealized, basis, qqq_txt))
    return "heartbeat: flat QQQ %s %s" % (qqq_txt, flat_state)


def run_once():
    if not API_KEY:
        log("TRADIER_API_KEY is not set, exiting")
        return 1
    account = resolve_account()
    log("account %s connected (paper host %s, trading %s, contracts %d)"
        % (account, HOST, "ENABLED" if ENABLE_TRADING else "dry-run", CONTRACTS))
    last_pnl_day = {}
    next_cycle_time = None
    holding = False
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
            holding = bool(legs)
            # Heartbeat every poll: one short line so the log always shows
            # the worker is alive and what it is doing.
            try:
                qqq_txt = "%.2f" % get_qqq_price()
            except Exception:
                qqq_txt = "?"
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
            log(heartbeat_line(now, legs, value, basis, qqq_txt, flat_state))
            if legs:
                # Keep a standing take-profit in place: a GTC credit order
                # at 110% of basis. This covers restarts and positions
                # entered before this code shipped; it is silent when an
                # order is already working.
                if basis > 0:
                    place_take_profit(account, legs, basis)
                unrealized = value - basis
                # 10% gain exit, any time of day. This is the fallback
                # for when no take-profit order is working: there is no
                # end-of-day forced close, so a position that never
                # reaches 10% stays open (and blocks new entries).
                if basis > 0 and unrealized >= 0.10 * basis:
                    log("10%% exit hit: unrealized %.2f on basis %.2f" % (unrealized, basis))
                    close_position(account, legs)
                    time.sleep(POLL_SECONDS)
                    continue
            else:
                # Walk-up entry, no day skip: while flat and in the
                # window, evaluate every poll; an unfilled cycle cools
                # down for 5 minutes before the next one.
                if (ENTRY_START <= now.time() < ENTRY_END
                        and (next_cycle_time is None or now >= next_cycle_time)):
                    qqq = get_qqq_price()
                    pair, cheapest = find_best_pair(qqq, get_expirations())
                    if pair is None:
                        if cheapest is not None:
                            log("no pair at or under %.2f "
                                "(QQQ %.2f, best pair %.2f short %s / long %s "
                                "%dP/%dC), will retry next poll"
                                % (MAX_DEBIT, qqq, cheapest[4], cheapest[0],
                                   cheapest[1], cheapest[2], cheapest[3]))
                        else:
                            log("no pair at or under %.2f "
                                "(QQQ %.2f, no candidate pair had full chain data), "
                                "will retry next poll"
                                % (MAX_DEBIT, qqq))
                    else:
                        short_exp, long_exp, put, call, debit = pair
                        log("entry: QQQ %.2f short %s / long %s %dP/%dC modeled debit %.2f x %d"
                            % (qqq, short_exp, long_exp, put, call, debit, CONTRACTS))
                        filled = run_walkup_cycle(account, short_exp, long_exp, put, call, debit)
                        if filled:
                            # Place the take-profit immediately: a GTC
                            # credit order at 110% of the debit actually
                            # paid. If the fill has not shown in
                            # positions yet, the per-poll check above
                            # places it on the next poll.
                            _, fill_basis, fill_legs = position_value_and_basis(account)
                            if fill_legs and fill_basis > 0:
                                place_take_profit(account, fill_legs, fill_basis)
                        else:
                            next_cycle_time = datetime.now(TZ) + timedelta(minutes=5)
        except Exception as exc:  # noqa: BLE001 - keep the worker alive
            log("error: %s" % exc)
        # Poll faster while holding a position so the 10% exit fires
        # close to the line; poll slower while flat.
        time.sleep(HOLD_POLL_SECONDS if holding else POLL_SECONDS)
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
