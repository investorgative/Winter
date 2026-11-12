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
  echo -n "waiting for the public tunnel URL"
  for i in $(seq 1 20); do
    URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$DIR/data/logs/cloudflared.log" 2>/dev/null | tail -1)
    [ -n "$URL" ] && break; echo -n "."; sleep 1
  done
  echo
  [ -n "$URL" ] && echo "public URL: $URL" || echo "public URL: not up yet - see the top bar of the website or: cat data/tunnel_url.txt"
else
  echo "cloudflared not installed - no public URL (run ./setup.sh)"
fi
