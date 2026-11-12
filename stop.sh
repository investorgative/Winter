#!/usr/bin/env bash
# Stops WINTER (tmux session Winter; the tunnel stops with it).
tmux kill-session -t Winter 2>/dev/null && echo "stopped Winter" || echo "Winter not running"
sleep 1
cd "$(dirname "$0")"
if [ -f data/cloudflared.pid ]; then
  PID=$(cat data/cloudflared.pid)
  if [ -r "/proc/$PID/cmdline" ] && tr '\0' ' ' < "/proc/$PID/cmdline" | grep -q cloudflared; then
    kill "$PID" 2>/dev/null && echo "stopped leftover tunnel ($PID)"
  fi
fi
