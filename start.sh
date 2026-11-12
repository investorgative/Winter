#!/usr/bin/env bash
# Starts WINTER in tmux session "Winter": forward tester + website :8752 + Cloudflare quick tunnel.
# The loop restarts it if it ever crashes.
cd "$(dirname "$0")"
DIR="$(pwd)"
PY="$DIR/venv/bin/python"
[ -x "$PY" ] || { echo "venv missing - run ./setup.sh first"; exit 1; }
mkdir -p "$DIR/data/logs"
PORT=$("$PY" -c "import json;print(json.load(open('config.json')).get('port',8752))" 2>/dev/null || echo 8752)
if tmux has-session -t Winter 2>/dev/null; then
  echo "tmux session Winter already running (./stop.sh first to restart)"
else
  tmux new-session -d -s Winter "cd '$DIR' && while true; do '$PY' winter.py; echo 'Winter exited - restarting in 10s'; sleep 10; done"
  echo "started tmux session Winter"
fi
echo "website on your network: http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
if command -v cloudflared >/dev/null 2>&1; then
  echo -n "waiting for the public link (checked from outside, up to ~3 min)"
  URL=""; STATE=""
  for i in $(seq 1 90); do
    STATE=$("$PY" -c "import json;s=json.load(open('data/tunnel_status.json'));print(('ok ' if s.get('verified') else 'reg ' if s.get('registered') else 'wait ')+(s.get('url') or ''))" 2>/dev/null)
    case "$STATE" in ok\ *) URL="${STATE#ok }"; break;; esac
    echo -n "."; sleep 2
  done
  echo
  if [ -n "$URL" ]; then
    echo "public URL (verified): $URL"
  else
    echo "public URL not verified yet (${STATE:-no status}). Winter keeps checking and renews a dead link by itself."
    echo "  current link:   cat data/tunnel_url.txt   (also in the website top bar: ✓ = verified)"
    echo "  find the cause: ./venv/bin/python winter.py tunnel-check"
  fi
else
  echo "cloudflared not installed - no public URL (run ./setup.sh)"
fi
