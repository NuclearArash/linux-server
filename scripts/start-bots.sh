#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$HOME/bot-server"
BOT_DIR="$BASE_DIR/bots"
PANEL_DIR="$BASE_DIR/panel"
STATE_FILE="$BASE_DIR/state.json"
BOT_REGISTRY_PATH="${BOT_REGISTRY_PATH:-$BASE_DIR/bot-registry.json}"
BOT_CREDENTIALS_KEY_PATH="${BOT_CREDENTIALS_KEY_PATH:-$BASE_DIR/.bot_credentials_key}"
HERMES_HOME_DIR="$HOME/.hermes"
NINEROUTER_HOME_DIR="$HOME/.9router"

notify_status_failure() {
    local MESSAGE="$1"
    if [ -n "${STATUS_BOT_TOKEN:-}" ] && [ -n "${STATUS_CHAT_ID:-}" ]; then
        curl -sS --max-time 15 -X POST \
            "https://api.telegram.org/bot${STATUS_BOT_TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${STATUS_CHAT_ID}" \
            --data-urlencode "text=${MESSAGE}" \
            --data-urlencode "parse_mode=HTML" >/dev/null 2>&1 || true
    fi
}

start_registry_bots() {
    if [ ! -f "$BOT_REGISTRY_PATH" ] || [ ! -d "$BOT_DIR" ]; then
        echo "No bot registry found; skipping automatic bot startup."
        return 0
    fi

    export BOT_REGISTRY_PATH
    export BOT_CREDENTIALS_KEY_PATH
    export BOT_CREDENTIALS_KEY="$(tr -d '\n' < "$BOT_CREDENTIALS_KEY_PATH" 2>/dev/null || true)"

    "$PANEL_DIR/.venv/bin/python" - "$BOT_REGISTRY_PATH" "$BOT_DIR" "$BOT_CREDENTIALS_KEY_PATH" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

registry_path = Path(sys.argv[1])
bots_dir = Path(sys.argv[2])
key_path = Path(sys.argv[3])

try:
    registry = json.loads(registry_path.read_text(encoding='utf-8')) if registry_path.exists() else {"bots": []}
except Exception:
    registry = {"bots": []}

key = key_path.read_text(encoding='utf-8').strip() if key_path.exists() else ""
if not key:
    raise SystemExit(0)

cipher = Fernet(key.encode('ascii'))
for definition in registry.get("bots", []):
    if not definition.get("enabled"):
        continue
    bot_id = definition.get("id")
    if not bot_id:
        continue

    directory_name = definition.get("directory_name") or bot_id
    if (
        not isinstance(directory_name, str)
        or directory_name in {".", ".."}
        or any(character in directory_name for character in '/\\\0')
    ):
        continue

    repo_dir = bots_dir / directory_name
    if repo_dir.is_symlink() or bots_dir.resolve() not in repo_dir.resolve().parents:
        continue
    if not repo_dir.is_dir():
        continue

    entrypoint = definition.get("entrypoint", "")
    if not entrypoint:
        continue

    entry_path = (repo_dir / entrypoint).resolve()
    if not entry_path.is_file():
        continue

    venv_python = repo_dir / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not venv_python.exists():
        continue

    pid_path = Path("/tmp") / f"bot-{bot_id}.pid"
    if pid_path.exists():
        continue

    env = os.environ.copy()
    env["PATH"] = os.pathsep.join((str(venv_python.parent), env.get("PATH", "")))
    env["HOME"] = os.path.expanduser("~")
    env["USER"] = os.environ.get("USER", "runner")
    env["LANG"] = "C.UTF-8"
    env["VIRTUAL_ENV"] = str(venv_python.parent.parent)

    encrypted = registry.get("encrypted_credentials", {}).get(bot_id)
    if encrypted:
        try:
            credentials = json.loads(cipher.decrypt(encrypted.encode('ascii')).decode('utf-8'))
            env.update(credentials.get("environment", {}))
        except (InvalidToken, ValueError, TypeError, UnicodeDecodeError):
            pass

    log_path = Path("/tmp") / f"bot-{bot_id}.log"
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            [str(venv_python), '-u', str(entry_path)],
            cwd=str(repo_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_path.write_text(str(process.pid), encoding='ascii')
    print(f"Started bot {bot_id} with PID {process.pid}")
PY
}

echo "======================================"
echo "Starting bot fleet, Panel, Cloudflare Tunnel, and private Tailscale SSH"
echo "======================================"

start_registry_bots

# Hermes Agent API and gateway
echo
echo "==> Starting Hermes Agent"
export PATH="$HOME/.local/bin:$HOME/.hermes/bin:$PATH"
export HERMES_HOME="$HERMES_HOME_DIR"
export API_SERVER_ENABLED="true"
export API_SERVER_HOST="127.0.0.1"
export API_SERVER_PORT="8642"
export API_SERVER_KEY="${HERMES_API_SERVER_KEY:-}"
export HERMES_API_SERVER_KEY="$API_SERVER_KEY"

mkdir -p "$HERMES_HOME_DIR" && chmod 700 "$HERMES_HOME_DIR"
if [ -f "$HERMES_HOME_DIR/.env" ]; then
    tmp="$(mktemp "$HERMES_HOME_DIR/.env.XXXXXX")"
    grep -v -E '^(API_SERVER_ENABLED|API_SERVER_HOST|API_SERVER_PORT|API_SERVER_KEY|HERMES_API_SERVER_KEY)=' "$HERMES_HOME_DIR/.env" > "$tmp" || true
    printf 'API_SERVER_ENABLED=true\nAPI_SERVER_HOST=127.0.0.1\nAPI_SERVER_PORT=8642\nAPI_SERVER_KEY=%s\nHERMES_API_SERVER_KEY=%s\n' "$API_SERVER_KEY" "$API_SERVER_KEY" >> "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$HERMES_HOME_DIR/.env"
else
    printf 'API_SERVER_ENABLED=true\nAPI_SERVER_HOST=127.0.0.1\nAPI_SERVER_PORT=8642\nAPI_SERVER_KEY=%s\nHERMES_API_SERVER_KEY=%s\n' "$API_SERVER_KEY" "$API_SERVER_KEY" > "$HERMES_HOME_DIR/.env"
    chmod 600 "$HERMES_HOME_DIR/.env"
fi

if [ -z "$API_SERVER_KEY" ]; then
    echo "ERROR: HERMES_API_SERVER_KEY is not configured."
    notify_status_failure "<b>Hermes startup failed</b> - API_SERVER_KEY is not configured."
    exit 1
fi
if ! command -v hermes >/dev/null 2>&1; then
    echo "ERROR: Hermes Agent is not installed."
    notify_status_failure "<b>Hermes startup failed</b> - Hermes Agent is not installed."
    exit 1
fi

TELEGRAM_RC=0
"$SCRIPT_DIR/configure-hermes-telegram.sh" || TELEGRAM_RC=$?
if [ "$TELEGRAM_RC" -eq 0 ]; then
    TELEGRAM_ME="$(curl -sS --max-time 10 "https://api.telegram.org/bot${HERMES_TELEGRAM_BOT_TOKEN}/getMe" 2>/dev/null || true)"
    TELEGRAM_BOT_USERNAME="$(printf '%s' "$TELEGRAM_ME" | jq -r 'select(.ok == true) | .result.username // empty' 2>/dev/null || true)"
    if [ -n "$TELEGRAM_BOT_USERNAME" ]; then
        echo "Hermes Telegram bot: @$TELEGRAM_BOT_USERNAME"
        printf '@%s' "$TELEGRAM_BOT_USERNAME" > /tmp/hermes_bot.txt
    else
        echo "WARNING: Telegram rejected HERMES_TELEGRAM_BOT_TOKEN; the Hermes bot will not connect."
        notify_status_failure "<b>Hermes Telegram is not connected</b> - Telegram rejected HERMES_TELEGRAM_BOT_TOKEN (check the secret and @BotFather). The server itself started normally."
    fi
elif [ -n "${HERMES_TELEGRAM_BOT_TOKEN:-}" ]; then
    notify_status_failure "<b>Hermes Telegram is not configured</b> - HERMES_TELEGRAM_BOT_TOKEN is set but the owner user ID is missing or invalid (set HERMES_TELEGRAM_ALLOWED_USERS). The server itself started normally."
fi
unset HERMES_TELEGRAM_BOT_TOKEN HERMES_TELEGRAM_ALLOWED_USERS HERMES_TELEGRAM_HOME_CHANNEL

nohup env -i PATH="$PATH" HOME="$HOME" USER="${USER:-$(id -un)}" LANG="C.UTF-8" \
    HERMES_HOME="$HERMES_HOME" API_SERVER_ENABLED="$API_SERVER_ENABLED" \
    API_SERVER_HOST="$API_SERVER_HOST" API_SERVER_PORT="$API_SERVER_PORT" \
    API_SERVER_KEY="$API_SERVER_KEY" bash "$PANEL_DIR/run_hermes_gateway.sh" > /tmp/hermes.log 2>&1 &
HERMES_PID=$!
echo "$HERMES_PID" > /tmp/hermes.pid

echo "Waiting for Hermes API..."
HERMES_READY=false
for i in {1..30}; do
    if curl -fsS --max-time 2 http://127.0.0.1:8642/health >/dev/null 2>&1; then
        HERMES_READY=true
        break
    fi
    if ! kill -0 "$HERMES_PID" 2>/dev/null; then
        echo "ERROR: Hermes exited during startup."
        tail -n 40 /tmp/hermes.log || true
        notify_status_failure "<b>Hermes startup failed</b> - Gateway exited before the API became ready. See /tmp/hermes.log in the workflow artifact."
        exit 1
    fi
    sleep 1
done
if [ "$HERMES_READY" != "true" ]; then
    echo "ERROR: Hermes API did not become ready within 30 seconds."
    tail -n 40 /tmp/hermes.log || true
    notify_status_failure "<b>Hermes startup timeout</b> - The API did not become ready within 30 seconds. See /tmp/hermes.log in the workflow artifact."
    exit 1
fi
echo "Hermes Agent PID: $HERMES_PID (API 127.0.0.1:8642)"

echo
echo "==> Starting 9Router"
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: Docker is required to run 9Router."
    exit 1
fi
if ! docker info >/dev/null 2>&1 && ! sudo -n docker info >/dev/null 2>&1; then
    echo "ERROR: Docker daemon is not available for 9Router."
    exit 1
fi

DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker)
fi

> /tmp/9router.log
"${DOCKER[@]}" pull decolua/9router:latest >> /tmp/9router.log 2>&1
"${DOCKER[@]}" rm -f 9router >> /tmp/9router.log 2>&1 || true
"${DOCKER[@]}" run -d --name 9router --restart unless-stopped \
    -p 127.0.0.1:20128:20128 \
    -v "$NINEROUTER_HOME_DIR:/app/data" \
    --env-file "$NINEROUTER_HOME_DIR/.env" \
    -e DATA_DIR=/app/data -e PORT=20128 -e HOSTNAME=0.0.0.0 \
    decolua/9router:latest >> /tmp/9router.log 2>&1

NINEROUTER_READY=false
for i in {1..30}; do
    HTTP_STATUS=$(curl -sS --max-time 2 -o /dev/null -w '%{http_code}' http://127.0.0.1:20128/dashboard 2>/dev/null || true)
    if [[ "$HTTP_STATUS" =~ ^[234][0-9][0-9]$ ]]; then
        NINEROUTER_READY=true
        break
    fi
    sleep 1
done
if [ "$NINEROUTER_READY" != "true" ]; then
    echo "ERROR: 9Router did not become ready within 30 seconds."
    "${DOCKER[@]}" logs --tail 40 9router >> /tmp/9router.log 2>&1 || true
    echo "9Router diagnostics:"
    tail -n 60 /tmp/9router.log || true
    exit 1
fi
echo "9Router ready at http://127.0.0.1:20128 (dashboard /dashboard, API /v1)"
MODEL_STATUS=$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:20128/v1/models 2>/dev/null || true)
echo "9Router model endpoint HTTP status: ${MODEL_STATUS:-unavailable}"

NINEROUTER_RESET=$("${DOCKER[@]}" exec 9router node -e \
    "fetch('http://127.0.0.1:20128/api/auth/reset-password',{method:'POST'}).then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" \
    >/dev/null 2>&1; echo $?)
if [ "$NINEROUTER_RESET" -eq 0 ]; then
    echo "9Router dashboard password reset to the configured server password."
else
    echo "WARNING: Could not reset the 9Router dashboard password from inside the container."
fi

echo
echo "==> Configuring OpenSSH Server"
sudo sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config 2>/dev/null || true
sudo sed -i 's/PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config 2>/dev/null || true
sudo systemctl restart ssh || sudo service ssh restart || true

SSH_USER="${SERVER_USERNAME:-admin}"
SSH_PASS="${SERVER_PASSWORD:-admin}"

echo "==> Configuring SSH User: $SSH_USER"
sudo useradd -m -s /bin/bash "$SSH_USER" 2>/dev/null || true
echo "$SSH_USER:$SSH_PASS" | sudo chpasswd
sudo usermod -aG sudo "$SSH_USER" 2>/dev/null || true
echo "$SSH_USER ALL=(ALL) NOPASSWD:ALL" | sudo tee "/etc/sudoers.d/$SSH_USER" >/dev/null

echo
echo "==> Starting Management Panel GUI"

cd "$PANEL_DIR"
source .venv/bin/activate

export SERVER_USERNAME="$SSH_USER"
export SERVER_PASSWORD="$SSH_PASS"
export GH_PAT="${GH_PAT:-}"
export STATUS_BOT_TOKEN="${STATUS_BOT_TOKEN:-}"
export STATUS_CHAT_ID="${STATUS_CHAT_ID:-}"
export GITHUB_REPO="${GITHUB_REPO:-ArashAtomic/linux-server}"
export GITHUB_REF_NAME="${GITHUB_REF_NAME:-main}"
export BOT_REGISTRY_PATH
export BOT_CREDENTIALS_KEY_PATH
export BOT_CREDENTIALS_KEY="$(tr -d '\n' < "$BOT_CREDENTIALS_KEY_PATH")"

nohup python -u app.py > /tmp/panel.log 2>&1 &
PANEL_PID=$!
echo "$PANEL_PID" > /tmp/panel.pid
deactivate

echo "Management Panel PID: $PANEL_PID (Port 8080)"

echo
echo "==> Starting Cloudflare Tunnel for Web Panel"
rm -f /tmp/cloudflared.url /tmp/panel_url.txt /tmp/ssh_cmd.txt /tmp/cloudflared.log
CF_PID=""
for attempt in 1 2 3; do
    echo "Starting Cloudflare Quick Tunnel (attempt $attempt/3)..."
    nohup cloudflared tunnel --edge-ip-version 4 --protocol http2 \
        --url http://127.0.0.1:8080 >> /tmp/cloudflared.log 2>&1 &
    CF_PID=$!
    echo "$CF_PID" > /tmp/cloudflared.pid

    echo "Waiting for Cloudflare Panel URL..."
    for i in {1..45}; do
        if [ ! -s /tmp/cloudflared.url ]; then
            grep -Eo 'https://[-a-zA-Z0-9]+\.trycloudflare\.com/?' /tmp/cloudflared.log \
                | grep -Ev '^https://api\.trycloudflare\.com/?$' \
                | head -n 1 > /tmp/cloudflared.url || true
        fi
        if [ -s /tmp/cloudflared.url ]; then
            break 2
        fi
        if ! kill -0 "$CF_PID" 2>/dev/null; then
            echo "Cloudflared exited before publishing a URL on attempt $attempt."
            break
        fi
        sleep 1
    done
    kill "$CF_PID" 2>/dev/null || true
    sleep 2
done

if [ -s /tmp/cloudflared.url ]; then
    cp /tmp/cloudflared.url /tmp/panel_url.txt
else
    echo "WARNING: Cloudflare Tunnel URL was not discovered."
    echo "Cloudflare URL unavailable; inspect /tmp/cloudflared.log" > /tmp/panel_url.txt
fi

echo
echo "==> Connecting Tailscale"
if [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
    sudo tailscale up --authkey="$TAILSCALE_AUTHKEY" --hostname="bot-server" --accept-routes || true
else
    echo "WARNING: TAILSCALE_AUTHKEY is not configured."
fi

sudo systemctl disable --now tailscale-funnel.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/tailscale-funnel.service
sudo systemctl daemon-reload 2>/dev/null || true
sudo tailscale funnel off 2>/dev/null || true

tailscale status || true
TS_DOMAIN=$(tailscale status --json 2>/dev/null | jq -r '.Self.DNSName // empty' | sed 's/\.$//' || true)
if [ -z "$TS_DOMAIN" ]; then
    TS_DOMAIN=$(tailscale status 2>/dev/null | grep -v '#' | awk 'NR==1 {print $2}' || true)
fi

echo "Tailscale Domain: ${TS_DOMAIN:-unknown}"

TS_IP=$(tailscale ip -4 2>/dev/null | head -n 1 || true)
if [ -n "$TS_IP" ]; then
    printf 'ListenAddress %s\n' "$TS_IP" | sudo tee /etc/ssh/sshd_config.d/tailscale.conf >/dev/null
    if sudo sshd -t; then
        sudo systemctl restart ssh || sudo service ssh restart || true
        echo "SSH is listening on Tailscale address $TS_IP only."
    else
        echo "WARNING: Invalid SSH configuration; restoring unrestricted SSH listener."
        sudo rm -f /etc/ssh/sshd_config.d/tailscale.conf
        sudo systemctl restart ssh || sudo service ssh restart || true
    fi
else
    echo "WARNING: Tailscale IPv4 address unavailable; SSH listener was not restricted."
fi

echo
echo "==> Tailscale Status:"
tailscale status || true

if [ -n "$TS_IP" ]; then
    SSH_CMD="ssh $SSH_USER@$TS_IP"
    echo "$SSH_CMD" > /tmp/ssh_cmd.txt
else
    SSH_CMD="SSH unavailable: Tailscale is not connected"
    echo "$SSH_CMD" > /tmp/ssh_cmd.txt
fi

echo
echo "======================================"
echo "Processes, Cloudflare Tunnel, and private Tailscale SSH Active"
echo "======================================"

echo "Web Panel URL : $(cat /tmp/panel_url.txt)"
echo "SSH Command   : $(cat /tmp/ssh_cmd.txt)"

sleep 2

echo
echo "Management Panel:"
ps -p "$PANEL_PID" -o pid,etime,cmd || true
