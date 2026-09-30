#!/usr/bin/env bash

set +e

echo "======================================"
echo "Stopping server processes and Cloudflare Tunnel"
echo "======================================"

stop_process() {
    NAME="$1"
    PID_FILE="$2"

    if [ ! -f "$PID_FILE" ]; then
        echo "$NAME: PID file not found"
        return
    fi

    PID=$(cat "$PID_FILE")

    if kill -0 "$PID" 2>/dev/null; then
        echo "$NAME: stopping PID $PID"

        kill "$PID" 2>/dev/null || true

        for i in {1..10}; do
            if ! kill -0 "$PID" 2>/dev/null; then
                echo "$NAME: stopped gracefully"
                rm -f "$PID_FILE"
                return
            fi

            sleep 1
        done

        echo "$NAME: did not stop gracefully, forcing termination"

        kill -9 "$PID" 2>/dev/null || true
    else
        echo "$NAME: already stopped"
    fi

    rm -f "$PID_FILE"
}

for pid_file in /tmp/bot-*.pid; do
    if [ -f "$pid_file" ]; then
        stop_process "Dynamic Bot" "$pid_file"
    fi
done

stop_process "Hermes Agent (supervisor)" "/tmp/hermes.pid"
pkill -TERM -f "hermes gateway" 2>/dev/null || true
stop_process "Management Panel" "/tmp/panel.pid"
stop_process "Cloudflare Tunnel" "/tmp/cloudflared.pid"

if command -v docker >/dev/null 2>&1; then
    docker stop 9router >/dev/null 2>&1 || sudo -n docker stop 9router >/dev/null 2>&1 || true
fi

rm -f /tmp/panel_url.txt /tmp/cloudflared.url /tmp/ssh_cmd.txt /tmp/cloudflared.log /tmp/hermes_bot.txt /tmp/bot-*.pid /tmp/bot-*.log

echo
echo "All processes stopped."
