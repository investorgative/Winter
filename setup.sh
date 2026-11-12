#!/usr/bin/env bash
# WINTER one-time setup on Raspberry Pi OS (Bookworm or newer). Safe to run again.
set -e
cd "$(dirname "$0")"
echo "== system packages"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip tmux curl
echo "== python venv (separate from your other projects)"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
mkdir -p data/logs data/cache
if [ ! -f config.json ]; then cp config.example.json config.json; echo "== created config.json"; fi
CUR=$(./venv/bin/python -c "import json;print(json.load(open('config.json')).get('passcode',''))")
if [ -z "$CUR" ]; then
  while true; do
    read -r -s -p "Choose the website passcode (4-8 digits, input hidden): " PC; echo
    if [[ "$PC" =~ ^[0-9]{4,8}$ ]]; then break; fi
    echo "digits only, 4 to 8 of them"
  done
  PC="$PC" ./venv/bin/python - <<'PY'
import json, os
c = json.load(open("config.json")); c["passcode"] = os.environ["PC"]
json.dump(c, open("config.json", "w"), indent=2)
PY
  chmod 600 config.json
  echo "== passcode saved in config.json (this file is not in git)"
fi
echo "== cloudflared (free quick tunnel, started by winter.py itself)"
if ! command -v cloudflared >/dev/null 2>&1; then
  ARCH=$(dpkg --print-architecture)
  case "$ARCH" in arm64|amd64|armhf) ;; *) ARCH="";; esac
  if [ -n "$ARCH" ]; then
    curl -fsSL -o /tmp/cloudflared.deb "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb" \
      && sudo dpkg -i /tmp/cloudflared.deb || echo "WARN: cloudflared install failed - website still works on your network"
  else
    echo "WARN: unknown architecture - install cloudflared yourself"
  fi
else
  echo "cloudflared already installed: $(cloudflared --version 2>/dev/null | head -1)"
fi
chmod +x start.sh stop.sh
echo
echo "Setup complete."
echo "  optional: copy your old download cache to skip most of the backtest download:"
echo "            cp -r ~/weather-edge/backtest_cache/. data/cache/"
echo "  1) ./venv/bin/python winter.py backtest     (fills the Backtest section)"
echo "  2) ./start.sh                               (tmux session Winter: forward tester + website + tunnel)"
