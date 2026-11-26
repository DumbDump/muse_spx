#!/usr/bin/env python3
"""AVGO EMA-crossover stock trader for Railway (paper).

Implements one fixed rule set, nothing else:

- Tradier PAPER host only (sandbox.tradier.com). The host is hardcoded
  and the program refuses to run against any other host, so it cannot
  touch a live account.
- Symbol AVGO (env SYMBOL), 100 shares per trade (env QUANTITY).
- Signal is the EMA 14 / EMA 45 crossover on 10-minute AVGO bars,
  evaluated on CLOSED bars only: a bar counts once its 10-minute
  window has ended, so an in-progress bar can never trigger an entry.
  EMAs are seeded from the prior sessions' bars (last 4 calendar
  days fetched) so the read is valid from the open. A crossover only
  counts as fresh when the crossing bar closed today, so a restart
  after days offline cannot enter on a stale signal.
- Long entry: EMA 14 crosses ABOVE EMA 45 -> buy QUANTITY shares.
  Short entry: EMA 14 crosses BELOW EMA 45 -> sell short QUANTITY
  shares. One position at a time; entries only while flat.
- Exits: the OPPOSITE crossover closes the position (and the flip is
  queued: the opposite direction is entered on the next poll, once
  the close has filled and the EMA level still agrees), OR a stop
  loss of 1.5% from the entry price (env STOP_PCT). After a stop-out
  the worker stays flat until the next fresh crossover; it never
  re-enters on the same trend that just stopped it out.
- Positions are held until an exit fires: there is no end-of-day
  hard exit, so a position may be carried overnight. The stop is
  evaluated on every poll during market hours from the latest quote;
  an overnight gap is caught at the first poll after the open.
- Orders are market orders, day duration, tagged "muse-avgo-ema".
  Before placing, the worker skips if a working AVGO equity order
  already exists, so a duplicate entry is never placed.
- Adoption / do-not-touch: on every poll the worker reads the AVGO
  equity position. A position is treated as its own only when its
  quantity is exactly +/-QUANTITY and a filled AVGO equity order with
  this worker's tag exists for it. Any other AVGO position is foreign:
  it is never closed and no new entry is placed while it exists.

Safety:

- Paper orders are placed ONLY when the environment variable
  ENABLE_TRADING is set to "1". Otherwise the program runs in dry-run
  mode: it evaluates the rules, logs exactly what it would place, and
  places nothing. Deploy with dry-run first and flip the flag only
  after the logs look right.
- The Tradier API key comes from the TRADIER_API_KEY environment
  variable (set it in Railway, never in this file or in git).

Railway setup:

- Push this file plus Procfile to a repo, create a Railway service
  from it, and set TRADIER_API_KEY to a Tradier SANDBOX (paper) key.
- Procfile runs: avgo-ema-worker: python avgo_ema_paper_trader.py
- The worker loops forever, so Railway keeps it running. Logs are the
  P&L record: the program prints a heartbeat line every poll (so
  silence never means "is it running?"), a P&L line on the first poll
  of the 6:00 a.m. and 8:00 a.m. PT hours on weekdays, plus a line for
  every entry, exit, skip, and error.

Notes:

- Tradier sandbox quotes are delayed about 15 minutes. The stop loss
  is evaluated against that delayed last price, so a fast intraday
  move can reach the stop before the feed shows it.
- Entry orders are day orders: an order left unfilled at the close
  expires with the session.

Usage:

TRADIER_API_KEY=<redacted>
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
ET = ZoneInfo("America/New_York")
HOST = "sandbox.tradier.com"
BASE = "https://%s" % HOST

API_KEY = os.environ.get("TRADIER_API_KEY", "")
ENABLE_TRADING = os.environ.get("ENABLE_TRADING", "0") == "1"
SYMBOL = os.environ.get("SYMBOL", "AVGO")
QUANTITY = max(1, int(os.environ.get("QUANTITY", "100")))
EMA_FAST = int(os.environ.get("EMA_FAST", "14"))
EMA_SLOW = int(os.environ.get("EMA_SLOW", "45"))
STOP_PCT = float(os.environ.get("STOP_PCT", "0.015"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
MARKET_OPEN = dtime(6, 30)   # 9:30 a.m. ET
MARKET_CLOSE = dtime(13, 0)  # 4:00 p.m. ET
ORDER_TAG = os.environ.get("ORDER_TAG", "muse-avgo-ema")
BAR_MINUTES = 10


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


def get_orders(account):
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


def get_quote(symbol):
    """Latest last price for the symbol, or None when unavailable."""
    data = api_get("/v1/markets/quotes?symbols=%s&greeks=false" % symbol)
    quotes_obj = data.get("quotes")
    if not isinstance(quotes_obj, dict):
        return None
    quote = quotes_obj.get("quote")
    if isinstance(quote, list):
        quote = quote[0] if quote else None
    if not isinstance(quote, dict) or quote.get("last") is None:
        return None
    return float(quote["last"])


def get_bars():
    """Return [(bar_start_et, close)] for SYMBOL 10-minute bars,
    chronological, regular session only (09:30-16:00 ET), CLOSED bars
    only: a bar counts once its 10-minute window has ended, so the
    in-progress bar can never trigger a signal. Fetches the last 4
    calendar days so the EMAs are seeded even on a Monday (prior
    Friday's bars). Tradier treats `end` as exclusive, so request one
    day past today to include today's bars. Returns [] when the
    endpoint gives nothing usable."""
    now_et = datetime.now(ET)
    today = datetime.now(TZ).date()
    start = today - timedelta(days=4)
    end = today + timedelta(days=1)
    try:
        data = api_get(
            "/v1/markets/timesales?symbol=%s&interval=10min&start=%s&end=%s&session_filter=all"
            % (SYMBOL, start.isoformat(), end.isoformat())
        )
    except Exception as exc:  # noqa: BLE001 - no bars, no signal
        log("timesales failed (%s), no signal this poll" % exc)
        return []
    series = data.get("series", {})
    rows = series.get("data")
    if rows is None:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    out = []
    for row in rows:
        try:
            t = row.get("time", "")
            hhmm = t[11:16]
            if not ("09:30" <= hhmm <= "16:00"):
                continue
            bar_start = datetime.fromisoformat(t).replace(tzinfo=ET)
            # Closed only: the bar's 10-minute window must have ended.
            if now_et < bar_start + timedelta(minutes=BAR_MINUTES):
                continue
            out.append((bar_start, float(row["close"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


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


def ema_cross(closes, bar_dates, today_et):
    """Detect the EMA crossover on the last two closed bars.

    Returns (event, level, fresh) where event is "up" (fast crossed
    above slow on the last bar), "down" (fast crossed below slow), or
    None; level is "bull" when fast > slow on the last bar, "bear"
    when fast < slow, else None; fresh is True only when the crossing
    bar closed today (a restart after days offline must not enter on
    a stale signal). Needs at least EMA_SLOW + 1 closes to compare
    two EMA points."""
    n = len(closes)
    if n < EMA_SLOW + 1:
        return None, None, False
    fast = ema(closes, EMA_FAST)
    slow = ema(closes, EMA_SLOW)
    # fast[i] is the EMA at closes[i + EMA_FAST - 1];
    # slow[j] is the EMA at closes[j + EMA_SLOW - 1].
    # Compare at the same bar index k: fast[k - EMA_FAST + 1] vs
    # slow[k - EMA_SLOW + 1].
    def at(k):
        return fast[k - EMA_FAST + 1], slow[k - EMA_SLOW + 1]

    f_prev, s_prev = at(n - 2)
    f_last, s_last = at(n - 1)
    if f_last > s_last:
        level = "bull"
    elif f_last < s_last:
        level = "bear"
    else:
        level = None
    event = None
    if f_prev <= s_prev and f_last > s_last:
        event = "up"
    elif f_prev >= s_prev and f_last < s_last:
        event = "down"
    fresh = bool(bar_dates) and bar_dates[-1].date() == today_et
    return event, level, fresh


def get_pending_orders(account):
    """Working AVGO equity orders (any tag) plus any working order
    carrying this worker's tag. Used to block duplicate placements."""
    pending = []
    for order in get_orders(account):
        if order.get("status") not in ("pending", "open"):
            continue
        if order.get("tag") == ORDER_TAG:
            pending.append(order)
            continue
        # Equity orders carry the symbol directly (no option legs).
        if order.get("symbol") == SYMBOL and order.get("class") == "equity":
            pending.append(order)
    return pending


def has_blocking_pending_order(account):
    pending = get_pending_orders(account)
    if pending:
        return True, pending[0].get("id")
    return False, None


def place_equity_order(account, side, qty):
    """Place a market day order for SYMBOL. side is one of buy, sell,
    sell_short, buy_to_cover. Never places while a colliding order is
    working. Returns the API response dict (or a marker dict)."""
    blocked, blocking_id = has_blocking_pending_order(account)
    if blocked:
        log("SKIPPED %s %d %s: pending order %s already working"
            % (side, qty, SYMBOL, blocking_id))
        return {"blocked": True, "blocking_order_id": blocking_id}
    fields = {
        "class": "equity",
        "symbol": SYMBOL,
        "side": side,
        "quantity": str(int(qty)),
        "type": "market",
        "duration": "day",
        "tag": ORDER_TAG,
    }
    if not ENABLE_TRADING:
        log("DRY RUN: would place %s %d %s (market)"
            % (side, qty, SYMBOL))
        return {"dry_run": True}
    resp = api_post_order(account, fields)
    order = resp.get("order", resp)
    log("PLACED %s %d %s market, order id=%s"
        % (side, qty, SYMBOL, order.get("id")))
    return resp


def cancel_order(account, order_id):
    """Cancel one working order. Tradier cancels via DELETE."""
    req = urllib.request.Request(
        BASE + "/v1/accounts/%s/orders/%s" % (account, order_id), method="DELETE"
    )
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", "Bearer %s" % API_KEY)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def avgo_position(account):
    """The account's AVGO equity position, or None. Option positions
    (symbols starting with the weekly root) are ignored."""
    for pos in get_positions(account):
        if pos.get("symbol") == SYMBOL:
            try:
                qty = int(float(pos.get("quantity", 0)))
            except (TypeError, ValueError):
                continue
            if qty == 0:
                continue
            try:
                basis = abs(float(pos.get("cost_basis", 0))) / abs(qty)
            except (TypeError, ValueError, ZeroDivisionError):
                basis = None
            return {"qty": qty, "entry": basis}
    return None


def has_tagged_avgo_fill(account):
    """True when a filled AVGO equity order with this worker's tag
    exists in the orders feed. Used to tell our own position apart
    from a foreign AVGO position in the same account."""
    for order in get_orders(account):
        if order.get("status") != "filled":
            continue
        if order.get("tag") != ORDER_TAG:
            continue
        if order.get("symbol") == SYMBOL and order.get("class") == "equity":
            return True
    return False


def pnl_snapshot(account):
    try:
        bal = get_balances(account)
        log("P&L equity=%s realized=%s open_pl=%s"
            % (bal.get("total_equity"), bal.get("close_pl"),
               bal.get("open_pl")))
    except Exception as exc:  # noqa: BLE001 - snapshot is best-effort
        log("P&L snapshot failed (%s)" % exc)


def poll(state, account, now):
    """One evaluation pass. state holds the worker's tracked position
    and flags across polls."""
    # -- reconcile the position from the account ---------------------
    raw = avgo_position(account)
    tracked = state["pos"]
    if raw is None:
        if tracked is not None:
            log("position closed (was %s %d @ %.2f)"
                % (tracked["side"], tracked["qty"], tracked["entry"] or 0))
            state["pos"] = None
        state["foreign"] = False
    elif tracked is not None and abs(raw["qty"]) == tracked["qty"] \
            and (raw["qty"] > 0) == (tracked["side"] == "long"):
        # Same position we are already tracking; keep the entry price.
        pass
    elif abs(raw["qty"]) == QUANTITY and has_tagged_avgo_fill(account):
        side = "long" if raw["qty"] > 0 else "short"
        state["pos"] = {"side": side, "qty": abs(raw["qty"]),
                        "entry": raw["entry"]}
        state["foreign"] = False
        log("adopted own %s position %d @ %.2f"
            % (side, abs(raw["qty"]), raw["entry"] or 0))
    else:
        # A foreign AVGO position: never close it, never add to it.
        if not state["foreign"]:
            log("foreign AVGO position qty %s, leaving untouched; "
                "no entries while it exists" % raw["qty"])
        state["foreign"] = True
        state["pos"] = None

    if state["foreign"]:
        return

    # -- signal from closed 10-minute bars --------------------------
    bars = get_bars()
    today_et = datetime.now(ET).date()
    if len(bars) < EMA_SLOW + 1:
        log("only %d closed bars, need %d — no signal"
            % (len(bars), EMA_SLOW + 1))
        return
    closes = [c for _, c in bars]
    bar_dates = [t for t, _ in bars]
    event, level, fresh = ema_cross(closes, bar_dates, today_et)
    # An event lives on one bar until the next bar closes; remember
    # which bar's event this worker already acted on, so the same
    # crossover cannot re-trigger an entry after a stop-out.
    bar_key = bars[-1][0]
    if event is not None and state.get("seen_event_bar") == bar_key:
        event = None
    pos = state["pos"]

    # -- manage an open position ------------------------------------
    if pos is not None:
        # Opposite crossover closes first (it also queues the flip);
        # the stop is only checked when no crossover fired.
        if (pos["side"] == "long" and event == "down") or \
                (pos["side"] == "short" and event == "up"):
            new_side = "short" if pos["side"] == "long" else "long"
            side = "sell" if pos["side"] == "long" else "buy_to_cover"
            resp = place_equity_order(account, side, pos["qty"])
            if not resp.get("blocked") and not resp.get("dry_run"):
                state["pending_flip"] = new_side
                state["stopped_out"] = False
                state["seen_event_bar"] = bar_key
                log("opposite crossover: closing %s, flip to %s queued"
                    % (pos["side"], new_side))
            elif resp.get("dry_run"):
                state["pending_flip"] = new_side
                state["stopped_out"] = False
                state["seen_event_bar"] = bar_key
            return
        if pos["entry"]:
            last = get_quote(SYMBOL)
            if last is not None:
                if pos["side"] == "long" and last <= pos["entry"] * (1 - STOP_PCT):
                    side = "sell"
                    resp = place_equity_order(account, side, pos["qty"])
                    if not resp.get("blocked"):
                        state["stopped_out"] = True
                        state["pending_flip"] = None
                        state["seen_event_bar"] = bar_key
                        log("STOP %.2f <= entry %.2f * %.3f: closing long"
                            % (last, pos["entry"], 1 - STOP_PCT))
                    return
                if pos["side"] == "short" and last >= pos["entry"] * (1 + STOP_PCT):
                    side = "buy_to_cover"
                    resp = place_equity_order(account, side, pos["qty"])
                    if not resp.get("blocked"):
                        state["stopped_out"] = True
                        state["pending_flip"] = None
                        state["seen_event_bar"] = bar_key
                        log("STOP %.2f >= entry %.2f * %.3f: closing short"
                            % (last, pos["entry"], 1 + STOP_PCT))
                    return
        return

    # -- flat: entries ----------------------------------------------
    # A queued flip fires once the close has filled and the EMA level
    # still agrees with the flip direction.
    if state["pending_flip"] is not None:
        want = state["pending_flip"]
        level_ok = (want == "long" and level == "bull") or \
            (want == "short" and level == "bear")
        if level_ok:
            side = "buy" if want == "long" else "sell_short"
            resp = place_equity_order(account, side, QUANTITY)
            if not resp.get("blocked"):
                state["pending_flip"] = None
                state["stopped_out"] = False
                log("flip entry: %s %d %s" % (side, QUANTITY, SYMBOL))
            return
        if level is not None:
            log("flip to %s stale (level now %s), dropping it"
                % (want, level))
            state["pending_flip"] = None
        return

    # A stop-out blocks re-entry until the next fresh crossover.
    if state["stopped_out"]:
        if event is not None and fresh:
            state["stopped_out"] = False
            log("fresh crossover %s after stop-out, entries resume" % event)
        else:
            return

    if event is None or not fresh:
        return
    side = "buy" if event == "up" else "sell_short"
    resp = place_equity_order(account, side, QUANTITY)
    if not resp.get("blocked"):
        state["seen_event_bar"] = bar_key
        log("crossover %s: entering %s %d %s"
            % (event, side, QUANTITY, SYMBOL))


def run_once():
    if not API_KEY:
        log("TRADIER_API_KEY is not set, exiting")
        return 1
    account = resolve_account()
    log("account %s connected (PAPER host %s, trading %s, %s %d shares, "
        "EMA %d/%d on 10min, stop %.1f%%)"
        % (account, HOST, "ENABLED" if ENABLE_TRADING else "dry-run",
           SYMBOL, QUANTITY, EMA_FAST, EMA_SLOW, STOP_PCT * 100))
    state = {"pos": None, "pending_flip": None, "stopped_out": False,
             "foreign": False, "seen_event_bar": None}
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
                if now.hour == hour and last_pnl_day.get(hour) != today:
                    pnl_snapshot(account)
                    last_pnl_day[hour] = today
            in_window = MARKET_OPEN <= now.time() < MARKET_CLOSE
            if not in_window:
                log("outside market window, waiting")
                time.sleep(POLL_SECONDS)
                continue
            pos_txt = "flat"
            if state["pos"] is not None:
                pos_txt = "%s %d @ %.2f" % (
                    state["pos"]["side"], state["pos"]["qty"],
                    state["pos"]["entry"] or 0)
            elif state["foreign"]:
                pos_txt = "foreign AVGO held, no entries"
            elif state["pending_flip"]:
                pos_txt = "flat, flip to %s queued" % state["pending_flip"]
            elif state["stopped_out"]:
                pos_txt = "flat, stopped out, waiting for next cross"
            try:
                last_txt = "%.2f" % get_quote(SYMBOL)
            except Exception:
                last_txt = "?"
            log("heartbeat %s last=%s pos=%s" % (SYMBOL, last_txt, pos_txt))
            poll(state, account, now)
        except Exception as exc:  # noqa: BLE001 - never die on one poll
            log("poll error: %s" % exc)
        time.sleep(POLL_SECONDS)


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
