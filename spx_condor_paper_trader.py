#!/usr/bin/env python3
"""SPX iron condor paper trader for Railway.

Replaces the paper SPX trend worker (spx_trend_paper_trader.py, disabled
2026-11-23). Implements one fixed rule set, nothing else:

- Tradier PAPER host only (sandbox.tradier.com). The host is hardcoded
  and the program refuses to run against any other host, so it cannot
  touch a live account.
- SPX iron condors, entered only when VIX is under 20 and not rising:
  VIX < VIX_MAX (20) AND (VIX below its 10-day average of daily closes
  OR down 3 sessions running), AND no VIX spike of more than 15% over
  the last 5 sessions. VIX history comes from Tradier's daily history
  endpoint; when that read fails the filter falls back to the VIX quote
  alone and logs that the trend check is degraded.
- No entry on a day SPX gapped more than 2% from the prior close, and
  no entry on the day before or the day of a blocked event date
  (FOMC decision days and CPI release days, DEFAULT_BLOCKED_DATES
  below, verified against the Fed/BLS calendars Nov 2026; override
  with the BLOCKED_DATES env var).
- Structure: 30-45 DTE (prefer closest to 35), short strikes at ~16
  delta each side (nearest listed strike with |delta| in [0.10, 0.20],
  closest to 0.16), long strikes the nearest listed strike at least
  $20 further out of the money, wing width at most $25 (sparse
  far-dated chains list $100-wide wings, which are rejected). Total
  mid credit must be at least $2.00 and at most $10.00. At most 1
  open at once, 1 contract each.
- Exits: a standing GTC take-profit is placed when a condor opens
  (buy back at 50% of the total credit received, entry plus any roll
  credits), and the poll-based exits remain as a fallback every poll:
  take profit when the buy-back cost falls to 50% of total credit;
  stop when the buy-back cost reaches 3x total credit (a loss of 2x
  the credit received). The fallbacks re-verify their target on a
  fresh read and the profit fallback never fires while the
  take-profit GTC is working (the Nov 19, 2026 phantom-print guard,
  as in the trend/QQQ workers). Any close cancels the working GTC
  first, and a new GTC is placed when the next condor opens.
- 21-DTE manage: a condor is closed at the mid when its expiry is 21
  days or fewer away, whatever its P&L. No condor is ever carried
  into expiry week unadjusted.
- Adjustment ladder: a side is TESTED when SPX comes within 1% of its
  short strike or that short leg's delta reaches 0.25. The tested
  side is rolled out to the next expiry 7-14 days later and at least
  $25 further out of the money, FOR A CREDIT — if the roll cannot be
  done for a credit, the whole condor is closed instead. After a
  tested-side roll, the untested side is rolled toward SPX (same
  expiry, back to ~16 delta) when its short delta has fallen below
  0.10, also for a credit. At most 2 adjustments per condor; after
  the second, a still-losing condor is closed at the stop.
- Leg-collision avoidance: before entering, the worker reads every
  open SPXW position in the paper account. If any of the four strikes
  is already held by another strategy, that expiry is skipped and the
  next expiry in the window is tried.
- Condors are identified from the orders feed, not from memory: the
  worker reconstructs its open condors from filled entry and roll
  orders, so a restart re-adopts them with their total credit and
  adjustment count intact. Only orders that match this worker's own
  shape are adopted: a 4-leg credit entry must be a real iron condor
  (2 puts + 2 calls, one expiry, $20-25 wings, equal quantities,
  fill credit $2.00-$10.00/share, and — when the feed carries a tag
  — this worker's tag); a roll's new legs must form a valid vertical
  of the same shape. Anything else is left untouched and logged.
  A grouped 4-leg condor in positions with no matching orders is
  adopted on its position cost basis as a fallback only when its
  credit is inside the worker's range (with $2/share of headroom for
  roll credits), and logged loudly, so no open condor is ever left
  unmanaged after a restart. Every poll the worker also checks the
  account's open legs and logs any SPXW leg that is not part of an
  adopted condor: those legs are never touched or managed.

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
- Procfile runs: spx-condor-worker: python spx_condor_paper_trader.py
- The worker loops forever, so Railway keeps it running. Logs are the
  P&L record: the program prints a heartbeat line every poll (so
  silence never means "is it running?"), a P&L line on the first poll
  of the 6:00 a.m. and 8:00 a.m. PT hours on weekdays, plus a line for
  every entry, exit, roll, skip, and error.

Notes:

- Tradier sandbox quotes are delayed about 15 minutes. Mids computed
  here are estimates, not live brokerage marks.
- Entry and roll orders are day orders: an order left unfilled at the
  close expires with the session. Take-profit GTCs are good-till-
  cancelled and survive the session.

Usage:

  TRADIER_API_KEY=xxx ENABLE_TRADING=0 python spx_condor_paper_trader.py
  TRADIER_API_KEY=xxx python spx_condor_paper_trader.py --check   # one P&L snapshot, then exit
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
ORDER_TAG = os.environ.get("ORDER_TAG", "muse-spx-condor")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))
ENTRY_START = dtime(7, 0)
ENTRY_END = dtime(12, 0)
MIN_DTE = int(os.environ.get("MIN_DTE", "30"))
MAX_DTE = int(os.environ.get("MAX_DTE", "45"))
TARGET_DTE = int(os.environ.get("TARGET_DTE", "35"))
TARGET_DELTA = float(os.environ.get("TARGET_DELTA", "0.16"))
DELTA_MIN = float(os.environ.get("DELTA_MIN", "0.10"))
DELTA_MAX = float(os.environ.get("DELTA_MAX", "0.20"))
WING_WIDTH = int(os.environ.get("WING_WIDTH", "20"))
MAX_WING_WIDTH = int(os.environ.get("MAX_WING_WIDTH", "25"))
MIN_CREDIT = float(os.environ.get("MIN_CREDIT", "2.00"))
MAX_CREDIT = float(os.environ.get("MAX_CREDIT", "10.00"))
MAX_OPEN = int(os.environ.get("MAX_OPEN", "1"))
PROFIT_TARGET_FRACTION = float(os.environ.get("PROFIT_TARGET_FRACTION", "0.50"))
STOP_MULTIPLE = float(os.environ.get("STOP_MULTIPLE", "2.0"))
MANAGE_DTE = int(os.environ.get("MANAGE_DTE", "21"))
TEST_DISTANCE_PCT = float(os.environ.get("TEST_DISTANCE_PCT", "0.01"))
TEST_DELTA = float(os.environ.get("TEST_DELTA", "0.25"))
ROLL_MIN_DAYS = int(os.environ.get("ROLL_MIN_DAYS", "7"))
ROLL_MAX_DAYS = int(os.environ.get("ROLL_MAX_DAYS", "14"))
ROLL_FURTHER = int(os.environ.get("ROLL_FURTHER", "25"))
MAX_ADJUSTMENTS = int(os.environ.get("MAX_ADJUSTMENTS", "2"))
VIX_MAX = float(os.environ.get("VIX_MAX", "20.0"))
GAP_SKIP_PCT = float(os.environ.get("GAP_SKIP_PCT", "0.02"))

# FOMC decision days and CPI release days, verified Nov 2026 against
# the Federal Reserve meeting calendar and the BLS release schedule.
# The 2027-11-17 FOMC date appears in secondary calendars only (the
# Fed's own 2027 calendar lists 8 meetings with no November one) and
# is included anyway: blocking one extra day costs nothing. Override
# with BLOCKED_DATES as comma-separated YYYY-MM-DD.
DEFAULT_BLOCKED_DATES = (
    "2026-12-09,2026-12-10,"
    "2027-01-13,2027-01-27,2027-02-11,2027-03-10,2027-03-17,"
    "2027-04-13,2027-04-28,2027-05-12,2027-06-09,2027-06-10,"
    "2027-07-14,2027-07-28,2027-08-11,2027-09-14,2027-09-15,"
    "2027-10-14,2027-10-27,2027-11-10,2027-11-17,2027-12-08,2027-12-10"
)
BLOCKED_DATES = {
    date.fromisoformat(d.strip())
    for d in os.environ.get("BLOCKED_DATES", DEFAULT_BLOCKED_DATES).split(",")
    if d.strip()
}


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


def get_all_orders(account):
    """All recent orders (filled, cancelled, pending, rejected). Same
    empty-response quirk as positions."""
    data = api_get("/v1/accounts/%s/orders" % account)
    orders_obj = data.get("orders")
    if not isinstance(orders_obj, dict):
        return []
    orders = orders_obj.get("order")
    if orders is None:
        return []
    if isinstance(orders, dict):
        return [orders]
    return orders


def get_spx_quote():
    data = api_get("/v1/markets/quotes?symbols=SPX&greeks=false")
    quotes_obj = data.get("quotes")
    if not isinstance(quotes_obj, dict):
        raise ValueError("SPX quote unavailable (null quotes)")
    quote = quotes_obj.get("quote")
    if isinstance(quote, list):
        quote = quote[0] if quote else None
    if not isinstance(quote, dict) or quote.get("last") is None:
        raise ValueError("SPX quote unavailable (null quote)")
    return quote


def get_spx_price():
    return float(get_spx_quote()["last"])


def option_symbol(expiry, strike, kind):
    return "SPXW%s%s%08d" % (expiry.strftime("%y%m%d"), kind, strike * 1000)


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
    while len(out) < 60:
        day += timedelta(days=1)
        if day.weekday() < 5:
            out.append(day)
    return out


def get_chain(expiry):
    """Return {(strike, kind): option dict} for one expiry, or {} when
    the endpoint returns no usable chain data. Greeks are requested:
    the 16-delta short selection and the test-delta roll trigger read
    delta from them. Chains take the underlying SPX; SPXW returns
    JSON null. The returned option symbols are still SPXW, which is
    what legs use."""
    data = api_get(
        "/v1/markets/options/chains?symbol=SPX&expiration=%s&greeks=true"
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


def chain_delta(opt):
    try:
        return float(opt["greeks"]["delta"])
    except (KeyError, TypeError, ValueError):
        return None


def get_vix_history_closes():
    """Daily VIX closes, oldest first, from Tradier's history endpoint.
    Returns [] when the endpoint returns nothing usable."""
    try:
        data = api_get("/v1/markets/history?symbol=VIX&interval=daily")
        days = data.get("history", {}).get("day")
        if days is None:
            return []
        if isinstance(days, dict):
            days = [days]
        closes = []
        for d in days:
            try:
                closes.append(float(d["close"]))
            except (KeyError, TypeError, ValueError):
                continue
        return closes
    except Exception as exc:  # noqa: BLE001 - caller falls back
        log("VIX history failed (%s)" % exc)
        return []


def vix_gate():
    """(True, reason) when the VIX entry filter passes, else
    (False, reason). VIX < VIX_MAX AND (below its 10-day average OR
    down 3 sessions running) AND no >15% spike over the last 5
    sessions. When history is unavailable, the quote alone decides
    and the degradation is logged."""
    try:
        quote = api_get("/v1/markets/quotes?symbols=VIX&greeks=false")
        q = quote["quotes"]["quote"]
        if isinstance(q, list):
            q = q[0]
        vix = float(q["last"])
    except Exception as exc:  # noqa: BLE001 - no VIX, no entry
        return False, "VIX quote unavailable (%s)" % exc
    if vix >= VIX_MAX:
        return False, "VIX %.2f at/above %.2f" % (vix, VIX_MAX)
    closes = get_vix_history_closes()
    if len(closes) < 10:
        log("VIX history short (%d closes), quote-only gate" % len(closes))
        return True, "VIX %.2f under %.2f (history degraded)" % (vix, VIX_MAX)
    avg10 = sum(closes[-10:]) / 10.0
    down3 = len(closes) >= 3 and closes[-3] > closes[-2] > closes[-1]
    if not (vix < avg10 or down3):
        return False, "VIX %.2f above 10-day avg %.2f and not down 3 sessions" % (vix, avg10)
    if len(closes) >= 6:
        five_ago = closes[-6]
        if five_ago > 0 and (closes[-1] / five_ago - 1.0) > 0.15:
            return False, "VIX spiked %.0f%% over last 5 sessions" % (
                (closes[-1] / five_ago - 1.0) * 100.0)
    return True, "VIX %.2f under %.2f, 10-day avg %.2f" % (vix, VIX_MAX, avg10)


def entry_blocked_by_event(today):
    """True when today is a blocked event date or the day before one."""
    for blocked in BLOCKED_DATES:
        if today == blocked or today == blocked - timedelta(days=1):
            return True, blocked
    return False, None


def held_spxw_strikes(account):
    """All SPXW strikes currently held in the paper account (any lane),
    for leg-collision avoidance."""
    strikes = set()
    for p in get_positions(account):
        if str(p.get("symbol", "")).startswith("SPXW"):
            _kind, strike = _leg_kind_strike(p.get("symbol", ""))
            if strike is not None:
                strikes.add(float(strike))
    return strikes


def _two_sided(opt):
    try:
        return float(opt["bid"]) > 0 and float(opt["ask"]) > 0
    except (KeyError, TypeError, ValueError):
        return False


def _short_for_delta(chain, kind, spx):
    """The short strike for one side: the listed strike whose |delta|
    is closest to TARGET_DELTA within [DELTA_MIN, DELTA_MAX], out of
    the money. Returns (strike, opt) or (None, None)."""
    pool = [(s, o) for (s, k), o in chain.items() if k == kind]
    best = None
    for strike, opt in pool:
        if kind == "put" and strike >= spx:
            continue
        if kind == "call" and strike <= spx:
            continue
        d = chain_delta(opt)
        if d is None or not (DELTA_MIN <= abs(d) <= DELTA_MAX):
            continue
        if best is None or abs(abs(d) - TARGET_DELTA) < abs(abs(chain_delta(best[1])) - TARGET_DELTA):
            best = (strike, opt)
    return best if best is not None else (None, None)


def _long_for_short(chain, kind, short_strike):
    """The long strike for a short: the nearest listed strike at
    least WING_WIDTH further out of the money. Returns (strike, opt,
    width) or (None, None, None) when none is listed or the nearest
    is wider than MAX_WING_WIDTH (sparse far-dated chains list
    $100-wide wings, which are rejected)."""
    pool = sorted(s for (s, k) in chain if k == kind)
    if kind == "put":
        cands = [s for s in pool if s <= short_strike - WING_WIDTH]
        if not cands:
            return None, None, None
        long_strike = max(cands)
    else:
        cands = [s for s in pool if s >= short_strike + WING_WIDTH]
        if not cands:
            return None, None, None
        long_strike = min(cands)
    width = abs(short_strike - long_strike)
    if width > MAX_WING_WIDTH:
        return None, None, None
    return long_strike, chain[(long_strike, kind)], width


def select_condor(spx, expiry, chain, held_strikes):
    """Pick the iron condor for one expiry. Returns
    (condor dict, None) or (None, reason). The condor dict carries
    the four strikes, the mid credit per share, the wing widths, and
    the four legs."""
    put_short, put_short_opt = _short_for_delta(chain, "put", spx)
    call_short, call_short_opt = _short_for_delta(chain, "call", spx)
    if put_short is None:
        return None, "no put short in delta [%.2f, %.2f]" % (DELTA_MIN, DELTA_MAX)
    if call_short is None:
        return None, "no call short in delta [%.2f, %.2f]" % (DELTA_MIN, DELTA_MAX)
    put_long, put_long_opt, put_width = _long_for_short(chain, "put", put_short)
    call_long, call_long_opt, call_width = _long_for_short(chain, "call", call_short)
    if put_long is None:
        return None, "no put long within $%d of %d" % (MAX_WING_WIDTH, put_short)
    if call_long is None:
        return None, "no call long within $%d of %d" % (MAX_WING_WIDTH, call_short)
    for opt, label in ((put_short_opt, "put short"), (put_long_opt, "put long"),
                       (call_short_opt, "call short"), (call_long_opt, "call long")):
        if not _two_sided(opt):
            return None, "%s has no two-sided quote" % label
    for strike in (put_short, put_long, call_short, call_long):
        if float(strike) in held_strikes:
            return None, "strike %d already held, skipping expiry" % strike
    put_credit = chain_mid(put_short_opt) - chain_mid(put_long_opt)
    call_credit = chain_mid(call_short_opt) - chain_mid(call_long_opt)
    credit = round(put_credit + call_credit, 2)
    if credit < MIN_CREDIT:
        return None, "credit %.2f below $%.2f floor" % (credit, MIN_CREDIT)
    if credit > MAX_CREDIT:
        return None, "credit %.2f above $%.2f cap" % (credit, MAX_CREDIT)
    legs = [
        ("sell_to_open", option_symbol(expiry, int(put_short), "P"), CONTRACTS),
        ("buy_to_open", option_symbol(expiry, int(put_long), "P"), CONTRACTS),
        ("sell_to_open", option_symbol(expiry, int(call_short), "C"), CONTRACTS),
        ("buy_to_open", option_symbol(expiry, int(call_long), "C"), CONTRACTS),
    ]
    return {
        "expiry": expiry,
        "put_short": int(put_short), "put_long": int(put_long),
        "call_short": int(call_short), "call_long": int(call_long),
        "put_width": int(put_width), "call_width": int(call_width),
        "credit_per_share": credit,
        "legs": legs,
    }, None


def candidate_expiries(today):
    """Expiries in the [MIN_DTE, MAX_DTE] window, closest to
    TARGET_DTE first."""
    out = []
    for exp in get_expirations():
        dte = (exp - today).days
        if MIN_DTE <= dte <= MAX_DTE:
            out.append(exp)
    out.sort(key=lambda e: (abs((e - today).days - TARGET_DTE), e))
    return out


def get_pending_orders(account):
    """Return working orders (status pending/open) with leg symbols,
    tag and type."""
    pending = []
    for order in get_all_orders(account):
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


def has_blocking_pending_order(account, legs, exclude_id=None, ignore_credit=False, ignore_debit=False, ignore_tag=False):
    """True when the worker already has a pending order that would
    collide with the given legs: same tag, or the same leg symbols.
    Matching on leg symbols means the check works even for orders
    placed before tagging, and even if the provider does not echo the
    tag back. ignore_tag=True blocks on symbol overlap only: a roll
    for an existing condor must not be blocked by a working entry
    order for a different condor (different legs, same tag)."""
    new_symbols = {symbol for _, symbol, _ in legs}
    for order in get_pending_orders(account):
        if exclude_id is not None and str(order.get("id")) == str(exclude_id):
            continue
        if ignore_credit and order.get("type") == "credit":
            continue
        if ignore_debit and order.get("type") == "debit":
            continue
        if (not ignore_tag and order.get("tag") == ORDER_TAG) or order["symbols"] & new_symbols:
            return True, order.get("id")
    return False, None


def place_order(account, order_type, price, legs, duration="day", exclude_order_id=None, ignore_tag=False):
    """legs: list of (side, symbol, qty). Returns the API response dict.
    Never creates a second pending order: if a colliding order is already
    pending, the placement is skipped and logged."""
    blocked, blocking_id = has_blocking_pending_order(
        account, legs, exclude_id=exclude_order_id,
        ignore_credit=(order_type == "debit"),
        ignore_debit=(order_type == "credit"),
        ignore_tag=ignore_tag)
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
    the new one and open a second position."""
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


def close_legs_for(legs):
    """Closing legs for open legs: buy back shorts, sell longs. `legs`
    is a list of position dicts (with symbol/quantity) or of
    (side, symbol, qty) open-leg tuples."""
    out = []
    for leg in legs:
        if isinstance(leg, dict):
            side = "buy_to_close" if float(leg["quantity"]) < 0 else "sell_to_close"
            out.append((side, leg["symbol"], abs(int(leg["quantity"]))))
        else:
            side, symbol, qty = leg
            out.append(("buy_to_close" if side == "sell_to_open" else "sell_to_close",
                        symbol, qty))
    return out


def _order_legs(order):
    legs = order.get("leg")
    if legs is None:
        return []
    if isinstance(legs, dict):
        return [legs]
    return legs


def _order_fill_credit(order):
    """Net credit received on a filled credit order, in dollars for
    the whole order: Tradier reports a credit fill as a negative
    avg_fill_price (per share). Returns 0.0 when the fill price is
    missing or the order is not a credit."""
    if order.get("type") != "credit":
        return 0.0
    try:
        px = float(order.get("avg_fill_price"))
    except (TypeError, ValueError):
        return 0.0
    qty = 0
    for leg in _order_legs(order):
        try:
            qty = max(qty, abs(int(leg.get("quantity") or 0)))
        except (TypeError, ValueError):
            continue
    if qty <= 0:
        qty = CONTRACTS
    return round(-px * qty * 100.0, 2)


def _signed_qty(side, qty):
    """Signed quantity: sells are negative, buys positive. Order legs
    carry an unsigned quantity with the direction in `side`; position
    legs are already signed."""
    try:
        q = abs(int(qty or 0))
    except (TypeError, ValueError):
        return 0
    return -q if str(side or "").startswith("sell") else q


def _vertical_structure(pairs):
    """(True, '') when two (symbol, signed_qty) legs form one valid
    vertical of this worker's shape: same kind, same expiry, one
    short and one long in the right order (put long below the short,
    call short below the long), wing $20-$25, equal |quantity|."""
    if len(pairs) != 2:
        return False, "not 2 legs"
    parsed = []
    for symbol, qty in pairs:
        if not str(symbol).startswith("SPXW"):
            return False, "non-SPXW leg %s" % symbol
        kind, strike = _leg_kind_strike(symbol)
        exp = _expiry_from_symbol(symbol)
        if kind is None or exp is None:
            return False, "unparsed leg %s" % symbol
        parsed.append((kind, exp, strike, qty))
    if parsed[0][0] != parsed[1][0]:
        return False, "mixed kinds"
    if parsed[0][1] != parsed[1][1]:
        return False, "mixed expiries"
    if abs(parsed[0][3]) != abs(parsed[1][3]) or parsed[0][3] == 0:
        return False, "unequal quantities"
    kind = parsed[0][0]
    lo, hi = sorted(parsed, key=lambda t: t[2])
    if kind == "put":
        if not (lo[3] > 0 and hi[3] < 0):
            return False, "put legs not long-below/short-above"
    else:
        if not (lo[3] < 0 and hi[3] > 0):
            return False, "call legs not short-below/long-above"
    width = hi[2] - lo[2]
    if not (WING_WIDTH <= width <= MAX_WING_WIDTH):
        return False, "wing $%.0f outside $%d-$%d" % (width, WING_WIDTH, MAX_WING_WIDTH)
    return True, ""


def _condor_structure(pairs):
    """(True, '') when four (symbol, signed_qty) legs form an iron
    condor by this worker's own entry rules: 2 puts + 2 calls, all
    one expiry, both sides valid verticals. Anything else is not
    this worker's condor and must be left untouched."""
    if len(pairs) != 4:
        return False, "not 4 legs"
    exps = set()
    puts, calls = [], []
    for symbol, qty in pairs:
        if not str(symbol).startswith("SPXW"):
            return False, "non-SPXW leg %s" % symbol
        kind, _strike = _leg_kind_strike(symbol)
        exp = _expiry_from_symbol(symbol)
        if kind is None or exp is None:
            return False, "unparsed leg %s" % symbol
        exps.add(exp)
        (puts if kind == "put" else calls).append((symbol, qty))
    if len(exps) != 1:
        return False, "mixed expiries"
    if len(puts) != 2 or len(calls) != 2:
        return False, "not 2 puts + 2 calls"
    for side, legs in (("put", puts), ("call", calls)):
        ok, why = _vertical_structure(legs)
        if not ok:
            return False, "%s side: %s" % (side, why)
    return True, ""


def adopt_condors(account, positions):
    """Reconstruct the worker's open condors from the filled-orders
    feed, validated against current positions. Returns a list of
    condor dicts: {expiry, put_short, put_long, call_short,
    call_long, legs (position dicts), credit (total dollars received,
    entry plus roll credits), adjustments, gtc_id}.

    A filled 4-leg credit order with four open sides is an entry; a
    filled 4-leg credit order with two close sides and two open sides
    is a roll of the condor whose closing legs match. A filled 4-leg
    debit order with four close sides closes its condor. Orders are
    processed oldest first so rolls apply on top of entries.

    A grouped 4-leg condor in positions with no matching orders is
    adopted on its position cost basis as a fallback and logged
    loudly, so no open condor is ever left unmanaged after a
    restart."""
    condors = []
    by_close_symbol = {}

    def _find_by_legs(symbols):
        for c in condors:
            cur = {p["symbol"] for p in c["legs"]}
            if symbols <= cur:
                return c
        return None

    filled = [o for o in get_all_orders(account) if o.get("status") == "filled"]
    filled.sort(key=lambda o: str(o.get("create_date", "")))
    for order in filled:
        legs = _order_legs(order)
        if len(legs) != 4:
            continue
        symbols = [str(l.get("option_symbol", "")) for l in legs]
        if not all(s.startswith("SPXW") for s in symbols):
            continue
        sides = [str(l.get("side", "")) for l in legs]
        opens = [s for s in sides if s.endswith("_to_open")]
        closes = [s for s in sides if s.endswith("_to_close")]
        if order.get("type") == "credit" and len(opens) == 4:
            # Adopt only this worker's own condors. The orders feed
            # does not reliably echo tags, so a tag is checked when
            # present and the structure + credit decide the rest: a
            # foreign 4-leg credit (another strategy, a manual trade)
            # is left untouched and logged.
            tag = order.get("tag")
            if tag is not None and tag != ORDER_TAG:
                log("SKIP adopting 4-leg credit order %s: tag %s is not this worker's, leaving untouched"
                    % (order.get("id"), tag))
                continue
            pairs = [(str(l.get("option_symbol", "")),
                      _signed_qty(l.get("side"), l.get("quantity")))
                     for l in legs]
            ok, why = _condor_structure(pairs)
            if not ok:
                log("SKIP adopting 4-leg credit order %s: %s, leaving untouched"
                    % (order.get("id"), why))
                continue
            contracts = max(abs(q) for _, q in pairs)
            per_share = _order_fill_credit(order) / (contracts * 100.0)
            if not (MIN_CREDIT <= per_share <= MAX_CREDIT):
                log("SKIP adopting 4-leg credit order %s: fill credit $%.2f/share outside $%.2f-$%.2f, leaving untouched"
                    % (order.get("id"), per_share, MIN_CREDIT, MAX_CREDIT))
                continue
            condors.append({
                "legs": [{"symbol": s} for s in symbols],
                "credit": _order_fill_credit(order),
                "adjustments": 0,
                "gtc_id": None,
                "entry_order_id": order.get("id"),
            })
        elif order.get("type") == "credit" and len(opens) == 2 and len(closes) == 2:
            close_syms = {s for s, side in zip(symbols, sides) if side.endswith("_to_close")}
            target = _find_by_legs(close_syms)
            if target is None:
                continue
            open_pairs = [(str(l.get("option_symbol", "")),
                           _signed_qty(l.get("side"), l.get("quantity")))
                          for l in legs if str(l.get("side", "")).endswith("_to_open")]
            ok, why = _vertical_structure(open_pairs)
            if not ok:
                log("SKIP adopting roll order %s: new legs %s, leaving condor as-is"
                    % (order.get("id"), why))
                continue
            open_syms = [s for s, side in zip(symbols, sides) if side.endswith("_to_open")]
            kept = [p for p in target["legs"] if p["symbol"] not in close_syms]
            kept.extend({"symbol": s} for s in open_syms)
            target["legs"] = kept
            target["credit"] = round(target["credit"] + _order_fill_credit(order), 2)
            target["adjustments"] = target.get("adjustments", 0) + 1
        elif order.get("type") == "debit" and len(closes) == 4:
            close_syms = set(symbols)
            target = _find_by_legs(close_syms)
            if target is not None:
                condors.remove(target)
    # Validate against current positions and fill in full leg dicts.
    pos_by_symbol = {str(p.get("symbol", "")): p for p in positions}
    open_condors = []
    for c in condors:
        full = []
        ok = True
        for p in c["legs"]:
            pos = pos_by_symbol.get(p["symbol"])
            if pos is None:
                ok = False
                break
            full.append(pos)
        if not ok:
            continue
        c["legs"] = full
        _enrich_condor(c)
        open_condors.append(c)
    # Fallback: any grouped 4-leg condor in positions with no matching
    # orders. Group by expiry: one put vertical + one call vertical,
    # same |quantity|, wings $20-25.
    matched = {p["symbol"] for c in open_condors for p in c["legs"]}
    leftovers = [p for p in positions
                 if str(p.get("symbol", "")).startswith("SPXW")
                 and str(p.get("symbol", "")) not in matched]
    grouped = _group_condors(leftovers)
    for legs in grouped:
        basis = -sum(float(p.get("cost_basis", 0) or 0) for p in legs)
        contracts = max(abs(float(p.get("quantity", 0) or 0)) for p in legs)
        per_share = basis / (contracts * 100.0) if contracts else 0.0
        # Roll credits can lift a real condor above the entry cap, so
        # the fallback allows $2/share of headroom. A $0.55/share
        # manual-style condor is still outside and stays untouched.
        if not (MIN_CREDIT <= per_share <= MAX_CREDIT + 2.00):
            log("NOT adopting position-grouped condor %s: cost-basis credit $%.2f/share outside $%.2f-$%.2f, leaving untouched"
                % (", ".join(str(p.get("symbol", "")) for p in legs),
                   per_share, MIN_CREDIT, MAX_CREDIT + 2.00))
            continue
        c = {"legs": legs, "credit": round(basis, 2), "adjustments": 0,
             "gtc_id": None, "entry_order_id": None}
        _enrich_condor(c)
        log("ADOPTED condor from positions (no matching entry order): "
            "expiry %s %dP/%dP %dC/%dC credit %.2f"
            % (c["expiry"], c["put_short"], c["put_long"],
               c["call_short"], c["call_long"], c["credit"]))
        open_condors.append(c)
    return open_condors


def _enrich_condor(c):
    """Fill in expiry/strikes from a condor's leg dicts."""
    kinds = {}
    for p in c["legs"]:
        kind, strike = _leg_kind_strike(p.get("symbol", ""))
        if kind is not None:
            kinds.setdefault(kind, []).append((strike, p))
    for kind in ("put", "call"):
        pairs = sorted(kinds.get(kind, []))
        if len(pairs) >= 2:
            shorts = [s for s, p in pairs if float(p.get("quantity", 0)) < 0]
            longs = [s for s, p in pairs if float(p.get("quantity", 0)) > 0]
            if shorts and longs:
                if kind == "put":
                    c["put_short"] = int(max(shorts))
                    c["put_long"] = int(min(longs))
                else:
                    c["call_short"] = int(min(shorts))
                    c["call_long"] = int(max(longs))
    exps = {_expiry_from_symbol(p.get("symbol", "")) for p in c["legs"]}
    exps.discard(None)
    # The manage rule (21-DTE) keys off the NEAREST leg expiry: the
    # side that expires first is the gamma risk. All expiries are
    # kept for the one-condor-per-expiry entry check.
    c["expiry"] = min(exps) if exps else None
    c["expiries"] = exps


def _group_condors(legs):
    """Group SPXW position legs into iron condors: one put vertical
    (short + long, same expiry, $20-25 apart) plus one call vertical
    with the same |quantity|. Returns a list of 4-leg lists."""
    by_key = {}
    for p in legs:
        kind, strike = _leg_kind_strike(p.get("symbol", ""))
        exp = _expiry_from_symbol(p.get("symbol", ""))
        if kind is None or exp is None:
            continue
        qty = float(p.get("quantity", 0))
        by_key.setdefault((kind, exp), []).append((strike, qty, p))
    verticals = []
    for (kind, exp), items in by_key.items():
        shorts = sorted(s for s, q, _ in items if q < 0)
        longs = sorted(s for s, q, _ in items if q > 0)
        for ss in shorts:
            for ls in longs:
                width = abs(ss - ls)
                if WING_WIDTH <= width <= MAX_WING_WIDTH:
                    short_p = next(p for s, q, p in items if s == ss and q < 0)
                    long_p = next(p for s, q, p in items if s == ls and q > 0)
                    if abs(float(short_p.get("quantity", 0))) == abs(float(long_p.get("quantity", 0))):
                        verticals.append((kind, exp, [short_p, long_p]))
                        break
    puts = [v for v in verticals if v[0] == "put"]
    calls = [v for v in verticals if v[0] == "call"]
    out = []
    used_calls = set()
    for i, (pk, pe, plegs) in enumerate(puts):
        for j, (ck, ce, clegs) in enumerate(calls):
            if j in used_calls:
                continue
            if abs(float(plegs[0].get("quantity", 0))) == abs(float(clegs[0].get("quantity", 0))):
                out.append(plegs + clegs)
                used_calls.add(j)
                break
    return out


def _untouched_legs(positions, condors):
    """(SPXW symbols, non-SPXW count) for open legs that are not part
    of any adopted condor. Those legs are off-limits: another lane's
    legs, a manual position, anything else. The worker never touches
    or manages them."""
    matched = {p["symbol"] for c in condors for p in c["legs"]}
    spxw_left = sorted({str(p.get("symbol", "")) for p in positions
                        if str(p.get("symbol", "")).startswith("SPXW")
                        and str(p.get("symbol", "")) not in matched})
    other_left = sum(1 for p in positions
                     if not str(p.get("symbol", "")).startswith("SPXW"))
    return spxw_left, other_left


def condor_buyback_credit(account, condor):
    """(buyback, credit, legs) for one open condor: buyback is the
    mid cost to close all four legs, credit is the total dollars
    received (entry plus roll credits). Returns (0.0, 0.0, []) when
    the condor is gone."""
    legs = condor.get("legs") or []
    if not legs:
        return 0.0, 0.0, []
    symbols = ",".join(p["symbol"] for p in legs)
    data = api_get("/v1/markets/quotes?symbols=%s&greeks=false" % symbols)
    quotes = data["quotes"]["quote"]
    if isinstance(quotes, dict):
        quotes = [quotes]
    mids = {}
    for q in quotes:
        try:
            mids[q["symbol"]] = (float(q["bid"]) + float(q["ask"])) / 2.0
        except (KeyError, TypeError, ValueError):
            continue
    value = 0.0
    for leg in legs:
        mid = mids.get(leg["symbol"], 0.0)
        value += float(leg["quantity"]) * mid * 100.0
    return -value, float(condor.get("credit", 0.0)), legs


def find_gtc_for_legs(account, close_legs):
    """Return the id of a working debit (GTC take-profit) order whose
    leg symbols match these close legs, or None. Matching is by leg
    symbols, not by tag, because Tradier's orders feed does not echo
    the tag back. Used to adopt a GTC placed before a restart, so no
    duplicate GTC is ever placed."""
    target = {symbol for _, symbol, _ in close_legs}
    for order in get_pending_orders(account):
        if order.get("type") != "debit":
            continue
        if order["symbols"] == target:
            return order["id"]
    return None


def place_gtc_take_profit(account, condor, credit):
    """Place the standing GTC take-profit for an open condor: a debit
    order to buy it back at 50% of the total credit received. Returns
    the order id, or None when skipped (a GTC is already working for
    these legs, or the placement is blocked)."""
    legs = condor.get("legs") or []
    if not legs:
        return None
    contracts = max(abs(int(leg["quantity"])) for leg in legs)
    if credit <= 0 or contracts <= 0:
        return None
    price = round(PROFIT_TARGET_FRACTION * credit / (contracts * 100.0), 2)
    price = max(price, 0.01)
    close_legs = close_legs_for(legs)
    existing = find_gtc_for_legs(account, close_legs)
    if existing is not None:
        log("GTC take-profit already working (order %s), not duplicating"
            % existing)
        return existing
    resp = place_order(account, "debit", price, close_legs, duration="gtc")
    if resp.get("blocked") or resp.get("dry_run"):
        return None
    order = resp.get("order", resp)
    log("GTC take-profit placed id=%s buyback %.2f (50%% of credit %.2f)"
        % (order.get("id"), price, credit))
    return order.get("id")


def close_condor(account, condor, reason):
    """Buy back the open condor at the mid. Cancels a blocking order
    first — the working GTC take-profit, or an unfilled entry for the
    same strikes — so the close is the only order working on these
    legs. The close is a DEBIT order for the buy-back cost; a negative
    or zero buy-back is floored at $0.01, since Tradier rejects a
    zero/negative price. Returns early when the condor is already
    gone."""
    buyback, credit, fresh_legs = condor_buyback_credit(account, condor)
    if not fresh_legs:
        log("closing (%s): condor already closed, nothing to do" % reason)
        return {"already_closed": True}
    legs = fresh_legs
    contracts = max(abs(int(leg["quantity"])) for leg in legs)
    price = round(buyback / (contracts * 100.0), 2)
    close_legs = close_legs_for(legs)
    blocked, blocking_id = has_blocking_pending_order(account, close_legs)
    if blocked:
        if cancel_confirmed(account, blocking_id):
            log("closing (%s): cancelled working order %s to place the close"
                % (reason, blocking_id))
        else:
            log("closing (%s): cancel of order %s refused and still pending, close skipped, will retry"
                % (reason, blocking_id))
            return {"blocked": True, "blocking_order_id": blocking_id}
    log("closing (%s): buyback=%.2f credit=%.2f pnl=%.2f debit=%.2f"
        % (reason, buyback, credit, credit - buyback, max(price, 0.01)))
    return place_order(account, "debit", max(price, 0.01), close_legs,
                       exclude_order_id=blocking_id if blocked else None)


def _short_delta_for(condor, side, chain):
    """Current delta of one side's short leg from a fresh chain, or
    None when unreadable."""
    if side == "put":
        opt = chain.get((float(condor["put_short"]), "put"))
    else:
        opt = chain.get((float(condor["call_short"]), "call"))
    if opt is None:
        return None
    return chain_delta(opt)


def side_tested(condor, side, spx):
    """(True, reason) when one side is tested: SPX within 1% of its
    short strike, or that short leg's delta at/above TEST_DELTA."""
    if side == "put":
        short = float(condor["put_short"])
        expiry = _expiry_from_symbol(
            next(p["symbol"] for p in condor["legs"]
                 if _leg_kind_strike(p.get("symbol", ""))[0] == "put"
                 and float(p.get("quantity", 0)) < 0))
    else:
        short = float(condor["call_short"])
        expiry = _expiry_from_symbol(
            next(p["symbol"] for p in condor["legs"]
                 if _leg_kind_strike(p.get("symbol", ""))[0] == "call"
                 and float(p.get("quantity", 0)) < 0))
    if abs(spx - short) / spx <= TEST_DISTANCE_PCT:
        return True, "SPX %.2f within %.1f%% of %s short %d" % (
            spx, TEST_DISTANCE_PCT * 100, side, short)
    if expiry is not None:
        chain = get_chain(expiry)
        d = _short_delta_for(condor, side, chain)
        if d is not None and abs(d) >= TEST_DELTA:
            return True, "%s short %d delta %.2f at/above %.2f" % (
                side, short, d, TEST_DELTA)
    return False, ""


def select_roll(condor, side, spx, today, expirations):
    """Pick the roll for a tested side: the next expiry 7-14 days
    past the side's current expiry, a new short at ~16 delta at
    least ROLL_FURTHER further out of the money, and a new long the
    nearest listed strike at least WING_WIDTH further out (wing at
    most MAX_WING_WIDTH). Returns (roll dict, None) or (None, reason).
    The roll is a single 4-leg credit order: close the old spread,
    open the new one. roll_credit_per_share must be positive."""
    if side == "put":
        old_short = int(condor["put_short"])
        old_long = int(condor["put_long"])
        old_expiry = _expiry_from_symbol(
            next(p["symbol"] for p in condor["legs"]
                 if _leg_kind_strike(p.get("symbol", ""))[0] == "put"
                 and float(p.get("quantity", 0)) < 0))
        kind = "put"
    else:
        old_short = int(condor["call_short"])
        old_long = int(condor["call_long"])
        old_expiry = _expiry_from_symbol(
            next(p["symbol"] for p in condor["legs"]
                 if _leg_kind_strike(p.get("symbol", ""))[0] == "call"
                 and float(p.get("quantity", 0)) < 0))
        kind = "call"
    if old_expiry is None:
        return None, "cannot parse %s expiry" % side
    current_dte = (old_expiry - today).days
    new_expiry = None
    for exp in expirations:
        dte = (exp - today).days
        if current_dte + ROLL_MIN_DAYS <= dte <= current_dte + ROLL_MAX_DAYS:
            new_expiry = exp
            break
    if new_expiry is None:
        return None, "no expiry %d-%d days past %s" % (
            current_dte + ROLL_MIN_DAYS, current_dte + ROLL_MAX_DAYS, old_expiry)
    chain = get_chain(new_expiry)
    if not chain:
        return None, "no chain for %s" % new_expiry
    # New short: ~16 delta at the new expiry, at least ROLL_FURTHER
    # further out of the money than the old short.
    pool = [(s, o) for (s, k), o in chain.items() if k == kind]
    best = None
    for strike, opt in pool:
        if kind == "put" and strike > old_short - ROLL_FURTHER:
            continue
        if kind == "call" and strike < old_short + ROLL_FURTHER:
            continue
        d = chain_delta(opt)
        if d is None or not (DELTA_MIN <= abs(d) <= DELTA_MAX):
            continue
        if best is None or abs(abs(d) - TARGET_DELTA) < abs(abs(chain_delta(best[1])) - TARGET_DELTA):
            best = (strike, opt)
    if best is None:
        return None, "no %s short %d+ further OTM at 16 delta on %s" % (
            kind, ROLL_FURTHER, new_expiry)
    new_short, new_short_opt = best
    new_long, new_long_opt, width = _long_for_short(chain, kind, new_short)
    if new_long is None:
        return None, "no %s long within $%d of %d on %s" % (
            kind, MAX_WING_WIDTH, new_short, new_expiry)
    old_chain = get_chain(old_expiry)
    old_short_opt = old_chain.get((float(old_short), kind))
    old_long_opt = old_chain.get((float(old_long), kind))
    for opt, label in ((new_short_opt, "new short"), (new_long_opt, "new long"),
                       (old_short_opt, "old short"), (old_long_opt, "old long")):
        if opt is None or not _two_sided(opt):
            return None, "%s has no two-sided quote" % label
    new_spread = chain_mid(new_short_opt) - chain_mid(new_long_opt)
    old_spread = chain_mid(old_short_opt) - chain_mid(old_long_opt)
    roll_credit = round(new_spread - old_spread, 2)
    if roll_credit <= 0:
        return None, "roll is a %.2f debit, not a credit — close instead" % (-roll_credit)
    legs = [
        ("buy_to_close", option_symbol(old_expiry, old_short, "P" if kind == "put" else "C"), CONTRACTS),
        ("sell_to_close", option_symbol(old_expiry, old_long, "P" if kind == "put" else "C"), CONTRACTS),
        ("sell_to_open", option_symbol(new_expiry, int(new_short), "P" if kind == "put" else "C"), CONTRACTS),
        ("buy_to_open", option_symbol(new_expiry, int(new_long), "P" if kind == "put" else "C"), CONTRACTS),
    ]
    return {
        "side": side, "old_expiry": old_expiry, "new_expiry": new_expiry,
        "old_short": old_short, "old_long": old_long,
        "new_short": int(new_short), "new_long": int(new_long),
        "roll_credit_per_share": roll_credit, "legs": legs,
    }, None


def select_roll_in(condor, side, spx, chain):
    """Roll the UNTESTED side toward SPX at the same expiry, back to
    ~16 delta, for a credit. Used after a tested-side roll when the
    untested short has drifted too far out (delta below 0.10).
    Returns (roll dict, None) or (None, reason)."""
    if side == "put":
        old_short = int(condor["put_short"])
        old_long = int(condor["put_long"])
        expiry = _expiry_from_symbol(
            next(p["symbol"] for p in condor["legs"]
                 if _leg_kind_strike(p.get("symbol", ""))[0] == "put"
                 and float(p.get("quantity", 0)) < 0))
        kind = "put"
    else:
        old_short = int(condor["call_short"])
        old_long = int(condor["call_long"])
        expiry = _expiry_from_symbol(
            next(p["symbol"] for p in condor["legs"]
                 if _leg_kind_strike(p.get("symbol", ""))[0] == "call"
                 and float(p.get("quantity", 0)) < 0))
        kind = "call"
    if expiry is None:
        return None, "cannot parse %s expiry" % side
    new_short, new_short_opt = _short_for_delta(chain, kind, spx)
    if new_short is None:
        return None, "no %s short in delta range at %s" % (kind, expiry)
    if kind == "put" and new_short <= old_short:
        return None, "roll-in would not move the put short toward SPX"
    if kind == "call" and new_short >= old_short:
        return None, "roll-in would not move the call short toward SPX"
    new_long, new_long_opt, width = _long_for_short(chain, kind, new_short)
    if new_long is None:
        return None, "no %s long within $%d of %d" % (kind, MAX_WING_WIDTH, new_short)
    old_short_opt = chain.get((float(old_short), kind))
    old_long_opt = chain.get((float(old_long), kind))
    for opt, label in ((new_short_opt, "new short"), (new_long_opt, "new long"),
                       (old_short_opt, "old short"), (old_long_opt, "old long")):
        if opt is None or not _two_sided(opt):
            return None, "%s has no two-sided quote" % label
    new_spread = chain_mid(new_short_opt) - chain_mid(new_long_opt)
    old_spread = chain_mid(old_short_opt) - chain_mid(old_long_opt)
    roll_credit = round(new_spread - old_spread, 2)
    if roll_credit <= 0:
        return None, "roll-in is a %.2f debit, skipping" % (-roll_credit)
    legs = [
        ("buy_to_close", option_symbol(expiry, old_short, "P" if kind == "put" else "C"), CONTRACTS),
        ("sell_to_close", option_symbol(expiry, old_long, "P" if kind == "put" else "C"), CONTRACTS),
        ("sell_to_open", option_symbol(expiry, int(new_short), "P" if kind == "put" else "C"), CONTRACTS),
        ("buy_to_open", option_symbol(expiry, int(new_long), "P" if kind == "put" else "C"), CONTRACTS),
    ]
    return {
        "side": side, "old_expiry": expiry, "new_expiry": expiry,
        "old_short": old_short, "old_long": old_long,
        "new_short": int(new_short), "new_long": int(new_long),
        "roll_credit_per_share": roll_credit, "legs": legs,
    }, None


def fallback_exit_allowed(account, condor, gtc_working, buyback, credit, fraction, below):
    """Whether the poll-based fallback may close the condor now.

    Guards against one-poll phantom prints (Nov 19, 2026: a $150
    buyback print tripped the 50% target on a trend spread, cancelling
    the working GTC to place a worse close that then blocked
    re-placing the GTC). Two guards, as in the trend/QQQ workers:
    (1) profit target only: when the take-profit GTC is working, let
        it fill — the fallback fires only when no GTC is working. A
        stop skips this guard: the 50% take-profit is far out of the
        money when the stop triggers, so it cannot protect the
        position.
    (2) re-verify the target on a fresh read; skip the close when the
        trigger print did not hold, or when the re-read legs are not
        the tracked condor's legs.
    `below` True: exit triggers when buyback <= fraction * credit
    (profit target). False: buyback >= fraction * credit (stop).
    Returns True only when the close may proceed."""
    if below and gtc_working:
        log("poll exit: take-profit GTC working, letting it fill")
        return False
    _b2, _c2, _l2 = condor_buyback_credit(account, condor)
    if not _l2 or _c2 <= 0:
        log("poll exit: condor gone on re-read, skipping close")
        return False
    if (_b2 <= fraction * _c2) if below else (_b2 >= fraction * _c2):
        return True
    log("poll exit: trigger print %.2f did not hold on re-read (buyback %.2f), skipping close"
        % (buyback, _b2))
    return False


def pnl_snapshot(account):
    bal = get_balances(account)
    positions = get_positions(account)
    condors = adopt_condors(account, positions)
    unrealized = 0.0
    for c in condors:
        buyback, credit, legs = condor_buyback_credit(account, c)
        if legs:
            unrealized += credit - buyback
    log("P&L equity=%.2f realized_today=%.2f unrealized=%.2f open_condors=%d"
        % (float(bal["equity"]), float(bal["close_pl"]), unrealized, len(condors)))


def heartbeat_line(now, condors, vix_txt, spx_txt):
    if condors:
        parts = []
        for c in condors:
            parts.append("%s %dP/%dC" % (c["expiry"], c["put_short"], c["call_short"]))
        return "heartbeat: open %d condor(s) %s VIX %s SPX %s" % (
            len(condors), ", ".join(parts), vix_txt, spx_txt)
    return "heartbeat: flat VIX %s SPX %s" % (vix_txt, spx_txt)


def _condor_key(condor):
    shorts = sorted(p["symbol"] for p in condor.get("legs") or []
                    if float(p.get("quantity", 0)) < 0)
    return tuple(shorts)


def run_once():
    if not API_KEY:
        log("TRADIER_API_KEY is not set, exiting")
        return 1
    account = resolve_account()
    log("account %s connected (paper host %s, trading %s, contracts %d, "
        "condor %d-%d DTE target %d, short %.0f delta, wings $%d max $%d, "
        "min credit $%.2f, max %d open, VIX max %.1f)"
        % (account, HOST, "ENABLED" if ENABLE_TRADING else "dry-run",
           CONTRACTS, MIN_DTE, MAX_DTE, TARGET_DTE, TARGET_DELTA * 100,
           WING_WIDTH, MAX_WING_WIDTH, MIN_CREDIT, MAX_OPEN, VIX_MAX))
    last_pnl_day = {}
    working_entry = None
    working_rolls = {}  # order id -> {condor key, roll dict}
    gtc_cleanup_done = False
    last_untouched = None  # (spxw symbols, non-spxw count) from last poll
    while True:
        try:
            now = datetime.now(TZ)
            today = now.date()
            if now.weekday() >= 5:
                time.sleep(POLL_SECONDS)
                continue
            for hour in (6, 8):
                if now.hour == hour and last_pnl_day.get(hour) != today:
                    pnl_snapshot(account)
                    last_pnl_day[hour] = today
            # Working rolls: check whether each roll order filled,
            # was cancelled/rejected, or is still working. A filled
            # roll changes the condor's legs, so the old GTC (on the
            # old legs) is stale and must be cancelled before a fresh
            # one is placed for the new legs.
            if working_rolls:
                try:
                    all_orders = {str(o.get("id")): o for o in get_all_orders(account)}
                except Exception as exc:  # noqa: BLE001 - retry next poll
                    log("roll status check failed (%s), keeping rolls tracked" % exc)
                    all_orders = {}
                for oid in list(working_rolls):
                    order = all_orders.get(str(oid))
                    if order is None:
                        continue
                    status = order.get("status")
                    if status == "filled":
                        roll = working_rolls.pop(oid)["roll"]
                        log("roll filled: %s side %d/%d -> %d/%d expiry %s, "
                            "credit $%.2f/share"
                            % (roll["side"], roll["old_short"], roll["old_long"],
                               roll["new_short"], roll["new_long"],
                               roll["new_expiry"], roll["roll_credit_per_share"]))
                        old_close = [
                            ("buy_to_close", option_symbol(roll["old_expiry"], roll["old_short"],
                                                           "P" if roll["side"] == "put" else "C"), CONTRACTS),
                            ("sell_to_close", option_symbol(roll["old_expiry"], roll["old_long"],
                                                            "P" if roll["side"] == "put" else "C"), CONTRACTS),
                        ]
                        stale_gtc = find_gtc_for_legs(account, old_close)
                        if stale_gtc is not None:
                            if cancel_confirmed(account, stale_gtc):
                                log("stale GTC %s cancelled after roll fill" % stale_gtc)
                    elif status in ("canceled", "rejected", "expired"):
                        log("roll order %s %s, clearing" % (oid, status))
                        working_rolls.pop(oid)
            positions = get_positions(account)
            condors = adopt_condors(account, positions)
            # Do-not-touch check: any open leg that is not part of an
            # adopted condor is off-limits — another worker's legs, a
            # manual position, anything else. Log the set when it
            # changes so foreign legs stay visible and are never
            # silently managed.
            spxw_left, other_left = _untouched_legs(positions, condors)
            untouched = (tuple(spxw_left), other_left)
            if untouched != last_untouched:
                last_untouched = untouched
                if spxw_left or other_left:
                    log("check: %d SPXW leg(s) not part of any adopted condor, leaving untouched: %s%s"
                        % (len(spxw_left), ", ".join(spxw_left),
                           ("; plus %d non-SPXW position(s), also untouched" % other_left)
                           if other_left else ""))
                else:
                    if positions:
                        log("check: every open leg belongs to an adopted condor, nothing left untouched")
                    else:
                        log("check: account flat, no legs to leave untouched")
            try:
                spx_txt = "%.2f" % get_spx_price()
                spx = float(spx_txt)
            except Exception:
                spx_txt = "?"
                spx = None
            try:
                vix_q = api_get("/v1/markets/quotes?symbols=VIX&greeks=false")
                vix_v = vix_q["quotes"]["quote"]
                if isinstance(vix_v, list):
                    vix_v = vix_v[0]
                vix_txt = "%.2f" % float(vix_v["last"])
            except Exception:
                vix_txt = "?"
            log(heartbeat_line(now, condors, vix_txt, spx_txt))
            # Best-effort stale-GTC cleanup once per run: a working
            # debit order carrying this worker's tag, matching no open
            # condor's close legs, is a leftover take-profit. Cancel
            # it once so it cannot close a future position.
            if not gtc_cleanup_done:
                gtc_cleanup_done = True
                try:
                    live_close_sets = []
                    for c in condors:
                        live_close_sets.append(
                            {s for _, s, _ in close_legs_for(c["legs"])})
                    for order in get_pending_orders(account):
                        if order.get("type") == "debit" and order.get("tag") == ORDER_TAG:
                            if all(order["symbols"] != s for s in live_close_sets):
                                try:
                                    cancel_order(account, order["id"])
                                    log("stale GTC %s cancelled (no open condor behind it)"
                                        % order["id"])
                                except Exception as exc:  # noqa: BLE001 - best effort
                                    log("stale GTC %s cancel refused (%s), ignoring"
                                        % (order["id"], exc))
                                break
                except Exception as exc:  # noqa: BLE001 - best effort
                    log("stale GTC cleanup failed (%s), ignoring" % exc)
            # A filled entry clears the working entry.
            if working_entry is not None:
                filled_entry = False
                for c in condors:
                    if (c.get("expiry") == working_entry.get("expiry")
                            and c.get("put_short") == working_entry.get("put_short")
                            and c.get("call_short") == working_entry.get("call_short")):
                        filled_entry = True
                        break
                if filled_entry:
                    log("entry order %s filled" % working_entry["id"])
                    working_entry = None
            # Manage each open condor.
            for condor in list(condors):
                key = _condor_key(condor)
                buyback, credit, legs = condor_buyback_credit(account, condor)
                if not legs:
                    continue
                # Standing GTC take-profit: place one (buy back at 50%
                # of the total credit) when a condor opens. Adopt the
                # working one after a restart instead of duplicating it.
                close_legs = close_legs_for(legs)
                gtc_id = find_gtc_for_legs(account, close_legs)
                if gtc_id is None:
                    gtc_id = place_gtc_take_profit(account, condor, credit)
                # 21-DTE manage: close at the mid, whatever the P&L.
                expiry = condor.get("expiry")
                if expiry is not None and (expiry - today).days <= MANAGE_DTE:
                    log("21-DTE manage: closing condor expiring %s" % expiry)
                    close_condor(account, condor, "21-DTE manage")
                    continue
                # Profit target fallback (guarded: never while the GTC
                # is working, re-verified on a fresh read).
                if credit > 0 and buyback <= PROFIT_TARGET_FRACTION * credit:
                    if fallback_exit_allowed(
                            account, condor, gtc_id is not None,
                            buyback, credit, PROFIT_TARGET_FRACTION, True):
                        log("profit target hit: buyback %.2f on credit %.2f"
                            % (buyback, credit))
                        close_condor(account, condor, "50% profit target")
                    continue
                # Stop: buy-back cost reached 3x total credit (a loss
                # of 2x the credit received). Guarded with a fresh
                # re-read; does not wait for the GTC.
                if credit > 0 and buyback >= (1.0 + STOP_MULTIPLE) * credit:
                    if fallback_exit_allowed(
                            account, condor, False,
                            buyback, credit, (1.0 + STOP_MULTIPLE), False):
                        log("stop hit: buyback %.2f on credit %.2f"
                            % (buyback, credit))
                        close_condor(account, condor, "stop 2x credit")
                    continue
                # Adjustment ladder: a tested side rolls out 7-14 days
                # and $25+ further OTM for a credit; the untested side
                # rolls in when its delta has fallen below 0.10.
                # At most MAX_ADJUSTMENTS per condor.
                if condor.get("adjustments", 0) >= MAX_ADJUSTMENTS:
                    continue
                # A roll already working for this condor blocks a
                # second one; the working roll is resolved at the top
                # of the next poll (filled / cancelled / still working).
                if any(v.get("key") == key for v in working_rolls.values()):
                    continue
                if spx is None:
                    continue
                roll_placed = False
                for side in ("put", "call"):
                    tested, test_reason = side_tested(condor, side, spx)
                    if not tested:
                        continue
                    log("tested side: %s" % test_reason)
                    expirations = get_expirations()
                    roll, roll_reason = select_roll(condor, side, spx, today, expirations)
                    if roll is None:
                        log("no roll for %s side (%s), closing condor instead"
                            % (side, roll_reason))
                        close_condor(account, condor, "no credit roll available")
                        roll_placed = True
                        break
                    resp = place_order(account, "credit", roll["roll_credit_per_share"],
                                       roll["legs"], ignore_tag=True)
                    if resp.get("blocked"):
                        log("roll placement blocked, will retry next poll")
                        roll_placed = True
                        break
                    if resp.get("dry_run"):
                        roll_placed = True
                        break
                    order = resp.get("order", resp)
                    working_rolls[str(order.get("id"))] = {"key": key, "roll": roll}
                    log("roll placed: %s side %d/%d -> %d/%d expiry %s, "
                        "credit $%.2f/share"
                        % (side, roll["old_short"], roll["old_long"],
                           roll["new_short"], roll["new_long"],
                           roll["new_expiry"], roll["roll_credit_per_share"]))
                    # Roll the untested side in when it has drifted
                    # too far out (delta below 0.10).
                    other = "call" if side == "put" else "put"
                    try:
                        other_expiry = _expiry_from_symbol(
                            next(p["symbol"] for p in condor["legs"]
                                 if _leg_kind_strike(p.get("symbol", ""))[0] == other
                                 and float(p.get("quantity", 0)) < 0))
                        other_chain = get_chain(other_expiry) if other_expiry else {}
                        other_delta = _short_delta_for(condor, other, other_chain)
                        if other_delta is not None and abs(other_delta) < 0.10:
                            roll_in, ri_reason = select_roll_in(
                                condor, other, spx, other_chain)
                            if roll_in is not None:
                                ri_resp = place_order(
                                    account, "credit", roll_in["roll_credit_per_share"],
                                    roll_in["legs"], ignore_tag=True)
                                if not ri_resp.get("blocked") and not ri_resp.get("dry_run"):
                                    ri_order = ri_resp.get("order", ri_resp)
                                    working_rolls[str(ri_order.get("id"))] = {
                                        "key": key, "roll": roll_in}
                                    log("roll-in placed: %s side %d/%d -> %d/%d, "
                                        "credit $%.2f/share"
                                        % (other, roll_in["old_short"], roll_in["old_long"],
                                           roll_in["new_short"], roll_in["new_long"],
                                           roll_in["roll_credit_per_share"]))
                            else:
                                log("no roll-in for %s side (%s)" % (other, ri_reason))
                    except Exception as exc:  # noqa: BLE001 - roll-in is optional
                        log("roll-in check failed (%s), skipping" % exc)
                    roll_placed = True
                    break
                if roll_placed:
                    continue
            # Entry evaluation: fewer than MAX_OPEN condors, in window.
            if len(condors) >= MAX_OPEN:
                time.sleep(POLL_SECONDS)
                continue
            in_window = ENTRY_START <= now.time() < ENTRY_END
            if working_entry is not None and not in_window:
                if cancel_confirmed(account, working_entry["id"]):
                    log("entry order %s cancelled, window ended" % working_entry["id"])
                    working_entry = None
                else:
                    log("entry order %s still working at window end, keeping it tracked"
                        % working_entry["id"])
            if not in_window:
                time.sleep(POLL_SECONDS)
                continue
            if spx is None:
                time.sleep(POLL_SECONDS)
                continue
            ok, gate_reason = vix_gate()
            if not ok:
                log("no entry: %s" % gate_reason)
                time.sleep(POLL_SECONDS)
                continue
            blocked, blocked_date = entry_blocked_by_event(today)
            if blocked:
                log("no entry: event date %s (blocked)" % blocked_date)
                time.sleep(POLL_SECONDS)
                continue
            try:
                quote = get_spx_quote()
                prevclose = float(quote.get("prevclose"))
                if prevclose and abs(spx - prevclose) / prevclose > GAP_SKIP_PCT:
                    log("no entry: SPX gapped %.2f%% from prior close" % (
                        (spx - prevclose) / prevclose * 100.0))
                    time.sleep(POLL_SECONDS)
                    continue
            except Exception as exc:  # noqa: BLE001 - gap check is advisory
                log("gap check failed (%s), proceeding" % exc)
            held = held_spxw_strikes(account)
            open_expiries = set()
            for c in condors:
                open_expiries |= c.get("expiries") or set()
                if c.get("expiry") is not None:
                    open_expiries.add(c["expiry"])
            selected = None
            select_reason = ""
            for expiry in candidate_expiries(today):
                if expiry in open_expiries:
                    continue
                chain = get_chain(expiry)
                if not chain:
                    continue
                selected, select_reason = select_condor(spx, expiry, chain, held)
                if selected is not None:
                    break
                log("expiry %s skipped: %s" % (expiry, select_reason))
            if selected is None:
                log("no condor: %s (SPX %.2f)" % (select_reason, spx))
                time.sleep(POLL_SECONDS)
                continue
            if working_entry is not None:
                if (working_entry.get("expiry") != selected["expiry"]
                        or working_entry.get("put_short") != selected["put_short"]
                        or working_entry.get("call_short") != selected["call_short"]):
                    if cancel_confirmed(account, working_entry["id"]):
                        log("entry order %s cancelled, selection moved"
                            % working_entry["id"])
                        working_entry = None
                    else:
                        log("entry order %s still working, replacement skipped this poll"
                            % working_entry["id"])
                        time.sleep(POLL_SECONDS)
                        continue
                else:
                    time.sleep(POLL_SECONDS)
                    continue
            # No new entry while any SPXW credit order is working (a
            # pre-restart entry the in-memory state lost, or a manual
            # order): placing on top of it opens a second position.
            try:
                pending = get_pending_orders(account)
                blocking_credit = None
                for order in pending:
                    if order.get("type") != "credit":
                        continue
                    if any(str(s).startswith("SPXW") for s in order.get("symbols", set())):
                        blocking_credit = order.get("id")
                        break
                if blocking_credit is not None:
                    log("no entry: pending credit order %s still working, skipping this poll"
                        % blocking_credit)
                    time.sleep(POLL_SECONDS)
                    continue
            except Exception as exc:  # noqa: BLE001 - stay safe, skip the entry
                log("pending credit-entry check failed (%s), entry skipped this poll" % exc)
                time.sleep(POLL_SECONDS)
                continue
            log("entry: SPX %.2f %s, %s %dP/%dP %dC/%dC credit %.2f x %d"
                % (spx, gate_reason, selected["expiry"], selected["put_short"],
                   selected["put_long"], selected["call_short"], selected["call_long"],
                   selected["credit_per_share"], CONTRACTS))
            resp = place_order(account, "credit", selected["credit_per_share"], selected["legs"])
            if resp.get("blocked") or resp.get("dry_run"):
                time.sleep(POLL_SECONDS)
                continue
            order = resp.get("order", resp)
            working_entry = {
                "id": order.get("id"),
                "expiry": selected["expiry"],
                "put_short": selected["put_short"],
                "call_short": selected["call_short"],
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
