# ❄ WINTER — Kalshi weather favorite-bias desk

Paper trader + backtester + optional Kalshi **demo** execution for a Raspberry Pi, with a
passcode-locked website (iPad-style lock screen) and a built-in free Cloudflare tunnel.

Winter is a **separate project**: own folder, own Python venv, own data, own tmux session (`Winter`),
own port (**8752**). It does not touch any other bot on the Pi.

## The strategy
The day before, at **15:00 local time** in each city, Winter looks at tomorrow's Kalshi daily-high
event (New York, Chicago, Miami, Austin, Los Angeles, Denver, Philadelphia) and finds the **market
favorite** (bracket with the highest mid price). It buys **YES** if the ask is **40–80¢** and the
spread is **≤ 2¢**. No weather model.

Why: Kalshi's day-ahead favorites were slightly underpriced (favorite–longshot bias).
Backtest May 9 – Nov 4 2026: 860 trades, +5.45¢ per contract after fees (95% CI +2.0 … +8.7¢).
The rule was chosen on May–Aug and held on Sep–Nov (+6.6¢). Biggest limit: **thin orderbooks**
(median open interest of a favorite ≈ 500 contracts), so size stays small.

| Book | What it does |
|---|---|
| **taker** | buys at the ask; live it walks the real orderbook and never pays above the decision ask. **This is the edge test.** |
| **maker** | rests a bid at the best bid until 09:00 local on the target day; fills only if the ask touches it or a trade prints below it |
| **demo** (optional) | sends every taker trade as a real order to the **Kalshi DEMO exchange** (fake money). Tests order handling; demo prices are not the real market |

## Files
| File | Purpose |
|---|---|
| `winter.py` | everything: strategy, backtest, forward tester, demo client, website server, tunnel |
| `static/` | the website (lock screen + dashboard) |
| `config.example.json` | settings template → copied to `config.json` by `setup.sh` (config.json is **not** in git) |
| `setup.sh` / `start.sh` / `stop.sh` | install / start the tmux session `Winter` / stop |
| `data/` | all results, logs and the download cache (not in git) |

## Setup on the Pi (Raspberry Pi OS Bookworm, 64-bit recommended)
```bash
cd ~
git clone https://github.com/investorgative/Winter.git winter
cd winter
chmod +x setup.sh && ./setup.sh     # venv, packages, cloudflared; asks you for the website passcode
```
`setup.sh` asks once for the passcode (4–8 digits, typing is hidden). It is stored only in
`config.json` on the Pi, which is never committed, because this repository is public.

**Optional, saves most of the backtest download** (if you still have the old weather-edge folder):
```bash
cp -r ~/weather-edge/backtest_cache/. ~/winter/data/cache/
```

Run the backtest once (fills the Backtest section of the website, ~10–20 min without the cache):
```bash
./venv/bin/python winter.py backtest            # last 180 days
./venv/bin/python winter.py backtest --days 365 # longer history
```

Start:
```bash
./start.sh
```
It prints the address on your network (`http://<pi-ip>:8752`) and the **public URL**
(`https://<random-words>.trycloudflare.com`). The public URL is also in the website's top bar
(click it to copy) and in `data/tunnel_url.txt`.

## The website
* Every visit and every reload starts at the **lock screen**. Type the passcode on the keypad or keyboard.
* The passcode is checked **on the Pi**. The page itself contains no data; data is only sent with a
  session token that exists in the browser's memory (gone on reload).
* Wrong codes: 5 free tries, then like an iPad: 1 min, 5 min, 15 min, then 60 min lockout (per IP).
* Auto-lock after `autolock_minutes` (default 10) without activity, or with the 🔒 button.
* Add it to the iPad/iPhone home screen (Share → Add to Home Screen) for a full-screen app.

## Daily use
```bash
tmux attach -t Winter          # watch it (leave with Ctrl+B, then D)
./stop.sh && ./start.sh        # restart (the public URL changes)
git pull && ./stop.sh && ./start.sh                       # update to the newest version
cat data/tunnel_url.txt        # current public URL
./venv/bin/python winter.py passcode-check                # where the passcode comes from
```
Change the passcode: `nano config.json` → `"passcode": "…"` → `./stop.sh && ./start.sh`.

Autostart after reboot: `crontab -e`, add
```
@reboot sleep 30 && /home/<user>/winter/start.sh
```

## Kalshi demo account (optional, fake money)
1. Sign up at **demo.kalshi.co/sign-up** (not .com). Kalshi's own guide says to use mock personal details.
2. Add fake funds with a test card: Visa `4000 0566 5566 5556`, any future date, any CVV.
3. Profile/Account settings → **API Keys** → create a key. Copy the **Key ID** and the **private key**
   (shown once, or downloaded as a file).
4. On the Winter website, unlock, scroll to the **Demo book** tile, paste the Key ID and the private key
   (or tap *choose the key file*), press **Connect demo**.

Winter checks the key with Kalshi demo right away, stores it on the Pi as `kalshi_demo.pem`
(permissions 600, in `.gitignore`), turns the demo book on, no restart. The key is never sent back to
the browser. **Run test order** places a 1-contract bid at 1¢ and cancels it; **Disconnect** deletes the key.
A wrong Key ID or key is rejected with a message and nothing is saved.

Command-line alternative: put the key file at `~/winter/kalshi_demo.pem`, the Key ID into
`config.json` → `"demo": {"enabled": true, "key_id": "…"}`, then `./venv/bin/python winter.py demo-check`.

## Settings (`config.json`)
| key | default | meaning |
|---|---|---|
| `port` | 8752 | website port |
| `passcode` | set by setup | lock screen code, 4–8 digits |
| `autolock_minutes` | 10 | inactivity lock |
| `tunnel` | true | start the Cloudflare quick tunnel |
| `start_bankroll` | 1000 | per book |
| `stake_frac` | 0.02 | stake per trade, share of the book |
| `max_stake_usd` | 50 | hard cap per trade (liquidity) |
| `max_daily` | 0.15 | max share of the book per Berlin day |
| `maker_fee_rate` | 0.0175 | 25% of taker; 0 if the series has no maker fee |
| `edge_assumed` | 0.0545 | used for the capacity estimate |
The strategy rule itself (40–80¢, ≤ 2¢ spread, 15:00 local) is fixed in `winter.py` on purpose: it was
validated out of sample, and tuning it on the same data would overfit.

## Ports on this Pi
| Port | Project |
|---|---|
| 8752 | **Winter** |
| 8755 | weather edge v4 |
| 8756 | January |
| 8757 | TEN·AM Desk |
| 8758 | BTC strategy backtester |
| 8759 | February (note: weather_fav.py v2 also defaulted to 8759 — don't run both) |
| 8763 | weather edge v3 |

## Troubleshooting
* **Public link does not open**
  1. Run `./venv/bin/python winter.py tunnel-check`. It tests each step: website answers locally, cloudflared runs,
     URL assigned, connected to Cloudflare, DNS, and the public link fetched from the Pi itself. The first FAIL is the cause.
  2. Every restart gives a **new** URL. Old links/bookmarks are dead. The current one: `cat data/tunnel_url.txt`
     or the website top bar (**✓** = verified from outside, **⚠** = not reachable right now, **…** = being checked).
  3. A brand-new URL needs up to 1–2 minutes. If you open it too early, the phone or router may remember "not found"
     for a while: wait, then try again in a private tab, another browser, or toggle airplane mode.
     `start.sh` only prints the link once it is verified.
  4. Winter checks the link every minute. If it is dead for ~5 minutes, it restarts cloudflared by itself
     (switching to protocol http2, which works on networks that block QUIC) and you get a new URL.
  5. A `~/.cloudflared/config.yml` from another project normally blocks quick tunnels; Winter avoids that with its own empty config.
* **"Winter is not reachable"** on the lock screen: the process is restarting or the Pi is offline (`tmux attach -t Winter`).
* **Port in use**: Winter now says so loudly in the log (with the process holding it) and retries; `./stop.sh` first, or change `port`.
* Logs: `data/logs/winter.log` (full error details), `data/logs/cloudflared.log`, `data/tunnel_status.json`.

## No look-ahead, same code everywhere
Backtest and forward tester call the same `decide()` function. The backtest uses only the last
60-min candle that closed at or before 15:00 local on the day before; settlement results are used
only for scoring.
