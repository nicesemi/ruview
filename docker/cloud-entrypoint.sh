#!/bin/sh
# Cloud entrypoint for π RuView.
#
# Starts the sensing WebSocket server (ws_server.py, upstream on :8765) and the
# HTTP gateway ($PORT, serves ui/ and shims /api/v1/*). One container, one
# exposed port — the shape every free PaaS (Render / Koyeb / Cloud Run / Zeabur)
# expects.
#
# No ESP32 hardware is reachable inside a PaaS container (no inbound UDP), so
# ws_server auto-detects as: ESP32 UDP (absent) > platform WiFi (absent) >
# simulated collector. The UI therefore runs on clearly-labelled synthetic CSI
# frames — enough to demo the whole visualisation stack.
set -eu

PORT="${PORT:-8080}"
export PORT
export UI_DIR="${UI_DIR:-/app/ui}"

echo "[cloud] starting sensing ws_server on :8765 (upstream for gateway)"
python -m v1.src.sensing.ws_server &
WS_PID=$!

# Wait for the upstream socket before letting the gateway dial it.
i=0
while [ "$i" -lt 60 ]; do
    if python -c "import socket,sys; socket.create_connection(('127.0.0.1',8765),1).close()" 2>/dev/null; then
        echo "[cloud] upstream ready after ${i}s"
        break
    fi
    i=$((i + 1))
    sleep 1
done

# Make sure a container stop terminates the sensing server too.
trap 'kill "$WS_PID" 2>/dev/null || true' INT TERM EXIT

echo "[cloud] starting gateway on :$PORT"
exec python /app/gateway.py
