#!/usr/bin/env python3
"""AVGO 2-hour RSI(2) / EMA(100) stock trader for Railway (paper).

Implements one fixed rule set, nothing else (spec Nov 27, 2026;
replaces the EMA 14/45 10-minute crossover worker):

- Tradier PAPER host only (sandbox.tradier.com). The host is hardcoded
  and the program refuses to run against any other host, so it cannot
  touch a live account.
- Symbol AVGO (env SYMBOL), 100 shares per trade (env QUANTITY),
  one position at a time. Positions are held overnight.
- Bars are 2-hour, session-aligned: 09:30, 11:30, 13:30, 15:30 ET.
  The 15:30 bar is 30 minutes (market closes 16:00 ET). Indicators
  are calculated on the bar's close.
- Bar source: Tradier 30-minute bars (last 7 days, session_filter=
  open) aggregated into 2-hour bars. Tradier's 60-minute bars are
  clock-hour aligned (09:00, 10:00, ...) and do NOT fit this grid,
  so they are not used. Tradier intraday history only reaches back
  ~88 days (~248 2-hour bars), which is short of the 300-bar warm-up,
  so the history is seeded from Yahoo Finance 1-hour bars (2-year
  lookback, fetched at startup and refreshed daily) and merged with
  the Tradier bars; on a timestamp collision the Tradier bar wins.
  Yahoo and Tradier 2-hour closes agree to ~$0.01 on average (max
  $0.25) over the 248-bar overlap, so the seam is clean. If the
  Yahoo seed cannot be fetched, the worker runs Tradier-only and
  simply does not trade until 300 bars are available.
- Warm-up: no trading until bar 300 (bar 1 is the first bar in the
  merged history). Indicators are calculated from the first bar.
- EMA(100) of close: smoothing factor 2/101, seeded from the first
  close. RSI(2): change = close - previous close; average gain and
  average loss each Wilder-smoothed with factor 1/2, seeded from the
  first change; RSI = 100 - 100 / (1 + avgGain / avgLoss).
- Entry (decided when a bar closes, filled at the next bar's open,
  only when flat): long when the close is above EMA(100) and RSI(2)
  is below 10 (buy 100); short when the close is below EMA(100) and
  RSI(2) is above 90 (sell short 100). In practice the worker acts
  on the first poll after the bar closes (polls every 60s), which is
  within ~a minute of the next bar's open; a signal from the 15:30
  bar is acted on at the next morning's open. There is no stop loss
  and no take-profit.
- Exit (decided at a bar's close, filled at the next bar's open):
  long exits when RSI(2) is above 70 or the trade has been held 80
  bars or more; short exits (cover) when RSI(2) is below 30 or the
  trade has been held 80 bars or more. The entry bar counts as bar 1.
- Signals are evaluated on CLOSED bars only: a 2-hour bar counts
  once its window has ended (09:30 bar ends 11:30, 11:30 ends 13:30,
  13:30 ends 15:30, 15:30 ends 16:00 ET). A bar from a past date is
  closed by definition, which is what makes the 15:30-bar signal
  (and early-close days) act on the next morning's open. An acted-on
  bar is remembered, so the same signal can never place twice.
- Orders are market orders, day duration, tagged "muse-avgo-ema".
  Before placing, the worker skips if a working AVGO equity order
  already exists, so a duplicate entry is never placed.
- Adoption / do-not-touch: on every poll the worker reads the AVGO
  equity position. A position is treated as its own only when its
  quantity is exactly +/-QUANTITY and a filled AVGO equity order with
  this worker's tag exists for it. Any other AVGO position is foreign:
  it is never closed and no new entry is placed while it exists. An
  adopted position's entry bar is inferred from its tagged fill's
  timestamp so the 80-bar hold still works.

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

- Tradier sandbox quotes are delayed about 15 minutes, and a 2-hour
  bar's last 30-minute component may only appear in the feed after
  its window ends, so a live fill can land a few minutes into the
  next bar rather than exactly at its open. The backtest models
  fills at the next bar's open.
- Entry orders are day orders: an order left unfilled at the close
  expires with the session.

Backtest (Nov 27, 2026, Yahoo 1h bars Nov 26 2024 - Nov 25 2026,
1,989 2h bars, trading from bar 300): 62 trades, 66.1% win,
+$15,267 closed P&L, profit factor 2.08, max drawdown -$2,843,
avg hold 6.8 bars. All 62 exits were RSI exits; the 80-bar cap
never fired. See ~/workspace/your_files/avgo-2h-rsi-backtest-2026-11-27.md.

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
WARMUP_BARS = int(os.environ.get("WARMUP_BARS", "300"))
MAX_HOLD_BARS = 80
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
MARKET_OPEN = dtime(6, 30)   # 9:30 a.m. ET
MARKET_CLOSE = dtime(13, 0)  # 4:00 p.m. ET
ORDER_TAG = os.environ.get("ORDER_TAG", "muse-avgo-ema")

# RSI(2) / EMA(100) setup (fixed by the spec, not tunable by env).
EMA_PERIOD = 100
RSI_PERIOD = 2
RSI_LONG_ENTRY = 10.0
RSI_SHORT_ENTRY = 90.0
RSI_LONG_EXIT = 70.0
RSI_SHORT_EXIT = 30.0

YAHOO_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
             "%s?interval=1h&range=2y&includePrePost=false")


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


# ------------------------------------------------------------------ bars
def fetch_yahoo_1h():
    """Yahoo Finance 1-hour bars for SYMBOL (2-year lookback).
    Returns [(bar_start_et, open, high, low, close)] chronological,
    regular session only. Raises on any failure; the caller decides
    the fallback."""
    req = urllib.request.Request(YAHOO_URL % SYMBOL)
    req.add_header("User-Agent",
                   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    result = data["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    out = []
    for t, o, h, l, c in zip(result["timestamp"], quote["open"],
                            quote["high"], quote["low"], quote["close"]):
        if c is None:
            continue
        bar = datetime.fromtimestamp(t, tz=ET)
        hhmm = bar.strftime("%H:%M")
        if not ("09:30" <= hhmm <= "15:30"):
            continue
        out.append((bar, float(o), float(h), float(l), float(c)))
    return out


def get_tradier_30min():
    """Tradier 30-minute bars for SYMBOL, last 7 calendar days,
    regular session only. Returns [(bar_start_et, o, h, l, c)]."""
    today = datetime.now(TZ).date()
    start = today - timedelta(days=7)
    end = today + timedelta(days=1)
    try:
        data = api_get(
            "/v1/markets/timesales?symbol=%s&interval=30min&start=%s&end=%s&session_filter=open"
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
            bar = datetime.fromisoformat(t).replace(tzinfo=ET)
            out.append((bar, float(row["open"]), float(row["high"]),
                        float(row["low"]), float(row["close"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def aggregate_2h(bars):
    """Aggregate 1-hour or 30-minute bars into session-aligned
    2-hour bars: 09:30 <- 09:30..11:00, 11:30 <- 11:30..13:00,
    13:30 <- 13:30..15:00, 15:30 <- 15:30 alone. Returns a list of
    dicts {time, open, high, low, close} sorted by time. A group
    with a missing component still aggregates from what is there."""
    groups = {}
    for bar in bars:
        t = bar[0]
        hhmm = t.strftime("%H:%M")
        if hhmm in ("09:30", "10:00", "10:30", "11:00"):
            key = t.replace(hour=9, minute=30, second=0, microsecond=0)
        elif hhmm in ("11:30", "12:00", "12:30", "13:00"):
            key = t.replace(hour=11, minute=30, second=0, microsecond=0)
        elif hhmm in ("13:30", "14:00", "14:30", "15:00"):
            key = t.replace(hour=13, minute=30, second=0, microsecond=0)
        elif hhmm == "15:30":
            key = t.replace(hour=15, minute=30, second=0, microsecond=0)
        else:
            continue
        groups.setdefault(key, []).append(bar)
    out = []
    for key in sorted(groups):
        g = sorted(groups[key])
        out.append({"time": key, "open": g[0][1],
                    "high": max(b[2] for b in g),
                    "low": min(b[3] for b in g),
                    "close": g[-1][4]})
    return out


def bar_end(bar_start):
    """When a 2-hour bar's window ends (ET). The 15:30 bar is 30 min."""
    if bar_start.hour == 15 and bar_start.minute == 30:
        return bar_start + timedelta(minutes=30)
    return bar_start + timedelta(hours=2)


def merge_2h(seed_bars, live_bars):
    """Merge two 2-hour bar lists by start time; live_bars win on a
    timestamp collision. Returns a sorted, de-duplicated list."""
    by_time = {b["time"]: b for b in seed_bars}
    for b in live_bars:
        by_time[b["time"]] = b
    return [by_time[k] for k in sorted(by_time)]


def closed_bars(bars, now_et):
    """Bars whose window has ended, or whose date is before today
    (covers the 15:30 bar after the close and early-close days)."""
    today = now_et.date()
    return [b for b in bars
            if b["time"].date() < today or bar_end(b["time"]) <= now_et]


# ------------------------------------------------------------ indicators
def ema100(closes):
    """EMA(100) of close, k = 2/101, seeded from the first close."""
    if not closes:
        return []
    k = 2.0 / (EMA_PERIOD + 1)
    out = [closes[0]]
    for c in closes[1:]:
        out.append(c * k + out[-1] * (1 - k))
    return out


def _rsi_value(avg_gain, avg_loss):
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1 + avg_gain / avg_loss)


def rsi2(closes):
    """RSI(2): avg gain / avg loss each Wilder-smoothed with factor
    1/2, seeded from the first change (bar 2). Index 0 is None (no
    previous close)."""
    n = len(closes)
    out = [None] * n
    if n < 2:
        return out
    avg_gain = max(closes[1] - closes[0], 0.0)
    avg_loss = max(closes[0] - closes[1], 0.0)
    out[1] = _rsi_value(avg_gain, avg_loss)
    for i in range(2, n):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain + max(change, 0.0)) / 2
        avg_loss = (avg_loss + max(-change, 0.0)) / 2
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


# ---------------------------------------------------------------- orders
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


def last_tagged_fill_time(account):
    """Create time of the most recent filled tagged AVGO equity order,
    as an aware ET datetime, or None. Used to infer the entry bar of
    an adopted position."""
    latest = None
    for order in get_orders(account):
        if order.get("status") != "filled":
            continue
        if order.get("tag") != ORDER_TAG:
            continue
        if order.get("symbol") != SYMBOL or order.get("class") != "equity":
            continue
        raw = order.get("create_date") or order.get("transaction_date")
        if not raw:
            continue
        try:
            t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=ET)
        t = t.astimezone(ET)
        if latest is None or t > latest:
            latest = t
    return latest


def infer_entry_signal_bar(bars, fill_time):
    """Index of the entry signal bar for a position filled at
    fill_time: the fill happens at the open of some bar, and the
    signal bar is the bar before it. Returns None when it cannot be
    determined."""
    if fill_time is None:
        return None
    for idx, bar in enumerate(bars):
        if bar["time"] <= fill_time < bar_end(bar["time"]):
            return idx - 1 if idx > 0 else 0
    return None


def pnl_snapshot(account):
    try:
        bal = get_balances(account)
        log("P&L equity=%s realized=%s open_pl=%s"
            % (bal.get("total_equity"), bal.get("close_pl"),
               bal.get("open_pl")))
    except Exception as exc:  # noqa: BLE001 - snapshot is best-effort
        log("P&L snapshot failed (%s)" % exc)


def refresh_yahoo_seed(state, force=False):
    """(Re)fetch the Yahoo 1h seed and store its 2h bars in state.
    Best-effort: on failure the previous seed is kept (or an empty
    seed when there never was one). A failed fetch is retried at most
    once an hour, so a Yahoo outage never turns into a retry storm.
    Returns True when a seed is available afterwards."""
    today = datetime.now(TZ).date()
    if not force and state.get("yahoo_date") == today \
            and state.get("yahoo_2h"):
        return True
    if not force and state.get("yahoo_next_retry") is not None \
            and datetime.now(TZ) < state["yahoo_next_retry"]:
        return bool(state.get("yahoo_2h"))
    try:
        bars_1h = fetch_yahoo_1h()
    except Exception as exc:  # noqa: BLE001 - seed is best-effort
        state["yahoo_next_retry"] = datetime.now(TZ) + timedelta(hours=1)
        log("Yahoo seed fetch failed (%s); %s"
            % (exc, "keeping previous seed"
               if state.get("yahoo_2h") else "running Tradier-only"))
        return bool(state.get("yahoo_2h"))
    seed_2h = aggregate_2h(bars_1h)
    state["yahoo_2h"] = seed_2h
    state["yahoo_date"] = today
    state["yahoo_next_retry"] = None
    log("Yahoo seed refreshed: %d 2h bars, %s..%s"
        % (len(seed_2h), seed_2h[0]["time"].date() if seed_2h else "-",
           seed_2h[-1]["time"].date() if seed_2h else "-"))
    return True


def get_signal_bars(state):
    """Merged, closed 2-hour bars: Yahoo seed + Tradier 30-min bars
    (Tradier wins on collision). Returns [] when the Tradier read
    fails."""
    live_30 = get_tradier_30min()
    if not live_30 and not state.get("yahoo_2h"):
        return []
    live_2h = aggregate_2h(live_30)
    merged = merge_2h(state.get("yahoo_2h") or [], live_2h)
    now_et = datetime.now(ET)
    return closed_bars(merged, now_et)


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
        # Same position we are already tracking; keep the entry price
        # and the entry signal bar.
        pass
    elif abs(raw["qty"]) == QUANTITY and has_tagged_avgo_fill(account):
        side = "long" if raw["qty"] > 0 else "short"
        state["pos"] = {"side": side, "qty": abs(raw["qty"]),
                        "entry": raw["entry"], "signal_bar": None}
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

    # -- signal from closed 2-hour bars ------------------------------
    bars = get_signal_bars(state)
    if len(bars) < WARMUP_BARS:
        log("only %d closed 2h bars, need %d (warm-up) — no signal"
            % (len(bars), WARMUP_BARS))
        return
    closes = [b["close"] for b in bars]
    ema = ema100(closes)
    rsi = rsi2(closes)
    i = len(bars) - 1  # last closed bar
    bar_key = bars[i]["time"]
    # Staleness guard: the last closed bar should be today or, on a
    # Monday / after a holiday, Friday's. Anything older means both
    # the Tradier read and the Yahoo seed are stale — do not trade
    # on it.
    if (datetime.now(ET).date() - bar_key.date()).days > 4:
        log("last closed 2h bar %s is stale, no signal"
            % bar_key.strftime("%Y-%m-%d %H:%M"))
        return
    if state.get("seen_bar") == bar_key:
        return  # already acted on this bar's signal
    pos = state["pos"]

    # -- manage an open position ------------------------------------
    if pos is not None:
        if pos.get("signal_bar") is None:
            # Adopted position: infer the entry signal bar from the
            # tagged fill's timestamp; fall back to "entered at the
            # last bar's open" (held = 1) so the 80-bar exit fires
            # late rather than early. Clamp to the bar before the
            # last closed bar so a bad fill timestamp can never make
            # the hold negative.
            fill_time = last_tagged_fill_time(account)
            sig = infer_entry_signal_bar(bars, fill_time)
            pos["signal_bar"] = sig if sig is not None else i - 1
            if pos["signal_bar"] >= i:
                pos["signal_bar"] = i - 1
            log("adopted position entry signal bar %d (fill %s)"
                % (pos["signal_bar"], fill_time))
        held = i - pos["signal_bar"]
        rsi_now = rsi[i]
        exit_now = False
        reason = ""
        if pos["side"] == "long":
            if rsi_now is not None and rsi_now > RSI_LONG_EXIT:
                exit_now, reason = True, "RSI(2) %.1f > %.0f" % (
                    rsi_now, RSI_LONG_EXIT)
            elif held >= MAX_HOLD_BARS:
                exit_now, reason = True, "held %d bars" % held
        else:
            if rsi_now is not None and rsi_now < RSI_SHORT_EXIT:
                exit_now, reason = True, "RSI(2) %.1f < %.0f" % (
                    rsi_now, RSI_SHORT_EXIT)
            elif held >= MAX_HOLD_BARS:
                exit_now, reason = True, "held %d bars" % held
        if exit_now:
            side = "sell" if pos["side"] == "long" else "buy_to_cover"
            resp = place_equity_order(account, side, pos["qty"])
            if not resp.get("blocked"):
                state["seen_bar"] = bar_key
                log("exit %s: %s (close %.2f, EMA100 %.2f, RSI(2) %s)"
                    % (pos["side"], reason, closes[i], ema[i],
                       "%.1f" % rsi_now if rsi_now is not None else "-"))
        return

    # -- flat: entries ----------------------------------------------
    side = None
    reason = ""
    if closes[i] > ema[i] and rsi[i] is not None \
            and rsi[i] < RSI_LONG_ENTRY:
        side, reason = "buy", "close %.2f > EMA100 %.2f, RSI(2) %.1f < %.0f" % (
            closes[i], ema[i], rsi[i], RSI_LONG_ENTRY)
    elif closes[i] < ema[i] and rsi[i] is not None \
            and rsi[i] > RSI_SHORT_ENTRY:
        side, reason = "sell_short", "close %.2f < EMA100 %.2f, RSI(2) %.1f > %.0f" % (
            closes[i], ema[i], rsi[i], RSI_SHORT_ENTRY)
    if side is None:
        return
    resp = place_equity_order(account, side, QUANTITY)
    if not resp.get("blocked"):
        state["seen_bar"] = bar_key
        log("entry %s: %s" % (side, reason))


def run_once():
    if not API_KEY:
        log("TRADIER_API_KEY is not set, exiting")
        return 1
    account = resolve_account()
    log("account %s connected (PAPER host %s, trading %s, %s %d shares, "
        "2h RSI(2)/EMA(100), warm-up %d bars)"
        % (account, HOST, "ENABLED" if ENABLE_TRADING else "dry-run",
           SYMBOL, QUANTITY, WARMUP_BARS))
    state = {"pos": None, "foreign": False, "seen_bar": None,
             "yahoo_2h": [], "yahoo_date": None}
    refresh_yahoo_seed(state, force=True)
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
            # Refresh the Yahoo seed once a day so the merged history
            # never develops a gap behind Tradier's rolling window.
            refresh_yahoo_seed(state)
            pos_txt = "flat"
            if state["pos"] is not None:
                pos_txt = "%s %d @ %.2f" % (
                    state["pos"]["side"], state["pos"]["qty"],
                    state["pos"]["entry"] or 0)
            elif state["foreign"]:
                pos_txt = "foreign AVGO held, no entries"
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
