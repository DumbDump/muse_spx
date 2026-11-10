#!/usr/bin/env python3
"""Nasdaq-100 double-calendar paper trader for Railway.

Third worker lane, built on the QQQ worker (qqq_paper_trader.py) and
approved Nov 10, 2026. It trades double calendars on the 20 largest
Nasdaq-100 names by market cap, on the paper account only:

- Tradier PAPER host only (sandbox.tradier.com). The host is hardcoded
  and the program refuses to run against any other host, so it cannot
  touch a live account.
- One double calendar per ticker at a time: sell the nearer expiry put
  and call, buy the later expiry at the same strikes. Up to 20 positions
  can be open at once (one per ticker); each ticker is managed
  independently, with its own tag ("muse-nasdaq-<TICKER>"), so a pending
  order for one ticker never blocks another.
- Strikes 2% each side of the current price (STRIKE_WIDTH_PCT, default
  0.02), snapped to the nearest strike actually listed in the chain —
  a $1 grid does not exist for every name (ASML/COST list $5 strikes),
  so the worker takes the closest listed strike to each target instead
  of assuming a grid.
- Short leg 5-7 DTE at entry (SHORT_DTE_MIN/MAX), long leg the next
  expiry after the short leg. Consequence: names with Friday-only
  weeklies (ASML, COST, PLTR, CSCO, INTC, NFLX, AMAT, PANW, LRCX)
  only qualify on Fridays, when the next Friday is 7 DTE out; AZN has
  monthlies only and almost never qualifies. The Mon/Wed/Fri names
  (NVDA, AAPL, MSFT, AMZN, GOOGL, TSLA, AVGO, META, MU, AMD) qualify
  most days.
- Entry window 8:00 a.m.-12:30 p.m. America/Los_Angeles, weekdays only.
- One bid per cycle, at the spread's bid: while a ticker is flat and
  in the window, the worker evaluates it every poll and places a single
  debit limit order at the spread's bid (long-leg bids minus short-leg
  asks, read off the same chains). There is no ceiling anymore —
  removed Nov 10 at the user's instruction: the bid is the price, full
  stop — and no walk-up: the price only ever moves once per poll cycle.
  A bid that does not fill is cancelled next cycle and re-placed at the
  then-current bid. A bid left working when its pair stops qualifying
  is cancelled as stale. A fill shows up as a position next poll, and
  the per-poll check then places the GTC take-profit at 110% of the
  debit paid. The working bid order is tracked in memory, so after a
  restart the worker re-adopts it from the orders feed (debit orders
  carrying its per-ticker tag) instead of being blocked by it until
  it fills or expires at the close.
- Liquidity gate, two-tier: the SHORT legs (front expiry) must have
  traded at least MIN_LEG_VOLUME contracts today (default 100), and
  the LONG legs (next expiry) at least MIN_LONG_LEG_VOLUME (default
  10). Volume concentrates in the front expiry — AAPL's Nov 18 legs
  traded 15 and 74 while the Nov 16 legs traded 200 and 2,791 — so one
  floor on all four legs would block liquid names outright. The skip
  line reports "low-volume" when a candidate fails the gate.
- 1 contract per trade (CONTRACTS).
- Exit at a 10% gain on the debit basis: as soon as an entry fills, the
  worker places a GTC credit order to close the position at 110% of the
  debit actually paid, so the exit triggers the moment the market touches
  it — even between polls or overnight. The poll-based 10% check stays
  as a fallback for when no take-profit order is working. The standing
  order caps the gain at exactly 10%: it will not capture an overshoot
  above the line the way a poll-time close at the mid could. There is no
  end-of-day forced close: a position that never reaches 10% stays open
  (and blocks new entries for that ticker) until it does.
- Loss exit: close at the mid when the position's value falls to 50% of
  the debit paid (STOP_LOSS_FRACTION, default 0.50). Time exit: close
  when the short leg has 1 day or less to expiry, so a position that
  never reaches 10% cannot ride through short-leg expiry.
- A position that has lost more than its debit is worth less than zero:
  its close is a DEBIT order for the absolute value, not a credit order
  at a negative price — Tradier rejects negative prices, which left the
  Nov 10 SPX position with no working order after its stop-loss fired.
- Exits are checked for every holding ticker before any entry walk-up
  starts in a cycle, so a walk-up can never delay a stop-loss.
- Every order carries its ticker's tag so the worker's own orders are
  identifiable. At most one pending order per ticker: before placing,
  the worker checks open orders and skips if an order with that ticker's
  tag, or with the same leg symbols, is already pending. Walk-up steps
  cancel the worker's own working order before replacing it; it never
  cancels anyone else's order.

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
  running the nasdaq-worker process type, and set TRADIER_API_KEY to a
  Tradier SANDBOX (paper) key (reference the worker service's key).
- Procfile runs: nasdaq-worker: python nasdaq_paper_trader.py
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
- Each ticker is its own root: expirations, chains, and legs all use the
  ticker symbol directly (unlike SPX, where expirations/chains take SPX
  while legs are SPXW).

Usage:

  TRADIER_API_KEY=xxx ENABLE_TRADING=0 python nasdaq_paper_trader.py
  TRADIER_API_KEY=xxx python nasdaq_paper_trader.py --check   # one P&L snapshot, then exit
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
TICKERS = [t.strip().upper() for t in os.environ.get(
    "TICKERS",
    "NVDA,AAPL,MSFT,AMZN,GOOGL,TSLA,AVGO,META,MU,AMD,"
    "ASML,COST,PLTR,CSCO,INTC,AZN,NFLX,AMAT,PANW,LRCX").split(",") if t.strip()]
CONTRACTS = max(1, min(10, int(os.environ.get("CONTRACTS", "1"))))
STRIKE_WIDTH_PCT = float(os.environ.get("STRIKE_WIDTH_PCT", "0.02"))
MAX_DEBIT_FRACTION = float(os.environ.get("MAX_DEBIT_FRACTION", "0.0025"))
# Liquidity gate: every leg of a candidate pair must have traded at
# least this many contracts today (Tradier chain "volume"). Deep OTM
# legs show volume 0 in the sandbox; a 100-contract floor keeps the
# worker out of names nobody is trading while passing liquid names
# (AAPL Nov 16 ATM legs run 100-2,700). Env-tunable: raise it to
# tighten, lower it to let thinner names in.
# The floor is two-tier because volume concentrates in the front
# expiry: the SHORT legs (front expiry) need MIN_LEG_VOLUME, but the
# LONG legs (next expiry) are structurally thinner — AAPL's Nov 18
# 342.5P/355C traded 15 and 74 contracts while the Nov 16 legs traded
# 200 and 2,791 — so they only need MIN_LONG_LEG_VOLUME (default 10):
# they must trade at all, not sit dead. A 100 floor on all four legs
# blocked AAPL entirely on Nov 10, which is what the test is for.
MIN_LEG_VOLUME = int(os.environ.get("MIN_LEG_VOLUME", "100"))
MIN_LONG_LEG_VOLUME = int(os.environ.get("MIN_LONG_LEG_VOLUME", "10"))
WALK_START_FRACTION = float(os.environ.get("WALK_START_FRACTION", "0.70"))
WALK_STEP_FRACTION = float(os.environ.get("WALK_STEP_FRACTION", "0.05"))
WALK_START_FLOOR = float(os.environ.get("WALK_START_FLOOR", "0.05"))
WALK_STEP_FLOOR = float(os.environ.get("WALK_STEP_FLOOR", "0.01"))
# NOTE: MAX_DEBIT_FRACTION, the WALK_* vars and COOLDOWN_SECONDS are
# retired: the ceiling was removed Nov 10 (the bid is the price) and
# the walk-up was replaced by one bid per cycle. The vars stay defined
# so an old Railway env does not break the import, but nothing reads
# them anymore.
HOLD_POLL_SECONDS = int(os.environ.get("HOLD_POLL_SECONDS", "120"))
SHORT_DTE_MIN = int(os.environ.get("SHORT_DTE_MIN", "5"))
SHORT_DTE_MAX = int(os.environ.get("SHORT_DTE_MAX", "7"))
ENTRY_START = dtime(8, 0)
ENTRY_END = dtime(12, 30)
COOLDOWN_SECONDS = 300
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "600"))
STOP_LOSS_FRACTION = float(os.environ.get("STOP_LOSS_FRACTION", "0.50"))
ORDER_TAG_PREFIX = os.environ.get("ORDER_TAG_PREFIX", "muse-nasdaq")

# Expirations are cached per ticker per calendar day: the listed
# expirations only change overnight, and re-fetching them for 20 tickers
# every poll would burn most of the API budget.
_expirations_cache = {}


def log(msg):
    print("[%s] %s" % (datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z"), msg), flush=True)


def tag_for(ticker):
    return "%s-%s" % (ORDER_TAG_PREFIX, ticker)


def root_of(symbol):
    """Underlying root of an option symbol (NVDA261116P00187000 -> NVDA)."""
    match = re.match(r"^([A-Z]+)\d{6}[PC]\d{8}$", symbol)
    return match.group(1) if match else None


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


def get_underlying_price(ticker):
    data = api_get("/v1/markets/quotes?symbols=%s&greeks=false" % ticker)
    quotes_obj = data.get("quotes")
    # Sandbox sometimes returns JSON null here; raise a clean error so the
    # poll retries instead of dying on a NoneType subscript.
    if not isinstance(quotes_obj, dict):
        raise ValueError("%s quote unavailable (null quotes)" % ticker)
    quote = quotes_obj.get("quote")
    if isinstance(quote, list):
        quote = quote[0] if quote else None
    if not isinstance(quote, dict) or quote.get("last") is None:
        raise ValueError("%s quote unavailable (null quote)" % ticker)
    return float(quote["last"])


def option_symbol(ticker, expiry, strike, kind):
    return "%s%s%s%08d" % (ticker, expiry.strftime("%y%m%d"), kind, strike * 1000)


def get_expirations(ticker):
    """Return sorted expiry dates for one ticker, cached per day."""
    today = datetime.now(TZ).date()
    cached = _expirations_cache.get(ticker)
    if cached and cached[0] == today:
        return cached[1]
    try:
        data = api_get("/v1/markets/options/expirations?symbol=%s&includeAllRoots=true"
                       % ticker)
        dates = data["expirations"]["date"]
        if isinstance(dates, str):
            dates = [dates]
        out = sorted(date.fromisoformat(d) for d in dates)
        if out:
            _expirations_cache[ticker] = (today, out)
            return out
    except Exception as exc:  # noqa: BLE001 - fallback is intentional
        log("%s expirations endpoint failed (%s), using weekday calendar" % (ticker, exc))
    out = []
    day = date.today()
    while len(out) < 12:
        day += timedelta(days=1)
        if day.weekday() < 5:
            out.append(day)
    _expirations_cache[ticker] = (today, out)
    return out


def get_chain(ticker, expiry):
    """Return {(strike, kind): (bid, ask, volume)} for one expiry, or {}
    when the endpoint returns no usable chain data (JSON null). Legs
    with no usable bid/ask (or a non-positive mid) are dropped — a
    spread cannot be priced off them. Callers treat {} as 'this expiry
    skipped', never as a crash."""
    data = api_get(
        "/v1/markets/options/chains?symbol=%s&expiration=%s&greeks=false"
        % (ticker, expiry.isoformat())
    )
    options_obj = data.get("options")
    if not isinstance(options_obj, dict):
        return {}
    options = options_obj.get("option")
    if options is None:
        return {}
    if isinstance(options, dict):
        options = [options]
    chain = {}
    for opt in options:
        try:
            bid = float(opt["bid"])
            ask = float(opt["ask"])
            if (bid + ask) / 2.0 <= 0:
                continue
            chain[(float(opt["strike"]), opt["option_type"])] = (
                bid, ask, int(opt.get("volume") or 0))
        except (KeyError, TypeError, ValueError):
            continue
    return chain


def get_chain_mids(ticker, expiry):
    """Return {(strike, kind): mid} for one expiry, derived from
    get_chain. Kept as the test seam; find_best_pair uses get_chain
    directly so it also sees bid/ask (for bid execution) and volume."""
    return {k: (b + a) / 2.0 for k, (b, a, _v) in get_chain(ticker, expiry).items()}


def _nearest_strike(target, available):
    """Closest listed strike to the target, or None when none listed."""
    if not available:
        return None
    return min(available, key=lambda s: abs(s - target))


def find_best_pair(ticker, price, expirations):
    """Return (pair, reason): the cheapest volume-passing pair for this
    ticker, or (None, reason) when it sits out.

    A pair is (short_exp, long_exp, put_strike, call_strike, debit,
    bid): debit is the mid-based modeled cost, bid is the spread's bid
    — what the position could be sold for right now (long-leg bids
    minus short-leg asks). The entry places a single order at the bid;
    there is no ceiling anymore (removed Nov 10 at the user's
    instruction: the bid is the price, full stop). The cheapest pair is
    the one to bid, whatever its modeled debit.

    Targets are price +/- STRIKE_WIDTH_PCT, snapped to the nearest
    strike actually listed in both chains. The long leg is the next
    expiry after the short leg.

    Liquidity gate, two-tier: the SHORT legs (front expiry) must have
    traded at least MIN_LEG_VOLUME contracts today (default 100), and
    the LONG legs (next expiry) at least MIN_LONG_LEG_VOLUME (default
    10). Volume concentrates in the front expiry — AAPL's Nov 18 legs
    traded 15 and 74 while the Nov 16 legs traded 200 and 2,791 — so
    one floor on all four legs would block liquid names outright. The
    skip line reports "low-volume" when a candidate fails the gate.

    Never raises: a ticker with no expiry in the 5-7 DTE window (the
    normal state for Friday-only names on Tue-Thu, and for AZN almost
    always) or with no chain data must not abort the whole cycle — the
    QQQ worker raises here because it has one ticker, but this worker
    has twenty, and one bad ticker must not block the other nineteen.
    The reason string tells the skip line why the ticker sat out."""
    put_target = price * (1.0 - STRIKE_WIDTH_PCT)
    call_target = price * (1.0 + STRIKE_WIDTH_PCT)
    today = datetime.now(TZ).date()
    best = None
    saw_window = False
    saw_chains = False
    saw_low_volume = False
    for i, short_exp in enumerate(expirations):
        dte = (short_exp - today).days
        if dte < SHORT_DTE_MIN or dte > SHORT_DTE_MAX:
            continue
        saw_window = True
        if i + 1 >= len(expirations):
            continue
        long_exp = expirations[i + 1]
        short_chain = get_chain(ticker, short_exp)
        long_chain = get_chain(ticker, long_exp)
        if short_chain or long_chain:
            saw_chains = True
        short_mids = {k: (b + a) / 2.0 for k, (b, a, _v) in short_chain.items()}
        long_mids = {k: (b + a) / 2.0 for k, (b, a, _v) in long_chain.items()}
        # Strikes listed in both chains, per kind; snap each target to
        # the closest listed strike (grids differ by name and price).
        put_strikes = sorted(
            {s for (s, k) in short_mids if k == "put"}
            & {s for (s, k) in long_mids if k == "put"})
        call_strikes = sorted(
            {s for (s, k) in short_mids if k == "call"}
            & {s for (s, k) in long_mids if k == "call"})
        put_strike = _nearest_strike(put_target, put_strikes)
        call_strike = _nearest_strike(call_target, call_strikes)
        if put_strike is None or call_strike is None:
            continue
        try:
            debit = (
                long_mids[(put_strike, "put")]
                + long_mids[(call_strike, "call")]
                - short_mids[(put_strike, "put")]
                - short_mids[(call_strike, "call")]
            )
            # Spread bid: what we could sell this calendar for now
            # (long-leg bids minus short-leg asks). Always at or below
            # the mid-based debit.
            bid = (
                long_chain[(put_strike, "put")][0]
                + long_chain[(call_strike, "call")][0]
                - short_chain[(put_strike, "put")][1]
                - short_chain[(call_strike, "call")][1]
            )
            short_vols = (
                short_chain[(put_strike, "put")][2],
                short_chain[(call_strike, "call")][2],
            )
            long_vols = (
                long_chain[(put_strike, "put")][2],
                long_chain[(call_strike, "call")][2],
            )
        except KeyError:
            continue
        if (min(short_vols) < MIN_LEG_VOLUME
                or min(long_vols) < MIN_LONG_LEG_VOLUME):
            saw_low_volume = True
            continue
        candidate = (short_exp, long_exp, put_strike, call_strike,
                     round(debit, 2), round(bid, 2))
        if best is None or candidate[4] < best[4]:
            best = candidate
    if not saw_window:
        return None, "no-5-7-DTE"
    if not saw_chains:
        # An expiry qualified but the endpoint returned no chain data: a
        # transient outage for this ticker, not a reason to stop the
        # other nineteen.
        log("%s: no option chains data from Tradier, skipping this poll" % ticker)
        return None, "no-chain-data"
    if best is None and saw_low_volume:
        return None, "low-volume"
    return best, None


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


def has_blocking_pending_order(account, legs, tag, exclude_id=None):
    """True when this ticker already has a pending order that would collide
    with the given legs: its own tag, or the same leg symbols. Matching on
    leg symbols means the check works even for orders placed before
    tagging, and even if the provider does not echo the tag back. The tag
    is per ticker, so a pending NVDA order never blocks an AMD entry.
    exclude_id skips one order the caller just cancelled, so a stale feed
    listing it does not block the replacement close."""
    new_symbols = {symbol for _, symbol, _ in legs}
    for order in get_pending_orders(account):
        if exclude_id is not None and str(order["id"]) == str(exclude_id):
            continue
        if order["tag"] == tag or order["symbols"] == new_symbols:
            return True, order["id"]
    return False, None


def adopt_working_bid_orders(account, bid_orders):
    """Adopt working bid orders placed before a restart. bid_orders is
    in-memory and resets on redeploy, but a day bid order placed
    before the restart is still working in Tradier; without this the
    worker would be blocked from bidding (one-pending rule) until that
    order fills or expires at the close. Only debit orders whose legs
    all belong to the ticker are adopted — the take-profit GTC is a
    credit order, so it is never mistaken for a bid. (Matching is by
    leg symbols, not by tag: Tradier's orders feed does not echo the
    tag back — verified Nov 10, order 41588979 carries no tag field —
    which is why the first version of this function, matching on tag,
    adopted nothing.) A holding ticker's orders are never adopted for
    entry anyway (the entry loop skips holding tickers). Returns the
    tickers adopted."""
    adopted = []
    try:
        pending = get_pending_orders(account)
    except Exception as exc:  # noqa: BLE001 - retry next cycle
        log("adopt: pending orders read failed (%s)" % exc)
        return adopted
    for order in pending:
        if order.get("type") not in ("debit", None):
            continue
        symbols = order.get("symbols") or set()
        if not symbols:
            continue
        roots = {root_of(s) for s in symbols}
        if len(roots) != 1:
            continue
        ticker = roots.pop()
        if ticker not in TICKERS or ticker in bid_orders:
            continue
        bid_orders[ticker] = order["id"]
        adopted.append(ticker)
        log("adopted working bid order %s for %s" % (order["id"], ticker))
    return adopted


def place_order(account, ticker, order_type, price, legs, duration="day",
                exclude_order_id=None):
    """legs: list of (side, symbol, qty). Returns the API response dict.
    Never creates a second pending order for this ticker: if a colliding
    order is already pending, the placement is skipped and logged."""
    tag = tag_for(ticker)
    blocked, blocking_id = has_blocking_pending_order(
        account, legs, tag, exclude_id=exclude_order_id)
    if blocked:
        log("SKIPPED %s %s order at %.2f: pending order %s already exists"
            % (ticker, order_type, price, blocking_id))
        return {"blocked": True, "blocking_order_id": blocking_id}
    fields = {
        "class": "multileg",
        "symbol": ticker,
        "type": order_type,
        "duration": duration,
        "price": "%.2f" % price,
        "tag": tag,
    }
    for i, (side, symbol, qty) in enumerate(legs):
        fields["option_symbol[%d]" % i] = symbol
        fields["side[%d]" % i] = side
        fields["quantity[%d]" % i] = str(qty)
    if not ENABLE_TRADING:
        log("DRY RUN: would place %s %s order at %.2f: %s"
            % (ticker, order_type, price, legs))
        return {"dry_run": True}
    resp = api_post_order(account, fields)
    order = resp.get("order", resp)
    log("PLACED %s %s order id=%s price=%.2f legs=%s"
        % (ticker, order_type, order.get("id"), price, legs))
    return resp


def positions_for_ticker(all_positions, ticker):
    """Open legs belonging to one ticker, by option-symbol root."""
    return [p for p in all_positions if root_of(p["symbol"]) == ticker]


def position_value_and_basis(account, ticker, all_positions=None):
    """Return (net_value, net_basis, legs) for one ticker's open legs."""
    if all_positions is None:
        all_positions = get_positions(account)
    legs = positions_for_ticker(all_positions, ticker)
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
    (SPXW261112... and NVDA261116... alike). The short leg is the
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


def place_take_profit(account, ticker, legs, basis):
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
    blocked, _ = has_blocking_pending_order(account, close_legs, tag_for(ticker))
    if blocked:
        return True
    resp = place_order(account, ticker, "credit", target, close_legs, duration="gtc")
    if resp.get("blocked"):
        return True
    if resp.get("dry_run"):
        return False
    order = resp.get("order", resp)
    log("take-profit: %s placed GTC credit order id=%s at %.2f (10%% on basis %.2f)"
        % (ticker, order.get("id"), target, basis))
    return True


def close_position(account, ticker, legs):
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
    value, basis, fresh_legs = position_value_and_basis(account, ticker)
    if not fresh_legs:
        log("closing: %s position already closed, nothing to do" % ticker)
        return {"already_closed": True}
    legs = fresh_legs
    contracts = max(abs(int(leg["quantity"])) for leg in legs)
    credit = round(value / (contracts * 100.0), 2)
    close_legs = close_legs_for(legs)
    blocked, blocking_id = has_blocking_pending_order(
        account, close_legs, tag_for(ticker))
    if blocked:
        try:
            cancel_order(account, blocking_id)
            log("closing: %s cancelled working order %s to place the close"
                % (ticker, blocking_id))
        except Exception as exc:  # noqa: BLE001 - retry next poll
            log("closing: %s cancel of order %s failed (%s), close skipped, will retry"
                % (ticker, blocking_id, exc))
            return {"blocked": True, "blocking_order_id": blocking_id}
    if credit < 0:
        order_type, price = "debit", -credit
    else:
        order_type, price = "credit", max(credit, 0.01)
    log("closing: %s value=%.2f basis=%.2f unrealized=%.2f %s=%.2f"
        % (ticker, value, basis, value - basis, order_type, price))
    # exclude_order_id: the cancel above just removed this order, but a
    # stale feed may still list it — do not let it block its own close.
    return place_order(account, ticker, order_type, price, close_legs,
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


def place_bid_order(account, ticker, short_exp, long_exp, put, call, bid):
    """Place ONE debit limit order at the spread's bid and return its
    order id (or None when blocked / dry-run). No walk-up: the bid is
    the price. If it does not fill, the next poll cycle cancels it and
    re-bids at the then-current bid — the price only ever moves once
    per cycle, never inside one. Only the worker's own tagged order is
    ever placed, and place_order enforces at most one pending order
    per ticker."""
    legs = [
        ("sell_to_open", option_symbol(ticker, short_exp, put, "P"), CONTRACTS),
        ("sell_to_open", option_symbol(ticker, short_exp, call, "C"), CONTRACTS),
        ("buy_to_open", option_symbol(ticker, long_exp, put, "P"), CONTRACTS),
        ("buy_to_open", option_symbol(ticker, long_exp, call, "C"), CONTRACTS),
    ]
    log("bid: %s %.2f short %s / long %s %dP/%dC"
        % (ticker, bid, short_exp, long_exp, put, call))
    resp = place_order(account, ticker, "debit", bid, legs)
    if resp.get("blocked"):
        log("bid: %s blocked by an existing pending order, not placed" % ticker)
        return None
    if resp.get("dry_run"):
        return None
    order = resp.get("order", resp)
    order_id = order.get("id")
    if not order_id:
        log("bid: %s order response without id, not placed: %s" % (ticker, resp))
        return None
    log("bid: %s order %s working at %.2f" % (ticker, order_id, bid))
    return order_id


def cancel_bid_order(account, ticker, order_id):
    """Cancel a working bid order from a previous cycle. Returns True
    when the order is gone (cancelled, or already filled — a fill means
    the position exists and the entry loop will pick it up next poll),
    False when it is still working and the cancel failed, so the caller
    leaves it alone and retries next cycle instead of placing a second
    order."""
    try:
        status = get_order_status(account, order_id)
    except Exception as exc:  # noqa: BLE001 - cancel anyway below
        log("bid: %s status check failed (%s), cancelling order %s"
            % (ticker, exc, order_id))
        status = None
    if status == "filled":
        return True
    if status not in ("open", "pending", "partially_filled", None):
        # Rejected, expired, or otherwise dead: nothing to cancel.
        return True
    try:
        cancel_order(account, order_id)
    except Exception as exc:  # noqa: BLE001 - still working, retry later
        log("bid: %s cancel of order %s failed (%s), leaving it working"
            % (ticker, order_id, exc))
        return False
    log("bid: %s cancelled order %s, will re-bid at the next cycle's bid"
        % (ticker, order_id))
    return True


def pnl_snapshot(account):
    bal = get_balances(account)
    all_positions = get_positions(account)
    total_unrealized = 0.0
    open_legs = 0
    for ticker in TICKERS:
        value, basis, legs = position_value_and_basis(account, ticker, all_positions)
        if legs:
            total_unrealized += value - basis
            open_legs += len(legs)
    log("P&L equity=%.2f realized_today=%.2f unrealized=%.2f open_legs=%d"
        % (float(bal["equity"]), float(bal["close_pl"]), total_unrealized,
           open_legs))


def run_once():
    if not API_KEY:
        log("TRADIER_API_KEY is not set, exiting")
        return 1
    account = resolve_account()
    log("account %s connected (paper host %s, trading %s, contracts %d, tickers %d)"
        % (account, HOST, "ENABLED" if ENABLE_TRADING else "dry-run",
           CONTRACTS, len(TICKERS)))
    last_pnl_day = {}
    # Working bid order per ticker: one single-bid attempt per poll
    # cycle. A bid that does not fill is cancelled next cycle and
    # re-placed at the then-current bid — the price only ever moves
    # once per cycle, never inside one.
    bid_orders = {}
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
                if now.hour == hour and last_pnl_day.get(hour) != today:
                    pnl_snapshot(account)
                    last_pnl_day[hour] = today
            # Prices once per cycle, reused by entries and the heartbeat.
            prices = {}
            for ticker in TICKERS:
                try:
                    prices[ticker] = get_underlying_price(ticker)
                except Exception as exc:  # noqa: BLE001 - skip this ticker
                    log("%s price unavailable (%s), skipping this poll"
                        % (ticker, exc))
            # 1) Exits first, for every holding ticker: a bid must
            # never delay a stop-loss.
            all_positions = get_positions(account)
            holding_tickers = []
            for ticker in TICKERS:
                value, basis, legs = position_value_and_basis(
                    account, ticker, all_positions)
                if not legs:
                    continue
                holding_tickers.append(ticker)
                # A position means any earlier bid order for this ticker
                # has filled; drop its tracking.
                bid_orders.pop(ticker, None)
                unrealized = value - basis
                # Loss exit: close at the mid when the position has lost
                # half the debit (value at or under STOP_LOSS_FRACTION of
                # basis). Poll-based — the GTC slot is held by the
                # take-profit — so a gap between polls can overshoot it.
                if basis > 0 and value <= STOP_LOSS_FRACTION * basis:
                    log("stop-loss hit: %s value %.2f on basis %.2f"
                        % (ticker, value, basis))
                    close_position(account, ticker, legs)
                    time.sleep(POLL_SECONDS)
                    continue
                # Time exit: close when the short leg has 1 day or less
                # to expiry, so the position never rides through
                # short-leg expiry and morphs into a naked long straddle.
                short_exp = short_expiry_from_legs(legs)
                if short_exp is not None and (short_exp - today).days <= 1:
                    log("short-leg expiry close: %s short leg expires %s"
                        % (ticker, short_exp))
                    close_position(account, ticker, legs)
                    time.sleep(POLL_SECONDS)
                    continue
                # 10% gain exit, any time of day. This is the fallback
                # for when no take-profit order is working: there is no
                # end-of-day forced close, so a position that never
                # reaches 10% stays open (and blocks new entries for
                # that ticker).
                if basis > 0 and unrealized >= 0.10 * basis:
                    log("10%% exit hit: %s unrealized %.2f on basis %.2f"
                        % (ticker, unrealized, basis))
                    close_position(account, ticker, legs)
                    time.sleep(POLL_SECONDS)
                    continue
                # Keep a standing take-profit in place: a GTC credit order
                # at 110% of basis. This covers restarts and positions
                # entered before this code shipped; it is silent when an
                # order is already working. Checked after the closes above
                # so this poll never places a GTC it is about to cancel.
                if basis > 0:
                    place_take_profit(account, ticker, legs, basis)
            holding = bool(holding_tickers)
            # Heartbeat every poll: one short line so the log always shows
            # the worker is alive and what it is doing.
            if holding_tickers:
                log("heartbeat: holding %s (%d position%s) | flat %d"
                    % (", ".join(holding_tickers), len(holding_tickers),
                       "s" if len(holding_tickers) != 1 else "",
                       len(TICKERS) - len(holding_tickers)))
            elif ENTRY_START <= now.time() < ENTRY_END:
                log("heartbeat: flat %d tickers, in window, will evaluate"
                    % len(TICKERS))
            else:
                log("heartbeat: flat %d tickers, waiting for window"
                    % len(TICKERS))
            # 2) Entries for flat tickers in the window: one bid per
            # ticker per cycle, at the spread's bid — no ceiling, no
            # walk-up. An unfilled bid is cancelled next cycle and
            # re-placed at the new bid; a bid left working when its
            # pair stops qualifying is cancelled as stale.
            if ENTRY_START <= now.time() < ENTRY_END:
                # Adopt working bid orders after a restart, so the
                # cancel-and-rebid flow keeps working (see above).
                adopt_working_bid_orders(account, bid_orders)
                skips = []
                for ticker in TICKERS:
                    if ticker in holding_tickers:
                        continue
                    price = prices.get(ticker)
                    if price is None:
                        continue
                    pair, reason = find_best_pair(
                        ticker, price, get_expirations(ticker))
                    prev_id = bid_orders.get(ticker)
                    if pair is None:
                        if prev_id:
                            if cancel_bid_order(account, ticker, prev_id):
                                bid_orders.pop(ticker, None)
                        skips.append("%s %s" % (ticker, reason))
                        continue
                    short_exp, long_exp, put, call, debit, bid = pair
                    if prev_id:
                        if not cancel_bid_order(account, ticker, prev_id):
                            # Still working and the cancel failed: leave
                            # it alone, retry next cycle.
                            continue
                        bid_orders.pop(ticker, None)
                    log("entry: %s %.2f short %s / long %s %dP/%dC modeled debit %.2f "
                        "bid %.2f x %d"
                        % (ticker, price, short_exp, long_exp, put, call,
                           debit, bid, CONTRACTS))
                    order_id = place_bid_order(
                        account, ticker, short_exp, long_exp, put, call, bid)
                    if order_id:
                        # A fill shows up as a position next poll; the
                        # per-poll check above then places the GTC
                        # take-profit at 110% of the debit paid.
                        bid_orders[ticker] = order_id
                if skips:
                    log("no entry: %s" % ", ".join(skips))
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
