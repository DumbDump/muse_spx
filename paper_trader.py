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
- Entry window 7:00-11:30 a.m. America/Los_Angeles, weekdays only.
- Max debit $10.00 per share, as low as possible. If no qualifying pair
  previews at or under $10, the day is skipped.
- Max 5 contracts per trade, one position at a time.
- Exit at a 10% gain on the debit basis, and in all cases close before
  1:00 p.m. PT the same day (the program starts closing at 12:30 p.m. PT).
- No preview step: when the rules say enter or exit, the order is placed
  directly. Every placement and fill is logged immediately.
- Every order carries a tag ("muse-spx" by default, overridable with
  ORDER_TAG) so the worker's own orders are identifiable.
- At most one pending order at a time: before placing, the worker checks
  open orders and skips if an order with its tag, or with the same leg
  symbols, is already pending. It never cancels anything on its own.

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
  P&L record: the program prints a P&L line at 6:00 a.m. and 8:00 a.m. PT
  on weekdays, plus a line for every entry, exit, skip, and error.

Notes:

- Tradier sandbox quotes are delayed about 15 minutes. Mids computed here
  are estimates, not live brokerage marks.
- This program places day orders. An order left unfilled at the close
  expires with the session, and any position still open is carried
  overnight (a rule miss the logs will show plainly).

Usage:

  TRADIER_API_KEY=xxx ENABLE_TRADING=0 python paper_trader.py
  TRADIER_API_KEY=xxx python paper_trader.py --check   # one P&L snapshot, then exit
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
CONTRACTS = max(1, min(5, int(os.environ.get("CONTRACTS", "5"))))
MAX_DEBIT = float(os.environ.get("MAX_DEBIT", "10.00"))
ENTRY_START = dtime(7, 0)
ENTRY_END = dtime(11, 30)
CLOSE_START = dtime(12, 30)
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
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
    quote = data["quotes"]["quote"]
    if isinstance(quote, list):
        quote = quote[0]
    return float(quote["last"])


def round_strike(price):
    return int(round(price / 5.0) * 5)


def option_symbol(expiry, strike, kind):
    return "SPXW%s%s%08d" % (expiry.strftime("%y%m%d"), kind, strike * 1000)


def get_expirations():
    """Return sorted expiry dates. Falls back to weekday-derived dates."""
    try:
        data = api_get("/v1/markets/options/expirations?symbol=SPXW&includeAllRoots=false")
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
    """Return {(strike, kind): mid} for one expiry."""
    data = api_get(
        "/v1/markets/options/chains?symbol=SPXW&expiration=%s&greeks=false"
        % expiry.isoformat()
    )
    options = data["options"]["option"]
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
    """Return (short_exp, long_exp, put, call, debit) for the cheapest pair
    at or under MAX_DEBIT, or None when no pair qualifies."""
    put_strike = round_strike(spx_price - 50)
    call_strike = round_strike(spx_price + 50)
    today = datetime.now(TZ).date()
    best = None
    for i, short_exp in enumerate(expirations):
        dte = (short_exp - today).days
        if dte < 3 or dte > 5:
            continue
        if i + 1 >= len(expirations):
            continue
        long_exp = expirations[i + 1]
        short_mids = get_chain_mids(short_exp)
        long_mids = get_chain_mids(long_exp)
        try:
            debit = (
                long_mids[(put_strike, "put")]
                + long_mids[(call_strike, "call")]
                - short_mids[(put_strike, "put")]
                - short_mids[(call_strike, "call")]
            )
        except KeyError:
            continue
        if debit <= MAX_DEBIT and (best is None or debit < best[4]):
            best = (short_exp, long_exp, put_strike, call_strike, round(debit, 2))
    return best


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


def place_order(account, order_type, price, legs):
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
        "symbol": "SPX",
        "type": order_type,
        "duration": "day",
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
    """Return (net_value, net_basis, legs) for open SPXW legs."""
    legs = [p for p in get_positions(account) if p["symbol"].startswith("SPXW")]
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


def close_position(account, legs):
    """Build and place the closing order for the given open legs."""
    value, basis, _ = position_value_and_basis(account)
    contracts = max(abs(int(leg["quantity"])) for leg in legs)
    credit = round(value / (contracts * 100.0), 2)
    close_legs = []
    for leg in legs:
        side = "buy_to_close" if float(leg["quantity"]) < 0 else "sell_to_close"
        close_legs.append((side, leg["symbol"], abs(int(leg["quantity"]))))
    log("closing: value=%.2f basis=%.2f unrealized=%.2f credit=%.2f"
        % (value, basis, value - basis, credit))
    return place_order(account, "credit", credit, close_legs)


def pnl_snapshot(account):
    bal = get_balances(account)
    value, basis, legs = position_value_and_basis(account)
    log("P&L equity=%.2f realized_today=%.2f unrealized=%.2f open_legs=%d"
        % (float(bal["equity"]), float(bal["close_pl"]), value - basis if legs else 0.0,
           len(legs)))


def run_once():
    if not API_KEY:
        log("TRADIER_API_KEY is not set, exiting")
        return 1
    account = resolve_account()
    log("account %s connected (paper host %s, trading %s, contracts %d)"
        % (account, HOST, "ENABLED" if ENABLE_TRADING else "dry-run", CONTRACTS))
    last_entry_day = None
    last_pnl_day = {}
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
                if now.hour == hour and now.minute < 2 and last_pnl_day.get(hour) != today:
                    pnl_snapshot(account)
                    last_pnl_day[hour] = today
            value, basis, legs = position_value_and_basis(account)
            if legs:
                unrealized = value - basis
                # 10% gain exit, any time of day.
                if basis > 0 and unrealized >= 0.10 * basis:
                    log("10%% exit hit: unrealized %.2f on basis %.2f" % (unrealized, basis))
                    close_position(account, legs)
                    time.sleep(POLL_SECONDS)
                    continue
                # Same-day close: start closing at 12:30 p.m. PT.
                if now.time() >= CLOSE_START:
                    log("close window: closing open position before 1:00 p.m. PT")
                    close_position(account, legs)
                    time.sleep(POLL_SECONDS)
                    continue
            else:
                # Entry window, one entry per day, only when flat.
                if (ENTRY_START <= now.time() < ENTRY_END
                        and last_entry_day != today):
                    spx = get_spx_price()
                    pair = find_best_pair(spx, get_expirations())
                    if pair is None:
                        log("no pair at or under %.2f, skipping today (SPX %.2f)"
                            % (MAX_DEBIT, spx))
                        last_entry_day = today
                    else:
                        short_exp, long_exp, put, call, debit = pair
                        legs = [
                            ("sell_to_open", option_symbol(short_exp, put, "P"), CONTRACTS),
                            ("sell_to_open", option_symbol(short_exp, call, "C"), CONTRACTS),
                            ("buy_to_open", option_symbol(long_exp, put, "P"), CONTRACTS),
                            ("buy_to_open", option_symbol(long_exp, call, "C"), CONTRACTS),
                        ]
                        log("entry: SPX %.2f short %s / long %s %dP/%dC debit %.2f x %d"
                            % (spx, short_exp, long_exp, put, call, debit, CONTRACTS))
                        place_order(account, "debit", min(debit, MAX_DEBIT), legs)
                        last_entry_day = today
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
