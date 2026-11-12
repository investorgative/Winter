#!/usr/bin/env python3
"""
WINTER - Kalshi weather favorite-bias desk (paper + demo), backtester and forward tester in one.

Strategy (chosen on May-Aug 2026, confirmed on Sep-Nov 2026 data):
  The day before (15:00 local in each city) find the market favorite of tomorrow's Kalshi
  daily-high event (bracket with the highest mid). Buy YES if its ask is 40-80c and the spread
  is <= 2c. No weather model, no nowcast.

Books:
  taker  buys at the ask (forward: walks the real orderbook, never above the decision ask)
  maker  rests a limit bid at the best bid until 09:00 local on the target day
  demo   optional: mirrors every taker trade as a real order on the Kalshi DEMO exchange
         (fake money, prices differ from the real market -> tests order handling, not the edge)

Commands (from the project folder, with the venv python):
  ./venv/bin/python winter.py                 forward tester + website (port 8752) + Cloudflare tunnel
  ./venv/bin/python winter.py backtest        backtest the last 180 days (--days, --start, --end, --cities)
  ./venv/bin/python winter.py demo-check      test the Kalshi DEMO login, place + cancel one order
  ./venv/bin/python winter.py tunnel-check    diagnose the public Cloudflare link
  ./venv/bin/python winter.py demo-connect --key-id ID < key.pem   connect the Kalshi demo key
  ./venv/bin/python winter.py passcode-check  show where the passcode comes from (never prints it)

All personal settings live in config.json (created from config.example.json by setup.sh).
"""
import requests, json, math, csv, os, sys, time, hashlib, random, argparse, threading, functools, traceback, statistics, re
import datetime as dt
import secrets, hmac, signal, shutil, subprocess, atexit, mimetypes
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))

# ---------- CONFIG (config.json overrides these defaults) ----------
DEFAULTS = {
    "port": 8752,
    "passcode": "",                  # set by setup.sh into config.json (never committed to git)
    "autolock_minutes": 10,          # website locks itself after this much inactivity
    "tunnel": True,                  # start a free Cloudflare quick tunnel (needs cloudflared)
    "start_bankroll": 1000.0,        # per book
    "stake_frac": 0.02,              # per trade, share of the book's bankroll (~quarter Kelly)
    "max_stake_usd": 50.0,           # hard cap per trade (cost incl. fee) - orderbooks are thin
    "max_daily": 0.15,               # per book and Berlin day
    "maker_fee_rate": 0.0175,        # conservative 25% of taker; 0.0 if the series has no maker fee
    "edge_assumed": 0.0545,          # backtest mean per contract after fees, for the capacity estimate
    "demo": {"enabled": False, "key_id": "", "key_file": "kalshi_demo.pem"},
}

def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    path = os.path.join(BASE, "config.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                user = json.load(f)
        except Exception as e:
            print(f"config.json is not valid JSON ({e}) - using defaults", flush=True)
            user = {}
        for k, v in user.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    if os.environ.get("WINTER_PASSCODE"):
        cfg["passcode"] = os.environ["WINTER_PASSCODE"]
    pc = str(cfg.get("passcode") or "")
    if not (pc.isdigit() and 4 <= len(pc) <= 8):
        pc = "".join(secrets.choice("0123456789") for _ in range(4))
        cfg["passcode"] = pc
        try:                                 # store it so it stays the same after a restart
            stored = {}
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    stored = json.load(f)
            stored["passcode"] = pc
            with open(path, "w", encoding="utf-8") as f:
                json.dump(stored, f, indent=2)
        except Exception:
            pass
        print(f"\n*** No valid passcode in config.json - generated one: {pc} (change it in config.json) ***\n", flush=True)
    return cfg

CONF = load_config()

# ---------- SETTINGS ----------
VERSION = "winter-1.0"
PORT = int(CONF["port"])
PASSCODE = str(CONF["passcode"])
AUTOLOCK_MIN = float(CONF["autolock_minutes"])
TUNNEL_ON = bool(CONF["tunnel"])
TZ_NAME = "Europe/Berlin"
START_BANKROLL = float(CONF["start_bankroll"])
DECISION_LOCAL = (15, 0)         # local time in each city, the day BEFORE the target day
DECISION_LATEST_HOUR = 18        # a missed decision is not taken after this local hour
MAKER_EXPIRY_LOCAL = (9, 0)      # maker order expires at this local time ON the target day
FAV_MIN, FAV_MAX = 0.40, 0.80    # allowed ask of the favorite (validated rule - do not tune)
MAX_SPREAD = 0.02                # validated rule - do not tune
STAKE_FRAC = float(CONF["stake_frac"])
MAX_STAKE_USD = float(CONF["max_stake_usd"])
EDGE_ASSUMED = float(CONF["edge_assumed"])
MAX_DAILY = float(CONF["max_daily"])
TAKER_FEE_RATE = 0.07            # ceil(rate * C * P * (1-P)), Kalshi general schedule
MAKER_FEE_RATE = float(CONF["maker_fee_rate"])
MAKER_CHECK_SEC = 300
SETTLE_EVERY_MIN = 60
TRAIN_SHARE = 0.6                # backtest: first 60% of days = "train", rest = "test"
BOOT = 2000
CITIES = {  # city: (Kalshi series, timezone)
    "New York":     ("KXHIGHNY",   "America/New_York"),
    "Chicago":      ("KXHIGHCHI",  "America/Chicago"),
    "Miami":        ("KXHIGHMIA",  "America/New_York"),
    "Austin":       ("KXHIGHAUS",  "America/Chicago"),
    "Los Angeles":  ("KXHIGHLAX",  "America/Los_Angeles"),
    "Denver":       ("KXHIGHDEN",  "America/Denver"),
    "Philadelphia": ("KXHIGHPHIL", "America/New_York"),
}
API = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_ENABLED = bool(CONF["demo"].get("enabled"))
DEMO_KEY_ID = str(CONF["demo"].get("key_id") or "")
DEFAULT_KEY_FILE = "kalshi_demo.pem"

def _sane_key_file(v):
    """key_file must be a plain file name/path ending in .pem/.key - anything else (e.g. a pasted key) -> default."""
    v = str(v or "").strip()
    if v and len(v) <= 200 and re.fullmatch(r"[\w./~-]+", v) and v.endswith((".pem", ".key")) \
            and (not os.path.isabs(v) or os.path.isdir(os.path.dirname(v))):
        return v
    if v:
        print(f'config.json: demo.key_file is not a file name - using {DEFAULT_KEY_FILE} instead', flush=True)
    return DEFAULT_KEY_FILE

DEMO_KEY_FILE = _sane_key_file(CONF["demo"].get("key_file"))
DEMO_API = "https://external-api.demo.kalshi.co/trade-api/v2"
# ------------------------------

DATA = os.path.join(BASE, "data")
LOGS = os.path.join(DATA, "logs")
CACHE = os.path.join(DATA, "cache")
STATIC = os.path.join(BASE, "static")
RUNLOG = os.path.join(LOGS, "winter.log")
CF_LOG = os.path.join(LOGS, "cloudflared.log")
CF_PID = os.path.join(DATA, "cloudflared.pid")
F_TRADES = os.path.join(DATA, "forward_trades.csv")
F_DECISIONS = os.path.join(DATA, "forward_decisions.csv")
F_SNAP = os.path.join(DATA, "forward_snapshots.csv")
F_STATE = os.path.join(DATA, "state.json")
B_TRADES = os.path.join(DATA, "backtest_trades.csv")
B_DECISIONS = os.path.join(DATA, "backtest_decisions.csv")
B_SUMMARY = os.path.join(DATA, "backtest.json")
F_DEPTH = os.path.join(DATA, "forward_depth.csv")
TZ = ZoneInfo(TZ_NAME)
UTC = dt.timezone.utc
BOOKS = ("taker", "maker")                        # backtest books
FWD_BOOKS = BOOKS + (("demo",) if DEMO_ENABLED else ())
TRADE_FIELDS = ["book", "logged", "city", "date", "ticker", "bracket", "side", "status",
                "limit", "price", "contracts", "fee", "decision_bid", "decision_ask", "oi",
                "placed_ts", "expiry_ts", "filled", "fill_src", "result", "win", "pnl"]
DEC_FIELDS = ["logged", "city", "date", "ticker", "bracket", "bid", "ask", "mid", "spread",
              "n_brackets", "action", "result"]
SNAP_FIELDS = ["logged", "city", "date", "ticker", "bracket", "bid", "ask", "oi"]
DEPTH_FIELDS = ["logged", "city", "date", "ticker", "ask", "oi", "at_ask", "within_1c", "within_2c",
                "within_3c", "cap_contracts", "cap_cost", "cap_profit", "ladder"]
MON = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
LOCK = threading.Lock()


def say(*a):
    line = dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True)
    try:
        with open(RUNLOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ---------- parsing / fees ----------
def to_dollars(v):
    """Kalshi price in any format -> dollars. int = cents, decimal = dollars, digit string = cents."""
    if v in (None, ""):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return x / 100
    if isinstance(v, float) or (isinstance(v, str) and "." in v):
        return x if x <= 1.0 else x / 100
    return x / 100

def px(obj, key):
    """Price from a market object or candle OHLC block: prefers '<key>_dollars'."""
    if not obj:
        return None
    v = obj.get(key + "_dollars")
    if v not in (None, ""):
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    return to_dollars(obj.get(key))

def num(obj, *keys):
    for k in keys:
        try:
            if obj.get(k) not in (None, ""):
                return float(obj[k])
        except Exception:
            pass
    return 0.0

def fee(n, p, rate):
    if rate <= 0 or n <= 0:
        return 0.0
    return math.ceil(round(rate * n * p * (1 - p) * 100, 6)) / 100

def size(bank, p, rate):
    per = p + rate * p * (1 - p)
    return int(min(STAKE_FRAC * bank, MAX_STAKE_USD) / per)

def target_date(ticker):
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", ticker or "")
    return dt.date(2000 + int(m.group(1)), MON[m.group(2)], int(m.group(3))) if m else None

def label(m):
    return m.get("yes_sub_title") or m.get("subtitle") or m.get("title") or m.get("ticker", "")

def decision_time(city, target):
    tz = ZoneInfo(CITIES[city][1])
    return dt.datetime.combine(target, dt.time(*DECISION_LOCAL), tz) - dt.timedelta(days=1)

def maker_expiry(city, target):
    return dt.datetime.combine(target, dt.time(*MAKER_EXPIRY_LOCAL), ZoneInfo(CITIES[city][1]))

# ---------- THE STRATEGY (shared by backtest and forward) ----------
def decide(brackets):
    """brackets: [{ticker, bracket, bid, ask, oi}] of one event. Returns (favorite or None, action)."""
    valid = [b for b in brackets if b["ask"] and b["ask"] > 0 and b["bid"] is not None]
    if len(valid) < 4:
        return None, "SKIP incomplete event"
    fav = max(valid, key=lambda b: (b["bid"] + b["ask"]) / 2)
    if not FAV_MIN <= fav["ask"] <= FAV_MAX:
        return fav, f"SKIP ask {fav['ask']*100:.0f}c outside {FAV_MIN*100:.0f}-{FAV_MAX*100:.0f}c"
    if fav["ask"] - fav["bid"] > MAX_SPREAD + 1e-9:
        return fav, f"SKIP spread {(fav['ask']-fav['bid'])*100:.0f}c > {MAX_SPREAD*100:.0f}c"
    return fav, "TRADE"

def maker_filled(candles, limit, after_ts, until_ts):
    """True if, after placement, the ask touched our bid or a trade printed below it."""
    for c in candles or []:
        end = c.get("end_period_ts", 0)
        if end <= after_ts or end > until_ts + 60:
            continue
        ask_low = px(c.get("yes_ask"), "low")
        trade_low = px(c.get("price"), "low")
        if (ask_low is not None and 0 < ask_low <= limit + 1e-9) or \
           (trade_low is not None and 0 < trade_low < limit - 1e-9):
            return True
    return False

# ---------- HTTP ----------
def get_cached(url, params=None, ttl_days=None, tries=4):
    key = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
    path = os.path.join(CACHE, key + ".json")
    if os.path.exists(path) and (ttl_days is None or time.time() - os.path.getmtime(path) < ttl_days * 86400):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    data = get_live(url, params, tries)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data

def get_live(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=30,
                             headers={"User-Agent": "weather-fav-paper"})
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                time.sleep(2 + 2 * i)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + i)
    return None

# ---------- storage ----------
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))

def write_csv(path, fields, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)

def append_csv(path, fields, rows):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", restval="")
        if new:
            w.writeheader()
        w.writerows(rows)

def write_json(path, obj):
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(path + ".tmp", path)

def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

# ---------- statistics ----------
def book_stats(trades, start_bank=START_BANKROLL):
    """trades: settled fills with keys date, price, contracts, win, pnl."""
    if not trades:
        return {"n": 0}
    pc = [t["pnl"] / t["contracts"] for t in trades]
    m = statistics.mean(pc)
    sd = statistics.stdev(pc) if len(pc) > 1 else 0
    days = {}
    for t, x in zip(trades, pc):
        days.setdefault(t["date"], []).append(x)
    ci = None
    if len(days) >= 5:
        keys, rnd, ms = list(days), random.Random(7), []
        for _ in range(BOOT):
            xs = [v for k in (rnd.choice(keys) for _ in keys) for v in days[k]]
            ms.append(sum(xs) / len(xs))
        ms.sort()
        ci = [ms[int(.025 * BOOT)], ms[int(.975 * BOOT)]]
    dist = [1.0]
    for t in trades:
        p = t["price"]
        nd = [0.0] * (len(dist) + 1)
        for k, v in enumerate(dist):
            nd[k] += v * (1 - p)
            nd[k + 1] += v * p
        dist = nd
    wins = sum(int(t["win"]) for t in trades)
    per_day = {}
    for t in trades:
        per_day[t["date"]] = per_day.get(t["date"], 0) + t["pnl"]
    eq, run, peak, mdd = [], start_bank, start_bank, 0.0
    for d in sorted(per_day):
        run += per_day[d]
        peak = max(peak, run)
        mdd = max(mdd, peak - run)
        eq.append({"d": d, "v": round(run, 2)})
    months = {}
    for t, x in zip(trades, pc):
        months.setdefault(t["date"][:7], []).append(x)
    return {"n": len(trades), "wins": wins, "exp_wins": round(sum(t["price"] for t in trades), 1),
            "mean": m, "t": m / (sd / math.sqrt(len(pc))) if sd else None, "ci": ci,
            "luck_p": sum(dist[wins:]), "pnl": round(sum(t["pnl"] for t in trades), 2),
            "fees": round(sum(t["fee"] for t in trades), 2), "mdd": round(mdd, 2),
            "staked": round(sum(t["contracts"] * t["price"] for t in trades), 2),
            "equity": eq, "months": [{"m": k, "mean": statistics.mean(v), "n": len(v)}
                                     for k, v in sorted(months.items())]}

def verdict(s):
    if s.get("n", 0) < 30:
        return "TOO FEW TRADES"
    if s["ci"] and s["ci"][0] > 0:
        return "EDGE LIKELY"
    if s["ci"] and s["ci"][1] < 0:
        return "LOSES"
    return "NOT PROVEN"

# =====================================================================
# BACKTEST
# =====================================================================
def bt_markets(series, start, end):
    out = {}
    for path in ("/markets", "/historical/markets"):
        cursor, pages = None, 0
        while True:
            params = {"series_ticker": series, "limit": 200}
            if path == "/markets":
                params["status"] = "settled"
            if cursor:
                params["cursor"] = cursor
            try:
                r = get_cached(API + path, params, ttl_days=1) or {}
            except Exception as e:
                say("  market list failed", path, e)
                break
            for m in r.get("markets", []):
                d = target_date(m.get("ticker"))
                if d and start <= d <= end and m.get("result") in ("yes", "no"):
                    m["_hist"] = path != "/markets"
                    out.setdefault(m["ticker"], m)
            cursor = r.get("cursor")
            pages += 1
            ds = [target_date(m.get("ticker")) for m in r.get("markets", [])]
            ds = [d for d in ds if d]
            if not cursor or pages > 200 or (ds and max(ds) < start):
                break
            time.sleep(0.05)
    return list(out.values())

def bt_candles(series, m, start_ts, end_ts, interval=60):
    paths = [f"{API}/series/{series}/markets/{m['ticker']}/candlesticks",
             f"{API}/historical/markets/{m['ticker']}/candlesticks"]
    if m.get("_hist"):
        paths.reverse()
    params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": interval}
    for url in paths:
        try:
            r = get_cached(url, params)
        except Exception:
            r = None
        cs = (r or {}).get("candlesticks") or []
        if cs:
            return cs
    return []

def bt_quote(series, m, t):
    end_ts = int(t.timestamp())
    cs = bt_candles(series, m, end_ts - 6 * 3600, end_ts)   # same window as weather_backtest.py
    for c in reversed([c for c in cs if c.get("end_period_ts", 0) <= end_ts]):
        yb, ya = px(c.get("yes_bid"), "close"), px(c.get("yes_ask"), "close")
        if ya is None or yb is None or ya <= 0:
            continue
        return yb, ya, num(c, "open_interest", "open_interest_fp")
    return None

def run_backtest(start, end, cities):
    trades, decisions = [], []
    for city in cities:
        series, tz = CITIES[city]
        say(f"{city}: loading settled markets ...")
        markets = bt_markets(series, start, end)
        by_day = {}
        for m in markets:
            by_day.setdefault(target_date(m["ticker"]), []).append(m)
        say(f"{city}: {len(markets)} markets on {len(by_day)} days")
        for i, (d, ms) in enumerate(sorted(by_day.items())):
            t_dec = decision_time(city, d)
            brackets = []
            for m in ms:
                q = bt_quote(series, m, t_dec)
                if q:
                    brackets.append({"ticker": m["ticker"], "bracket": label(m), "bid": q[0],
                                     "ask": q[1], "oi": q[2], "y": 1 if m["result"] == "yes" else 0,
                                     "_m": m})
            fav, action = decide(brackets)
            decisions.append({"logged": t_dec.strftime("%Y-%m-%d %H:%M"), "city": city,
                              "date": d.isoformat(), "ticker": fav["ticker"] if fav else "",
                              "bracket": fav["bracket"] if fav else "",
                              "bid": fav["bid"] if fav else "", "ask": fav["ask"] if fav else "",
                              "mid": round((fav["bid"] + fav["ask"]) / 2, 4) if fav else "",
                              "spread": round(fav["ask"] - fav["bid"], 4) if fav else "",
                              "n_brackets": len(brackets), "action": action,
                              "result": ("yes" if fav["y"] else "no") if fav else ""})
            if action != "TRADE":
                continue
            win = fav["y"] == 1
            # taker
            p = fav["ask"]
            n = max(1, size(START_BANKROLL, p, TAKER_FEE_RATE))
            f = fee(n, p, TAKER_FEE_RATE)
            trades.append(bt_row("taker", city, d, fav, p, n, f, win, "filled", t_dec))
            # maker
            lim = fav["bid"]
            if lim >= 0.01:
                exp = maker_expiry(city, d)
                cs = bt_candles(series, fav["_m"], int(t_dec.timestamp()), int(exp.timestamp()))
                if maker_filled(cs, lim, int(t_dec.timestamp()), int(exp.timestamp())):
                    n = max(1, size(START_BANKROLL, lim, MAKER_FEE_RATE))
                    f = fee(n, lim, MAKER_FEE_RATE)
                    trades.append(bt_row("maker", city, d, fav, lim, n, f, win, "filled", t_dec))
                else:
                    trades.append(bt_row("maker", city, d, fav, lim, 0, 0.0, win, "unfilled", t_dec))
            if i % 20 == 0:
                say(f"  {city} {d}  decisions {len(decisions)}  trades {len(trades)}")
    return trades, decisions

def bt_row(book, city, d, fav, price, n, f, win, status, t_dec):
    pnl = ((n * (1 - price) if win else -n * price) - f) if n else 0.0
    return {"book": book, "logged": t_dec.strftime("%Y-%m-%d %H:%M"), "city": city,
            "date": d.isoformat(), "ticker": fav["ticker"], "bracket": fav["bracket"], "side": "YES",
            "status": "settled" if n else "unfilled", "limit": price, "price": price, "contracts": n,
            "fee": f, "decision_bid": fav["bid"], "decision_ask": fav["ask"], "oi": fav["oi"],
            "filled": status, "fill_src": "candle", "result": "yes" if fav["y"] else "no",
            "win": int(win) if n else "", "pnl": round(pnl, 2)}

def summarize_backtest(trades, decisions, start, end):
    days = sorted({t["date"] for t in decisions})
    split = days[int(len(days) * TRAIN_SHARE)] if days else ""
    books = {}
    for b in BOOKS:
        rows = [t for t in trades if t["book"] == b]
        filled = [t for t in rows if t["contracts"]]
        s = book_stats(filled)
        s["train"] = book_stats([t for t in filled if t["date"] < split])
        s["test"] = book_stats([t for t in filled if t["date"] >= split])
        for k in ("train", "test"):
            s[k] = {x: s[k].get(x) for x in ("n", "mean", "t", "ci", "pnl")}
        s["orders"] = len(rows)
        s["fill_rate"] = len(filled) / len(rows) if rows else None
        s["verdict"] = verdict(s)
        books[b] = s
    actions = {}
    for d in decisions:
        k = d["action"].split(" ")[0] if d["action"] == "TRADE" else d["action"].split(" ")[1]
        actions[k] = actions.get(k, 0) + 1
    return {"generated": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"), "start": start.isoformat(),
            "end": end.isoformat(), "split": split, "decisions": len(decisions), "actions": actions,
            "books": books}

def print_backtest(sm):
    print("\n" + "=" * 76)
    print(f"BACKTEST {sm['start']} .. {sm['end']}   train < {sm['split']} <= test")
    print("=" * 76)
    print("decisions:", sm["decisions"], " ", sm["actions"])
    for b, s in sm["books"].items():
        if not s.get("n"):
            print(f"  {b}: no fills")
            continue
        ci = "n/a" if not s["ci"] else f"[{s['ci'][0]*100:+.1f}, {s['ci'][1]*100:+.1f}]c"
        fr = "" if s["fill_rate"] is None else f" fill rate {s['fill_rate']:.0%}"
        f = lambda x: "  n/a" if not x or x.get("mean") is None else f"{x['mean']*100:+.1f}c (n={x['n']})"
        print(f"  {b:<6} n={s['n']} wins {s['wins']} (price-implied {s['exp_wins']})  "
              f"mean {s['mean']*100:+.2f}c/contract  t {s['t'] or 0:+.2f}  CI {ci}{fr}")
        print(f"         train {f(s['train'])}  test {f(s['test'])}  P&L ${s['pnl']:+.2f} "
              f"(fees ${s['fees']:.2f}, max DD ${s['mdd']:.2f})  -> {s['verdict']}")
    print()

def cmd_backtest(argv):
    ap = argparse.ArgumentParser(prog="winter.py backtest")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--cities", default=",".join(CITIES))
    a = ap.parse_args(argv)
    end = dt.date.fromisoformat(a.end) if a.end else dt.date.today() - dt.timedelta(days=1)
    start = dt.date.fromisoformat(a.start) if a.start else end - dt.timedelta(days=a.days - 1)
    cities = [c.strip() for c in a.cities.split(",") if c.strip() in CITIES]
    say(f"Backtest {start} .. {end}: {', '.join(cities)}")
    trades, decisions = run_backtest(start, end, cities)
    if not decisions:
        say("No data. Check internet access and dates.")
        return
    write_csv(B_TRADES, TRADE_FIELDS, trades)
    write_csv(B_DECISIONS, DEC_FIELDS, decisions)
    sm = summarize_backtest(trades, decisions, start, end)
    write_json(B_SUMMARY, sm)
    print_backtest(sm)
    say(f"Saved {B_SUMMARY}. It appears on the website within a minute (section Backtest).")

# =====================================================================
# FORWARD TESTER
# =====================================================================
def live_markets(series):
    out, cursor = [], None
    while True:
        params = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = get_live(API + "/markets", params) or {}
        out += r.get("markets", [])
        cursor = r.get("cursor")
        if not cursor:
            return out

def orderbook_asks(ticker):
    """YES ask ladder [(price, qty)] best first, from the NO bids."""
    r = get_live(f"{API}/markets/{ticker}/orderbook") or {}
    ob = r.get("orderbook_fp") or r.get("orderbook") or {}
    levels = []
    for p, q in ob.get("no_dollars") or []:
        levels.append((round(1 - float(p), 4), float(q)))
    if not levels:
        for p, q in ob.get("no") or []:
            levels.append((round(1 - to_dollars(p), 4), float(q)))
    return sorted(levels)

def fetch_levels(ticker):
    try:
        return orderbook_asks(ticker)
    except Exception as e:
        say("  orderbook failed", ticker, e)
        return None

def log_depth(stamp, city, target, fav, levels):
    """Record the ask ladder and how much of it the edge could absorb.
    Marginal value of a contract at level p = EDGE_ASSUMED - (p - ask) - extra fee; stop below 1c."""
    if not levels:
        return
    ask = fav["ask"]
    within = lambda c: sum(q for p, q in levels if p <= ask + c + 1e-9)
    cap_n = cap_cost = cap_profit = 0.0
    for p, q in levels:
        marginal = EDGE_ASSUMED - (p - ask) - TAKER_FEE_RATE * ((p * (1 - p)) - ask * (1 - ask))
        if marginal < 0.01:
            break
        cap_n += q
        cap_cost += q * p
        cap_profit += q * marginal
    append_csv(F_DEPTH, DEPTH_FIELDS, [{
        "logged": stamp, "city": city, "date": target.isoformat(), "ticker": fav["ticker"],
        "ask": ask, "oi": fav["oi"], "at_ask": within(0), "within_1c": within(.01),
        "within_2c": within(.02), "within_3c": within(.03), "cap_contracts": cap_n,
        "cap_cost": round(cap_cost, 2), "cap_profit": round(cap_profit, 2),
        "ladder": " ".join(f"{p:.2f}x{q:.0f}" for p, q in levels[:10])}])
    say(f"  depth: {within(0):.0f} at ask, {within(.02):.0f} within 2c, "
        f"capacity ~{cap_n:.0f} contracts (${cap_cost:.0f} cost, ~${cap_profit:.2f} expected)")

def taker_fill(fav, n_want, levels=None):
    """Walk the real book up to the decision ask. Returns (contracts, avg price, fee, source)."""
    limit = fav["ask"]
    if levels is None:
        levels = fetch_levels(fav["ticker"])
    if not levels:
        return n_want, limit, fee(n_want, limit, TAKER_FEE_RATE), "quote"
    got, cost, raw_fee = 0, 0.0, 0.0
    for p, q in levels:
        if p > limit + 1e-9 or got >= n_want:
            break
        take = int(min(q, n_want - got))
        got += take
        cost += take * p
        raw_fee += TAKER_FEE_RATE * take * p * (1 - p)
    if got == 0:
        return 0, limit, 0.0, "book-empty"
    return got, cost / got, math.ceil(round(raw_fee * 100, 6)) / 100, "book"

def bank_of(rows, book):
    return START_BANKROLL + sum(float(r["pnl"]) for r in rows
                                if r["book"] == book and r["status"] == "settled" and r["pnl"] != "")

def used_today(rows, book):
    today = dt.datetime.now(TZ).date().isoformat()
    return sum(int(r["contracts"] or 0) * float(r["limit"] or 0) + float(r["fee"] or 0)
               for r in rows if r["book"] == book and r["logged"][:10] == today
               and r["status"] in ("open", "working", "settled"))

def forward_decide(city):
    series, tz = CITIES[city]
    now = dt.datetime.now(ZoneInfo(tz))
    target = now.date() + dt.timedelta(days=1)
    ms = [m for m in live_markets(series) if target_date(m.get("ticker")) == target]
    stamp = dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    brackets = []
    for m in ms:
        yb, ya = px(m, "yes_bid"), px(m, "yes_ask")
        brackets.append({"ticker": m["ticker"], "bracket": label(m), "bid": yb if yb is not None else 0.0,
                         "ask": ya if ya else 1.0, "oi": num(m, "open_interest", "open_interest_fp")})
    append_csv(F_SNAP, SNAP_FIELDS, [dict(b, logged=stamp, city=city, date=target.isoformat())
                                     for b in brackets])
    fav, action = decide(brackets)
    append_csv(F_DECISIONS, DEC_FIELDS, [{
        "logged": stamp, "city": city, "date": target.isoformat(),
        "ticker": fav["ticker"] if fav else "", "bracket": fav["bracket"] if fav else "",
        "bid": fav["bid"] if fav else "", "ask": fav["ask"] if fav else "",
        "mid": round((fav["bid"] + fav["ask"]) / 2, 4) if fav else "",
        "spread": round(fav["ask"] - fav["bid"], 4) if fav else "",
        "n_brackets": len(brackets), "action": action, "result": ""}])
    msg = f"{city} {target}: {action}"
    if fav:
        msg += f" | fav {fav['bracket']} bid {fav['bid']:.2f} ask {fav['ask']:.2f}"
    say(msg)
    if action != "TRADE":
        return True
    levels = fetch_levels(fav["ticker"])
    log_depth(stamp, city, target, fav, levels)
    rows = read_csv(F_TRADES)
    exp = maker_expiry(city, target)
    for book in FWD_BOOKS:
        if any(r["book"] == book and r["city"] == city and r["date"] == target.isoformat() for r in rows):
            continue
        bank = bank_of(rows, book)
        base = {"book": book, "logged": stamp, "city": city, "date": target.isoformat(),
                "ticker": fav["ticker"], "bracket": fav["bracket"], "side": "YES",
                "decision_bid": fav["bid"], "decision_ask": fav["ask"], "oi": fav["oi"],
                "placed_ts": int(time.time()), "expiry_ts": int(exp.timestamp()),
                "result": "", "win": "", "pnl": ""}
        if book == "taker":
            n_want = size(bank, fav["ask"], TAKER_FEE_RATE)
            if n_want < 1 or n_want * fav["ask"] > MAX_DAILY * bank - used_today(rows, book):
                say("  taker: daily budget used, skipped")
                continue
            n, p, f, src = taker_fill(fav, n_want, levels)
            if n < 1:
                rows.append(dict(base, status="nofill", limit=fav["ask"], price="", contracts=0,
                                 fee=0, filled="no", fill_src=src))
                say("  taker: no liquidity at the decision ask")
                continue
            rows.append(dict(base, status="open", limit=fav["ask"], price=round(p, 4), contracts=n,
                             fee=f, filled=dt.datetime.now(TZ).strftime("%H:%M"), fill_src=src))
            say(f"  taker: BUY YES {n}x @ {p:.3f} (fee ${f:.2f}, {src}{'' if n == n_want else f', wanted {n_want}'})")
        elif book == "demo":
            rows.append(demo_execute(base, fav, bank))
        else:
            lim = fav["bid"]
            n = size(bank, lim, MAKER_FEE_RATE) if lim >= 0.01 else 0
            if n < 1 or n * lim > MAX_DAILY * bank - used_today(rows, book):
                say("  maker: skipped (budget or no bid)")
                continue
            rows.append(dict(base, status="working", limit=lim, price="", contracts=n, fee=0,
                             filled="", fill_src=""))
            say(f"  maker: BID YES {n}x @ {lim:.2f} until {exp.strftime('%Y-%m-%d %H:%M %Z')}")
    write_csv(F_TRADES, TRADE_FIELDS, rows)
    return True

def forward_check_makers():
    rows = read_csv(F_TRADES)
    changed = False
    now_ts = int(time.time())
    for r in rows:
        if r["status"] != "working":
            continue
        city = r["city"]
        series = CITIES.get(city, ("",))[0]
        placed, expiry, lim = int(r["placed_ts"]), int(r["expiry_ts"]), float(r["limit"])
        until = min(now_ts, expiry)
        filled = False
        try:
            r1 = get_live(f"{API}/series/{series}/markets/{r['ticker']}/candlesticks",
                          {"start_ts": placed, "end_ts": until, "period_interval": 1}) or {}
            filled = maker_filled(r1.get("candlesticks"), lim, placed, until)
        except Exception as e:
            say("  candle check failed", r["ticker"], e)
        if not filled:
            try:
                m = (get_live(f"{API}/markets/{r['ticker']}") or {}).get("market", {})
                ya = px(m, "yes_ask")
                filled = ya is not None and 0 < ya <= lim + 1e-9
            except Exception:
                pass
        if filled:
            n = int(r["contracts"])
            r.update(status="open", price=lim, fee=fee(n, lim, MAKER_FEE_RATE),
                     filled=dt.datetime.now(TZ).strftime("%m-%d %H:%M"), fill_src="candle/quote")
            say(f"Maker filled: {city} {r['date']} {r['bracket']} {n}x @ {lim:.2f}")
            changed = True
        elif now_ts >= expiry:
            r.update(status="unfilled", contracts=r["contracts"], filled="no")
            say(f"Maker expired unfilled: {city} {r['date']} {r['bracket']}")
            changed = True
        time.sleep(0.05)
    if changed:
        write_csv(F_TRADES, TRADE_FIELDS, rows)
    return changed

def market_result(ticker):
    for url in (f"{API}/markets/{ticker}", f"{API}/historical/markets/{ticker}"):
        try:
            m = (get_live(url) or {}).get("market", {})
        except Exception:
            continue
        if m.get("result") in ("yes", "no"):
            return m["result"]
    return None

def forward_settle():
    rows = read_csv(F_TRADES)
    n_set = 0
    cache = {}
    for r in rows:
        if r["status"] != "open":
            continue
        today = dt.datetime.now(ZoneInfo(CITIES.get(r["city"], ("", TZ_NAME))[1])).date().isoformat()
        if r["date"] >= today:
            continue
        key = (r["book"] == "demo", r["ticker"])
        res = cache.get(key) or (demo_market_result(r["ticker"]) if r["book"] == "demo"
                                 else market_result(r["ticker"]))
        cache[key] = res
        if not res:
            continue
        n, p, f = int(r["contracts"]), float(r["price"]), float(r["fee"])
        win = res == "yes"
        r.update(status="settled", result=res, win=int(win),
                 pnl=round((n * (1 - p) if win else -n * p) - f, 2))
        n_set += 1
        time.sleep(0.05)
    if n_set:
        write_csv(F_TRADES, TRADE_FIELDS, rows)
    decs = read_csv(F_DECISIONS)
    n_dec = 0
    for d in decs:
        if d["result"] or not d["ticker"]:
            continue
        today = dt.datetime.now(ZoneInfo(CITIES.get(d["city"], ("", TZ_NAME))[1])).date().isoformat()
        if d["date"] >= today:
            continue
        res = cache.get((False, d["ticker"])) or market_result(d["ticker"])
        cache[(False, d["ticker"])] = res
        if res:
            d["result"] = res
            n_dec += 1
    if n_dec:
        write_csv(F_DECISIONS, DEC_FIELDS, decs)
    if n_set:
        say(f"Settled {n_set} forward trades.")
    return n_set or n_dec

# =====================================================================
# KALSHI DEMO EXECUTION (fake money)
# =====================================================================
_DEMO_KEY = None

def demo_key():
    global _DEMO_KEY
    if _DEMO_KEY is None:
        try:
            from cryptography.hazmat.primitives import serialization
        except ImportError:
            raise RuntimeError("missing package: pip install cryptography")
        path = demo_key_path()
        if not os.path.exists(path):
            raise RuntimeError(f"demo private key not found: {path}")
        with open(path, "rb") as f:
            _DEMO_KEY = serialization.load_pem_private_key(f.read(), password=None)
    return _DEMO_KEY

def demo_key_path():
    return DEMO_KEY_FILE if os.path.isabs(DEMO_KEY_FILE) else os.path.join(BASE, DEMO_KEY_FILE)

def demo_key_id():
    k = DEMO_KEY_ID or os.environ.get("KALSHI_DEMO_KEY_ID", "")
    if not k:
        raise RuntimeError("no demo key connected (website: Demo tile, or config.json -> demo.key_id)")
    return k

def save_config(updates):
    """Merge updates into config.json without touching other keys (passcode etc.)."""
    path = os.path.join(BASE, "config.json")
    cfg = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

def set_demo(enabled, key_id):
    """Switch the demo book on/off at runtime (no restart needed)."""
    global DEMO_ENABLED, DEMO_KEY_ID, FWD_BOOKS, _DEMO_KEY
    DEMO_ENABLED, DEMO_KEY_ID, _DEMO_KEY = bool(enabled), key_id or "", None
    FWD_BOOKS = BOOKS + (("demo",) if DEMO_ENABLED else ())
    _DATA_CACHE["t"] = 0.0

def normalize_pem(text):
    """Accept a pasted key even if line breaks got lost (iPad copy/paste) or the header is missing."""
    t = (text or "").strip().replace("\r", "")
    m = re.search(r"-----BEGIN ([A-Z ]*PRIVATE KEY)-----(.*?)-----END \1-----", t, re.S)
    if m:
        kind, body = m.group(1), m.group(2)
    else:
        kind, body = None, t
    body = re.sub(r"[^A-Za-z0-9+/=]", "", body)
    if len(body) < 40:          # Ed25519 keys are only 64 base64 characters, RSA ~1600
        raise ValueError("this is not a private key - paste the whole key text (including the BEGIN/END lines) or pick the key file")
    lines = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    kinds = [kind] if kind else ["PRIVATE KEY", "RSA PRIVATE KEY"]
    return [f"-----BEGIN {k}-----\n{lines}\n-----END {k}-----\n" for k in kinds]

def demo_connect(key_id, pem_text):
    """Validate a pasted demo key with Kalshi, then store it and switch the demo book on."""
    from cryptography.hazmat.primitives import serialization
    global DEMO_KEY_ID, _DEMO_KEY
    key_id = (key_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{6,80}", key_id):
        raise ValueError("the Key ID looks wrong - copy it from demo.kalshi.co -> API keys")
    key, pem = None, None
    for cand in normalize_pem(pem_text):
        try:
            key = serialization.load_pem_private_key(cand.encode(), password=None)
            pem = cand
            break
        except Exception:
            continue
    if key is None:
        raise ValueError("the private key could not be read - paste the complete text or pick the downloaded key file")
    old = (DEMO_KEY_ID, _DEMO_KEY)
    DEMO_KEY_ID, _DEMO_KEY = key_id, key
    try:
        bal = demo_balance()
    except Exception as e:
        DEMO_KEY_ID, _DEMO_KEY = old
        msg = str(e)
        if "401" in msg or "403" in msg:
            msg = "Kalshi demo rejected this Key ID + key combination (check that both are from the DEMO site)"
        raise ValueError(msg[:200])
    global DEMO_KEY_FILE
    DEMO_KEY_FILE = _sane_key_file(DEMO_KEY_FILE)
    path = demo_key_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pem)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    save_config({"demo": {"enabled": True, "key_id": key_id, "key_file": DEMO_KEY_FILE}})
    set_demo(True, key_id)
    _DEMO_KEY = key
    DEMO_BAL["v"] = bal
    say(f"demo: key {key_id[:6]}... connected from the website, demo book ON (balance ${bal})")
    return bal

def demo_disconnect():
    set_demo(False, "")
    save_config({"demo": {"enabled": False, "key_id": ""}})
    try:
        os.remove(demo_key_path())
    except FileNotFoundError:
        pass
    DEMO_BAL.pop("v", None)
    say("demo: disconnected from the website, key file deleted, demo book OFF")

def demo_connected():
    return bool(DEMO_KEY_ID) and os.path.exists(demo_key_path())

def demo_req(method, path, params=None, body=None, auth=True):
    """Signed request to the DEMO exchange only. path like '/portfolio/balance'."""
    assert "demo" in DEMO_API, "safety: DEMO_API must point to the demo exchange"
    import base64
    from urllib.parse import urlparse
    url = DEMO_API + path
    headers = {"Content-Type": "application/json", "User-Agent": "weather-fav-demo"}
    if auth:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, ed25519
        key = demo_key()
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + urlparse(url).path).encode()      # query string NOT signed
        if isinstance(key, ed25519.Ed25519PrivateKey):
            sig = key.sign(msg)
        else:
            sig = key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                            salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        headers.update({"KALSHI-ACCESS-KEY": demo_key_id(), "KALSHI-ACCESS-TIMESTAMP": ts,
                        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()})
    r = requests.request(method, url, params=params, json=body, headers=headers, timeout=30)
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise RuntimeError(f"demo {method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
    return r.json() if r.text else {}

def demo_balance():
    r = demo_req("GET", "/portfolio/balance") or {}
    if r.get("balance_dollars") not in (None, ""):
        return float(r["balance_dollars"])
    return to_dollars(r.get("balance")) if r.get("balance") is not None else None

def order_num(o, key):
    """fill_count etc. may come as int, '12' or fixed-point '12.00' (key or key_fp)."""
    for k in (key, key + "_fp"):
        if o.get(k) not in (None, ""):
            try:
                return float(o[k])
            except (TypeError, ValueError):
                pass
    return 0.0

def demo_execute(base, fav, bank):
    """Mirror the taker decision on the demo exchange: IOC limit buy YES at the real decision ask."""
    row = dict(base, book="demo", limit=fav["ask"], price="", contracts=0, fee=0, filled="no")
    n_want = size(bank, fav["ask"], TAKER_FEE_RATE)
    if n_want < 1:
        return dict(row, status="nofill", fill_src="demo: size 0")
    try:
        m = (demo_req("GET", f"/markets/{fav['ticker']}", auth=False) or {}).get("market")
        if not m:
            say("  demo: ticker not listed on the demo exchange")
            return dict(row, status="nofill", fill_src="demo: not listed")
        import uuid
        body = {"ticker": fav["ticker"], "side": "yes", "action": "buy", "type": "limit",
                "count": int(n_want), "yes_price": int(round(fav["ask"] * 100)),
                "time_in_force": "immediate_or_cancel", "client_order_id": str(uuid.uuid4())}
        o = (demo_req("POST", "/portfolio/orders", body=body) or {}).get("order", {})
    except Exception as e:
        say("  demo order failed:", e)
        return dict(row, status="error", fill_src=f"demo: {str(e)[:80]}")
    n = int(order_num(o, "fill_count"))
    cost = px(o, "taker_fill_cost") or 0.0
    fees = px(o, "taker_fees") or 0.0
    demo_ask = px(m, "yes_ask")
    note = f"demo:{o.get('order_id', '?')[:8]} demo ask {demo_ask if demo_ask is not None else '?'}"
    if n < 1:
        say(f"  demo: IOC {n_want}x @ {fav['ask']:.2f} not filled (demo ask {demo_ask})")
        return dict(row, status="nofill", fill_src=note)
    p = cost / n if cost else fav["ask"]
    say(f"  demo: filled {n}/{n_want} @ {p:.3f}, fee ${fees:.2f} ({note})")
    return dict(row, status="open", price=round(p, 4), contracts=n, fee=round(fees, 2),
                filled=dt.datetime.now(TZ).strftime("%H:%M"), fill_src=note)

def demo_market_result(ticker):
    try:
        m = (demo_req("GET", f"/markets/{ticker}", auth=False) or {}).get("market", {})
    except Exception:
        return None
    return m.get("result") if m.get("result") in ("yes", "no") else None

def demo_smoke():
    """Step-by-step test of the demo connection: login, markets, 1x YES bid at 1c, read back, cancel.
    Returns [{"step", "ok", "detail"}]; stops at the first failure."""
    import uuid
    steps, ctx = [], {}
    def step(name, fn):
        try:
            out = fn()
            steps.append({"step": name, "ok": True, "detail": str(out)[:160]})
            return True
        except Exception as e:
            steps.append({"step": name, "ok": False, "detail": str(e)[:200]})
            return False
    series = CITIES["New York"][0]
    def markets():
        r = demo_req("GET", "/markets", params={"series_ticker": series, "status": "open", "limit": 200}, auth=False) or {}
        ms = r.get("markets", [])
        if not ms:
            raise RuntimeError(f"no open {series} markets on demo")
        m = max(ms, key=lambda m: (px(m, "yes_bid") or 0) + (px(m, "yes_ask") or 1))
        ctx["m"] = m
        return f"{len(ms)} open, sample {m['ticker']} demo bid {px(m, 'yes_bid')} ask {px(m, 'yes_ask')}"
    def place():
        body = {"ticker": ctx["m"]["ticker"], "side": "yes", "action": "buy", "type": "limit", "count": 1,
                "yes_price": 1, "time_in_force": "good_till_canceled", "post_only": True,
                "client_order_id": str(uuid.uuid4())}
        o = (demo_req("POST", "/portfolio/orders", body=body) or {}).get("order", {})
        if not o.get("order_id"):
            raise RuntimeError(f"no order id in response: {o}")
        ctx["oid"] = o["order_id"]
        return f"order {o['order_id'][:8]}... status {o.get('status')}"
    ok = (step("private key loads", lambda: type(demo_key()).__name__)
          and step("key id set", lambda: demo_key_id()[:6] + "...")
          and step("login + balance", lambda: f"${demo_balance()}")
          and step(f"weather markets on demo ({series})", markets)
          and step("place 1x YES bid @ 1c (post-only)", place)
          and step("read order back", lambda: (demo_req("GET", f"/portfolio/orders/{ctx['oid']}") or {}).get("order", {}).get("status"))
          and step("cancel order", lambda: (demo_req("DELETE", f"/portfolio/orders/{ctx['oid']}") or {}).get("order", {}).get("status", "canceled")))
    return steps

def cmd_demo_check():
    print(f"Kalshi DEMO check against {DEMO_API}")
    steps = demo_smoke()
    for s in steps:
        print(f"  {'OK  ' if s['ok'] else 'FAIL'} {s['step']}: {s['detail']}")
    if all(s["ok"] for s in steps):
        print("Demo connection works." + ("" if DEMO_ENABLED else
              ' Turn it on in the website (Demo tile) or set "enabled": true in config.json and restart.'))
    else:
        sys.exit(1)

def due_cities(state):
    out = []
    for c, (_, tz) in CITIES.items():
        now = dt.datetime.now(ZoneInfo(tz))
        target = (now.date() + dt.timedelta(days=1)).isoformat()
        if state.get(c) == target:
            continue
        if (now.hour, now.minute) >= DECISION_LOCAL and now.hour < DECISION_LATEST_HOUR:
            out.append((c, target))
    return out

def next_decision_berlin(city):
    tz = ZoneInfo(CITIES[city][1])
    now = dt.datetime.now(tz)
    t = now.replace(hour=DECISION_LOCAL[0], minute=DECISION_LOCAL[1], second=0, microsecond=0)
    if t <= now:
        t += dt.timedelta(days=1)
    return t.astimezone(TZ).strftime("%a %H:%M")


# =====================================================================
# DATA FOR THE WEBSITE
# =====================================================================
DEMO_BAL = {}
_DATA_CACHE = {"t": 0.0, "v": None}

def capacity_summary():
    rows = read_csv(F_DEPTH)
    if not rows:
        return None
    med = lambda k: statistics.median(float(r[k]) for r in rows if r[k] != "")
    days = {}
    for r in rows:
        d = days.setdefault(r["logged"][:10], [0.0, 0.0])
        d[0] += float(r["cap_cost"])
        d[1] += float(r["cap_profit"])
    asks = [float(r["ask"]) for r in rows]
    my = size(START_BANKROLL, statistics.median(asks), TAKER_FEE_RATE)
    return {"n": len(rows), "days": len(days), "at_ask": med("at_ask"), "within_2c": med("within_2c"),
            "cap_contracts": med("cap_contracts"), "cap_cost": med("cap_cost"), "oi": med("oi"),
            "my_size": my, "edge": EDGE_ASSUMED,
            "day_cost": statistics.mean(v[0] for v in days.values()),
            "day_cost_max": max(v[0] for v in days.values()),
            "day_profit": statistics.mean(v[1] for v in days.values())}

def next_decision_ts(city):
    tz = ZoneInfo(CITIES[city][1])
    now = dt.datetime.now(tz)
    t = now.replace(hour=DECISION_LOCAL[0], minute=DECISION_LOCAL[1], second=0, microsecond=0)
    if t <= now:
        t += dt.timedelta(days=1)
    return int(t.timestamp())

def log_tail(n=14):
    try:
        with open(RUNLOG, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-400:]
    except Exception:
        return []
    out = []
    for l in lines:
        if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", l):
            l = l.rstrip()
            if "Traceback" in l:
                l = l.split("Traceback")[0].rstrip() + " (details in data/logs/winter.log)"
            out.append(l[:170])
    return out[-n:]

def build_data():
    if time.time() - _DATA_CACHE["t"] < 10 and _DATA_CACHE["v"] is not None:
        return _DATA_CACHE["v"]
    rows = read_csv(F_TRADES)
    fwd = {}
    for b in FWD_BOOKS:
        mine = [r for r in rows if r["book"] == b]
        done = [{"date": r["date"], "price": float(r["price"]), "contracts": int(r["contracts"]),
                 "win": int(r["win"]), "pnl": float(r["pnl"]), "fee": float(r["fee"])}
                for r in mine if r["status"] == "settled"]
        s = book_stats(done)
        s["verdict"] = verdict(s)
        s["open"] = sum(r["status"] in ("open", "working") for r in mine)
        s["orders"] = sum(r["status"] in ("open", "settled", "unfilled") for r in mine)
        s["filled_n"] = sum(r["status"] in ("open", "settled") for r in mine)
        s["pnl"] = s.get("pnl", 0.0)
        fwd[b] = s
    state = read_json(F_STATE, {})
    sched = []
    for c, (_, tz) in CITIES.items():
        now = dt.datetime.now(ZoneInfo(tz))
        tgt = (now.date() + dt.timedelta(days=1)).isoformat()
        sched.append({"city": c, "tz": tz, "local": now.strftime("%H:%M"),
                      "done": tgt if state.get(c) == tgt else "", "next_ts": next_decision_ts(c)})
    opn = []
    for r in rows:
        if r["status"] in ("open", "working"):
            exp = dt.datetime.fromtimestamp(int(r["expiry_ts"]), ZoneInfo(CITIES.get(r["city"], ("", TZ_NAME))[1]))
            opn.append(dict(r, expires=exp.strftime("%a %H:%M local")))
    data = {"version": VERSION, "now_ts": int(time.time()),
            "updated": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"), "start": START_BANKROLL,
            "rule": {"min": round(FAV_MIN * 100), "max": round(FAV_MAX * 100), "spread": round(MAX_SPREAD * 100),
                     "stake": round(STAKE_FRAC * 100, 1), "max_stake": MAX_STAKE_USD, "max_daily": round(MAX_DAILY * 100),
                     "tfee": TAKER_FEE_RATE, "mfee": MAKER_FEE_RATE, "exp": "%02d:%02d" % MAKER_EXPIRY_LOCAL,
                     "decision": "%02d:%02d" % DECISION_LOCAL},
            "fwd": fwd, "sched": sched, "open": opn,
            "decs": read_csv(F_DECISIONS)[::-1][:40],
            "done": [r for r in rows if r["status"] == "settled"][::-1][:60],
            "bt": read_json(B_SUMMARY, None), "cap": capacity_summary(),
            "demo": {"enabled": DEMO_ENABLED, "connected": demo_connected(), "balance": DEMO_BAL.get("v"),
                     "key_hint": (DEMO_KEY_ID[:6] + "…") if DEMO_KEY_ID else ""},
            "tunnel": {"url": TUNNEL.get("url"), "state": TUNNEL.get("state"), "verified": TUNNEL.get("verified")},
            "log": log_tail(), "autolock_min": AUTOLOCK_MIN}
    _DATA_CACHE.update(t=time.time(), v=data)
    return data

# =====================================================================
# PASSCODE LOCK (server side: the data never leaves the Pi without a valid token)
# =====================================================================
AUTH_LOCK = threading.Lock()
TOKENS = {}                       # token -> expiry ts (kept in memory: a restart locks everyone)
FAILS = {}                        # ip -> {"n": wrong attempts, "until": locked-until ts}
TOKEN_TTL = 12 * 3600
LOCKOUT = {6: 60, 7: 300, 8: 900}  # like an iPad: 6th wrong code 1 min, 7th 5 min, 8th 15 min, then 60 min

def lockout_for(n):
    if n < 6:
        return 0
    return LOCKOUT.get(n, 3600)

def auth_status(ip):
    with AUTH_LOCK:
        f = FAILS.get(ip, {"n": 0, "until": 0})
        return {"attempts": f["n"], "wait": max(0, int(f["until"] - time.time()))}

def try_unlock(ip, code):
    with AUTH_LOCK:
        f = FAILS.setdefault(ip, {"n": 0, "until": 0})
        now = time.time()
        if f["until"] > now:
            return None, {"error": "locked", "wait": int(f["until"] - now), "attempts": f["n"]}
        if hmac.compare_digest(str(code).encode(), PASSCODE.encode()):
            FAILS.pop(ip, None)
            tok = secrets.token_urlsafe(32)
            for t in [t for t, e in TOKENS.items() if e < now]:
                TOKENS.pop(t, None)
            TOKENS[tok] = now + TOKEN_TTL
            return tok, None
        f["n"] += 1
        f["until"] = now + lockout_for(f["n"])
        return None, {"error": "wrong", "wait": lockout_for(f["n"]), "attempts": f["n"]}

def token_ok(tok):
    if not tok:
        return False
    with AUTH_LOCK:
        exp = TOKENS.get(tok)
        if not exp or exp < time.time():
            TOKENS.pop(tok, None)
            return False
        return True

def revoke(tok):
    with AUTH_LOCK:
        TOKENS.pop(tok, None)

# =====================================================================
# WEB SERVER
# =====================================================================
STATIC_FILES = {"index.html", "winter.css", "winter.js", "icon.svg", "manifest.webmanifest"}
DOWNLOADS = {"forward_trades.csv": F_TRADES, "forward_decisions.csv": F_DECISIONS,
             "forward_depth.csv": F_DEPTH, "backtest_trades.csv": B_TRADES,
             "backtest_decisions.csv": B_DECISIONS}

class Web(BaseHTTPRequestHandler):
    server_version = "Winter"
    sys_version = ""

    def log_message(self, *a):
        pass

    def client_ip(self):
        peer = self.client_address[0]
        if peer in ("127.0.0.1", "::1"):          # behind the Cloudflare tunnel
            return self.headers.get("CF-Connecting-IP") or peer
        return peer

    def send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def token(self):
        return self.headers.get("X-Winter-Token", "")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self.static("index.html")
            if path.startswith("/static/"):
                return self.static(path[len("/static/"):])
            if path == "/healthz":
                return self.send(200, {"ok": True, "version": VERSION})
            if path == "/api/status":
                st = auth_status(self.client_ip())
                return self.send(200, dict(st, version=VERSION, autolock_min=AUTOLOCK_MIN, digits=len(PASSCODE)))
            if path == "/api/data":
                if not token_ok(self.token()):
                    return self.send(401, {"error": "locked"})
                return self.send(200, build_data())
            if path.startswith("/api/file/"):
                if not token_ok(self.token()):
                    return self.send(401, {"error": "locked"})
                name = path[len("/api/file/"):]
                fp = DOWNLOADS.get(name)
                if not fp or not os.path.exists(fp):
                    return self.send(404, {"error": "not found"})
                with open(fp, "rb") as f:
                    return self.send(200, f.read(), "text/csv; charset=utf-8",
                                     {"Content-Disposition": f'attachment; filename="{name}"'})
            return self.send(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            say("web error", traceback.format_exc().splitlines()[-1])
            try:
                self.send(500, {"error": "server error"})
            except Exception:
                pass

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 20000 if path == "/api/demo/connect" else 4096)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            body = {}
        if path == "/api/unlock":
            ip = self.client_ip()
            tok, err = try_unlock(ip, str(body.get("code", ""))[:12])
            if tok:
                say(f"web: unlocked from {ip}")
                return self.send(200, {"token": tok, "autolock_min": AUTOLOCK_MIN})
            if err["error"] == "wrong" and err["attempts"] >= 3:
                say(f"web: wrong passcode #{err['attempts']} from {ip}")
            return self.send(429 if err["error"] == "locked" else 401, err)
        if path == "/api/lock":
            revoke(self.token())
            return self.send(200, {"ok": True})
        if path.startswith("/api/demo/"):
            if not token_ok(self.token()):
                return self.send(401, {"error": "locked"})
            try:
                with LOCK:
                    if path == "/api/demo/connect":
                        bal = demo_connect(str(body.get("key_id", "")), str(body.get("private_key", "")))
                        return self.send(200, {"ok": True, "balance": bal})
                    if path == "/api/demo/check":
                        steps = demo_smoke()
                        return self.send(200, {"ok": all(s["ok"] for s in steps), "steps": steps})
                    if path == "/api/demo/disconnect":
                        demo_disconnect()
                        return self.send(200, {"ok": True})
            except ImportError:
                return self.send(400, {"error": "package cryptography missing: ./venv/bin/pip install cryptography"})
            except Exception as e:
                return self.send(400, {"error": str(e)[:240]})
        return self.send(404, {"error": "not found"})

    def static(self, name):
        if name not in STATIC_FILES:
            return self.send(404, {"error": "not found"})
        fp = os.path.join(STATIC, name)
        if not os.path.exists(fp):
            return self.send(404, {"error": "missing " + name})
        ctype = {"html": "text/html; charset=utf-8", "css": "text/css; charset=utf-8",
                 "js": "application/javascript; charset=utf-8", "svg": "image/svg+xml",
                 "webmanifest": "application/manifest+json"}.get(name.rsplit(".", 1)[-1], "application/octet-stream")
        with open(fp, "rb") as f:
            self.send(200, f.read(), ctype)

SERVER = {}

def bind_server():
    """Bind the website port in the main thread so a failure is loud, not a silent dead thread."""
    for attempt in range(6):
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", PORT), Web)
            srv.daemon_threads = True
            SERVER["srv"] = srv
            return srv
        except OSError as e:
            who = ""
            try:
                out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=5).stdout
                who = " | ".join(l.strip() for l in out.splitlines() if f":{PORT} " in l)[:200]
            except Exception:
                pass
            say(f"web: port {PORT} not free ({e}) {who} - retry {attempt + 1}/6 in 5 s")
            time.sleep(5)
    say(f"web: FATAL - port {PORT} stays busy. Is Winter running twice? ./stop.sh, or change 'port' in config.json")
    sys.exit(2)

def serve():
    SERVER["srv"].serve_forever()

# =====================================================================
# CLOUDFLARE QUICK TUNNEL (free, no account; new random URL on every start)
# A watchdog fetches <public url>/healthz from the Pi every minute. If the link is dead for
# ~5 minutes, cloudflared is restarted (new URL), switching to protocol http2 on the way.
# =====================================================================
TUNNEL = {"url": None, "state": "off", "proc": None, "registered": False, "verified": None,
          "fails": 0, "last_err": "", "since": 0.0, "protocol": "auto", "restarts": 0}
CF_EMPTY_CONF = os.path.join(DATA, "cloudflared-quick.yml")
CF_STATUS = os.path.join(DATA, "tunnel_status.json")

def _child_dies_with_parent():
    try:                                    # Linux: kill cloudflared if Winter dies (PR_SET_PDEATHSIG)
        import ctypes
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)
    except Exception:
        pass

def _kill_stale_tunnel():
    try:
        pid = int(open(CF_PID).read().strip())
        cmd = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="ignore")
        if "cloudflared" in cmd and f":{PORT}" in cmd:
            os.kill(pid, signal.SIGTERM)
            say(f"tunnel: stopped a leftover cloudflared (pid {pid})")
    except Exception:
        pass

def _tunnel_publish():
    st = {k: TUNNEL[k] for k in ("url", "state", "registered", "verified", "fails", "last_err", "protocol", "restarts")}
    st["updated"] = int(time.time())
    try:
        write_json(CF_STATUS, st)
    except Exception:
        pass
    _DATA_CACHE["t"] = 0.0

def tunnel_loop():
    exe = shutil.which("cloudflared")
    if not exe:
        TUNNEL["state"] = "missing"
        _tunnel_publish()
        say("tunnel: cloudflared not installed - website only on your network (run ./setup.sh)")
        return
    _kill_stale_tunnel()
    for name in ("config.yml", "config.yaml"):
        if os.path.exists(os.path.expanduser(f"~/.cloudflared/{name}")):
            say(f"tunnel: ~/.cloudflared/{name} exists (another project) - Winter ignores it via its own empty config")
    with open(CF_EMPTY_CONF, "w") as f:
        f.write("# empty on purpose: quick tunnels refuse to start when a default config.yml is present\n")
    while True:
        TUNNEL.update(state="starting", url=None, registered=False, verified=None, fails=0, last_err="", since=time.time())
        _tunnel_publish()
        cmd = [exe, "tunnel", "--config", CF_EMPTY_CONF, "--no-autoupdate"]
        if TUNNEL["protocol"] != "auto":
            cmd += ["--protocol", TUNNEL["protocol"]]
        cmd += ["--url", f"http://127.0.0.1:{PORT}"]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                 encoding="utf-8", errors="replace", preexec_fn=_child_dies_with_parent)
        except Exception as e:
            say("tunnel: could not start cloudflared:", e)
            TUNNEL.update(state="down", last_err=str(e)[:200])
            _tunnel_publish()
            time.sleep(60)
            continue
        TUNNEL["proc"] = p
        try:
            with open(CF_PID, "w") as f:
                f.write(str(p.pid))
        except Exception:
            pass
        try:
            log = open(CF_LOG, "w", encoding="utf-8")
        except Exception:
            log = None
        for line in p.stdout:                      # must never stop reading, or cloudflared blocks
            try:
                if log:
                    log.write(line)
                    log.flush()
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if m and TUNNEL["url"] != m.group(0):
                    TUNNEL.update(url=m.group(0), state="connecting")
                    with open(os.path.join(DATA, "tunnel_url.txt"), "w") as f:
                        f.write(m.group(0) + "\n")
                    say(f"tunnel: URL assigned {m.group(0)} (waiting for the connection)")
                    _tunnel_publish()
                if "Registered tunnel connection" in line and not TUNNEL["registered"]:
                    TUNNEL.update(registered=True, state="up")
                    say(f"tunnel: connected to Cloudflare - PUBLIC URL {TUNNEL['url']}")
                    _tunnel_publish()
                if " ERR " in line or "error=" in line:
                    TUNNEL["last_err"] = line.strip()[-220:]
            except Exception:
                pass
        p.wait()
        if log:
            log.close()
        TUNNEL.update(state="down", url=None, proc=None, registered=False, verified=None)
        _tunnel_publish()
        say(f"tunnel: cloudflared exited (code {p.returncode}) - restarting in 15 s"
            + (f" | last error: {TUNNEL['last_err'][-120:]}" if TUNNEL["last_err"] else ""))
        time.sleep(15)

def _wd_sleep(sec):
    time.sleep(sec)

def tunnel_watchdog():
    """Check the public link from the outside every minute; restart a dead tunnel."""
    while True:
        _wd_sleep(60)
        try:
            url, p = TUNNEL.get("url"), TUNNEL.get("proc")
            if not url or not p or p.poll() is not None:
                continue
            age = time.time() - TUNNEL["since"]
            ok = False
            try:
                r = requests.get(url + "/healthz", timeout=15, headers={"User-Agent": "winter-watchdog"})
                ok = r.status_code == 200 and "winter" in r.text
                err = f"HTTP {r.status_code}"
            except Exception as e:
                err = type(e).__name__
            if ok:
                if TUNNEL["verified"] is not True:
                    say(f"tunnel: public link verified from outside ({url})")
                TUNNEL.update(verified=True, fails=0)
            else:
                TUNNEL["fails"] += 1
                TUNNEL["verified"] = False
                if TUNNEL["fails"] in (2, 4):
                    say(f"tunnel: public link not reachable yet ({err}), check {TUNNEL['fails']}")
                if TUNNEL["fails"] >= 5 and age > 240:
                    TUNNEL["restarts"] += 1
                    if TUNNEL["restarts"] >= 1 and TUNNEL["protocol"] == "auto":
                        TUNNEL["protocol"] = "http2"
                    say(f"tunnel: link dead for ~5 min ({err}) - restarting cloudflared with protocol "
                        f"{TUNNEL['protocol']} (you will get a NEW URL)")
                    stop_tunnel()
            _tunnel_publish()
        except Exception:
            pass

def stop_tunnel(*_):
    p = TUNNEL.get("proc")
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(5)
        except Exception:
            p.kill()

def cmd_tunnel_check():
    """Diagnose the public link step by step (run while Winter is running)."""
    import socket
    def line(ok, name, detail=""):
        print(f"  {'OK  ' if ok else 'FAIL' if ok is False else 'INFO'} {name}{': ' + detail if detail else ''}")
    print(f"Winter tunnel check (port {PORT})")
    try:
        r = requests.get(f"http://127.0.0.1:{PORT}/healthz", timeout=5)
        line(r.status_code == 200, "website answers locally", f"HTTP {r.status_code}")
    except Exception as e:
        line(False, "website answers locally", f"{type(e).__name__} -> Winter is not running or the port is busy (tmux attach -t Winter)")
    exe = shutil.which("cloudflared")
    line(bool(exe), "cloudflared installed", exe or "run ./setup.sh")
    if exe:
        try:
            v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
            line(None, "cloudflared version", v[:80])
        except Exception:
            pass
    for name in ("config.yml", "config.yaml"):
        pth = os.path.expanduser(f"~/.cloudflared/{name}")
        if os.path.exists(pth):
            line(None, f"{pth} exists", "fine: Winter starts cloudflared with its own empty config")
    st = read_json(CF_STATUS, {})
    running = False
    try:
        pid = int(open(CF_PID).read().strip())
        running = "cloudflared" in open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="ignore")
    except Exception:
        pass
    line(running, "cloudflared process running")
    url = st.get("url")
    line(bool(url), "public URL assigned", url or "none yet")
    line(bool(st.get("registered")), "connected to Cloudflare edge", "protocol " + str(st.get("protocol", "auto")))
    if url:
        host = url.replace("https://", "")
        try:
            ips = sorted({a[4][0] for a in socket.getaddrinfo(host, 443)})
            line(True, "DNS resolves", ", ".join(ips[:3]))
        except Exception as e:
            line(False, "DNS resolves", f"{e} -> brand-new URLs can take 1-2 min; phones may cache 'not found' for a while")
        try:
            r = requests.get(url + "/healthz", timeout=15)
            line(r.status_code == 200, "public link works from the Pi", f"HTTP {r.status_code}"
                 + (" (Cloudflare reaches the tunnel but not the website)" if r.status_code in (502, 530) else ""))
        except Exception as e:
            line(False, "public link works from the Pi", type(e).__name__)
    if st.get("last_err"):
        line(None, "last cloudflared error", st["last_err"][-160:])
    try:
        errs = [l.strip() for l in open(CF_LOG, encoding="utf-8", errors="replace") if " ERR " in l][-5:]
        for e in errs:
            print("       " + e[-170:])
    except Exception:
        pass

# ---------- main loop ----------
def safe(fn, *a):
    try:
        with LOCK:
            return fn(*a)
    except Exception:
        say("ERROR in", fn.__name__, traceback.format_exc())

def _exit(signum, frame):
    stop_tunnel()
    sys.exit(0)

def cmd_run():
    atexit.register(stop_tunnel)
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, _exit)
    bind_server()
    threading.Thread(target=serve, daemon=True).start()
    if TUNNEL_ON:
        threading.Thread(target=tunnel_loop, daemon=True).start()
        threading.Thread(target=tunnel_watchdog, daemon=True).start()
    say(f"WINTER {VERSION} · website http://<pi-ip>:{PORT} · books: {', '.join(FWD_BOOKS)}"
        f" · stake {STAKE_FRAC*100:.0f}% (max ${MAX_STAKE_USD:.0f})")
    for c in CITIES:
        say(f"  {c:<13} next decision {next_decision_berlin(c)} Berlin")
    last_maker = last_settle = 0.0
    attempts = {}
    while True:
        state = read_json(F_STATE, {})
        for city, target in due_cities(state):
            if time.time() - attempts.get(city, 0) < 300:
                continue                      # failed recently: retry every 5 min until cut-off
            attempts[city] = time.time()
            if safe(forward_decide, city):
                state[city] = target
                write_json(F_STATE, state)
        if time.time() - last_maker >= MAKER_CHECK_SEC:
            safe(forward_check_makers)
            last_maker = time.time()
        if time.time() - last_settle >= SETTLE_EVERY_MIN * 60:
            safe(forward_settle)
            if DEMO_ENABLED:
                try:
                    DEMO_BAL["v"] = demo_balance()
                except Exception as e:
                    say("demo balance failed:", e)
            last_settle = time.time()
        time.sleep(30)

def main():
    for d in (DATA, LOGS, CACHE):
        os.makedirs(d, exist_ok=True)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "backtest":
        cmd_backtest(sys.argv[2:])
    elif cmd == "demo-check":
        cmd_demo_check()
    elif cmd == "demo-connect":
        ap = argparse.ArgumentParser(prog="winter.py demo-connect")
        ap.add_argument("--key-id", required=True)
        a = ap.parse_args(sys.argv[2:])
        print("Paste the private key, then Ctrl+D:" if sys.stdin.isatty() else "Reading the private key ...")
        pem = sys.stdin.read()
        try:
            bal = demo_connect(a.key_id, pem)
        except ImportError:
            print("FAIL: package cryptography missing -> ./venv/bin/pip install cryptography"); sys.exit(1)
        except Exception as e:
            print(f"FAIL: {e}"); sys.exit(1)
        print(f"OK: demo key connected, demo balance ${bal}. Restart Winter so the running process uses it: ./stop.sh && ./start.sh")
    elif cmd == "tunnel-check":
        cmd_tunnel_check()
    elif cmd == "passcode-check":
        src = "env WINTER_PASSCODE" if os.environ.get("WINTER_PASSCODE") else \
              ("config.json" if os.path.exists(os.path.join(BASE, "config.json")) else "built-in default")
        print(f"passcode: {len(PASSCODE)} digits, from {src} (not printed)")
    elif cmd == "run":
        cmd_run()
    else:
        print(__doc__)

if __name__ == "__main__":
    main()
