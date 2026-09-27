import os
import re
import time
import signal
import secrets
import threading
import subprocess
import json
import urllib.request
import urllib.parse
from threading import Lock
from functools import wraps
from flask import Flask, Response, jsonify, render_template, request, session, redirect, stream_with_context, url_for
import psutil
import requests
import yaml
from urllib.parse import urlparse

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

BASE_DIR = os.path.expanduser("~/bot-server")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
START_TIME = time.time()
HERMES_API_URL = os.environ.get("HERMES_API_URL", "http://127.0.0.1:8642")
HERMES_API_KEY = os.environ.get("HERMES_API_SERVER_KEY", "")
HERMES_MODEL = os.environ.get("HERMES_MODEL", "hermes-agent")
HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
HERMES_ENV_FILE = os.path.join(HERMES_HOME, ".env")
HERMES_PROVIDER_KEYS = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "xai": "XAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "groq": "GROQ_API_KEY",
    "github_copilot": "COPILOT_GITHUB_TOKEN",
    "telegram": "TELEGRAM_BOT_TOKEN",
    "custom": "OPENAI_API_KEY",
    "ninerouter": "OPENAI_API_KEY"
}
ACTIVE_HERMES_REQUESTS = {}
ACTIVE_HERMES_REQUESTS_LOCK = Lock()

def get_hermes_model():
    try:
        config_path = os.path.join(HERMES_HOME, "config.yaml")
        with open(config_path, "r") as config_file:
            config = yaml.safe_load(config_file) or {}
        model_config = config.get("model", {})
        if isinstance(model_config, str):
            model_config = {"default": model_config}
        if not isinstance(model_config, dict):
            model_config = {}
        selected_model = model_config.get("default") or model_config.get("model")
        if isinstance(selected_model, str) and selected_model.strip():
            return selected_model.strip()
    except (OSError, yaml.YAMLError, AttributeError):
        pass
    return HERMES_MODEL

BOTS = {
    "love-whispers": {
        "name": "Love Whispers",
        "icon": "❤️",
        "dir": os.path.join(BASE_DIR, "bots/love-whispers-bot"),
        "entry": "bot.py",
        "pid_path": "/tmp/love-whispers.pid",
        "log_path": "/tmp/love-whispers.log"
    },
    "packtogether": {
        "name": "PackTogether",
        "icon": "🎒",
        "dir": os.path.join(BASE_DIR, "bots/PackTogether"),
        "entry": "packtogether/bot.py",
        "pid_path": "/tmp/packtogether.pid",
        "log_path": "/tmp/packtogether.log"
    }
}

SERVER_SECRET_KEYS = [
    "SERVER_USERNAME",
    "SERVER_PASSWORD",
    "STATUS_BOT_TOKEN",
    "STATUS_CHAT_ID",
    "HERMES_API_SERVER_KEY",
    "HERMES_TELEGRAM_BOT_TOKEN",
    "API_KEY_9ROUTER",
    "TAILSCALE_AUTHKEY",
]

RESTART_COUNTS = {
    "love-whispers": 0,
    "packtogether": 0
}

def load_bot_state():
    state = {"love-whispers": True, "packtogether": True}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    state.update(saved)
        except Exception:
            pass
    return state

def save_bot_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"Failed to save state: {e}")

def set_bot_enabled(bot_key, enabled):
    state = load_bot_state()
    state[bot_key] = bool(enabled)
    save_bot_state(state)

def get_auth_credentials():
    user = os.environ.get("SERVER_USERNAME", "admin")
    pwd = os.environ.get("SERVER_PASSWORD", "admin")
    return user, pwd

def hermes_headers():
    return {
        "Authorization": f"Bearer {HERMES_API_KEY}",
        "Accept": "application/json"
    }

def read_hermes_env():
    values = {}
    if not os.path.isfile(HERMES_ENV_FILE):
        return values
    try:
        with open(HERMES_ENV_FILE, "r") as env_file:
            for line in env_file:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip()
    except OSError:
        pass
    return values

def write_hermes_env(values):
    os.makedirs(HERMES_HOME, mode=0o700, exist_ok=True)
    temp_path = f"{HERMES_ENV_FILE}.tmp"
    with open(temp_path, "w") as env_file:
        for key, value in sorted(values.items()):
            env_file.write(f"{key}={value}\n")
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, HERMES_ENV_FILE)

def valid_provider_url(value):
    parsed = urlparse(value)
    return parsed.scheme in ["http", "https"] and bool(parsed.netloc) and len(value) <= 500

def write_hermes_model(provider, model, base_url=None):
    config_path = os.path.join(HERMES_HOME, "config.yaml")
    config = {}
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r") as config_file:
                config = yaml.safe_load(config_file) or {}
        except (OSError, yaml.YAMLError):
            config = {}
    model_config = config.setdefault("model", {})
    model_config["provider"] = provider
    model_config["default"] = model
    if base_url:
        model_config["base_url"] = base_url.rstrip("/")
    elif provider != "custom":
        model_config.pop("base_url", None)
    temp_path = f"{config_path}.tmp"
    with open(temp_path, "w") as config_file:
        yaml.safe_dump(config, config_file, sort_keys=False)
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, config_path)

def restart_hermes():
    pid_path = "/tmp/hermes.pid"
    old_pid = None
    if os.path.isfile(pid_path):
        try:
            with open(pid_path, "r") as pid_file:
                old_pid = int(pid_file.read().strip())
            os.kill(old_pid, signal.SIGTERM)
            for _ in range(40):
                if not psutil.pid_exists(old_pid):
                    break
                time.sleep(0.25)
            if psutil.pid_exists(old_pid):
                os.kill(old_pid, signal.SIGKILL)
                time.sleep(0.5)
        except (OSError, ValueError):
            pass

    env = os.environ.copy()
    env.update({
        "HERMES_HOME": HERMES_HOME,
        "API_SERVER_ENABLED": "true",
        "API_SERVER_HOST": "127.0.0.1",
        "API_SERVER_PORT": "8642",
        "API_SERVER_KEY": HERMES_API_KEY
    })
    process = subprocess.Popen(
        ["hermes", "gateway"],
        stdout=open("/tmp/hermes.log", "a"),
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True
    )
    with open(pid_path, "w") as pid_file:
        pid_file.write(str(process.pid))
    for _ in range(30):
        try:
            response = requests.get(f"{HERMES_API_URL}/health", timeout=1)
            if response.ok:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise RuntimeError("Hermes did not become ready after restart")

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

try:
    from .assistant import create_assistant_blueprint
    from .assistant_settings import PROVIDERS as SETTINGS_PROVIDERS
except ImportError:
    from assistant import create_assistant_blueprint
    from assistant_settings import PROVIDERS as SETTINGS_PROVIDERS

app.register_blueprint(create_assistant_blueprint(login_required, restart_hermes))


def get_bot_proc(bot_key):
    pid_path = BOTS[bot_key]["pid_path"]
    if os.path.exists(pid_path):
        try:
            with open(pid_path, "r") as f:
                pid = int(f.read().strip())
                if psutil.pid_exists(pid):
                    proc = psutil.Process(pid)
                    if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                        return proc
        except Exception:
            pass
    return None

def get_panel_url():
    if os.path.exists("/tmp/panel_url.txt"):
        try:
            with open("/tmp/panel_url.txt", "r") as f:
                url = f.read().strip()
                if url.startswith("http"):
                    return url
        except Exception:
            pass
    if os.path.exists("/tmp/cloudflared.url"):
        try:
            with open("/tmp/cloudflared.url", "r") as f:
                url = f.read().strip()
                if url.startswith("https://"):
                    return url
        except Exception:
            pass
    return "http://localhost:8080"

def get_ssh_cmd():
    if os.path.exists("/tmp/ssh_cmd.txt"):
        try:
            with open("/tmp/ssh_cmd.txt", "r") as f:
                cmd = f.read().strip()
                if cmd.startswith("ssh"):
                    return cmd
        except Exception:
            pass
    user = os.environ.get("SERVER_USERNAME", "admin")
    return f"ssh {user}@100.x.x.x"

def format_uptime(seconds):
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"

def send_telegram_msg(message, target_chat_id=None):
    token = os.environ.get("STATUS_BOT_TOKEN")
    chat_id = target_chat_id or os.environ.get("STATUS_CHAT_ID")
    if not token or not chat_id:
        return
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }).encode("utf-8")
    
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")

def trigger_github_redeploy(source="User"):
    pat = os.environ.get("GH_PAT")
    repo = os.environ.get("GITHUB_REPO", "ArashAtomic/linux-server")
    ref = os.environ.get("GITHUB_REF_NAME", "main")

    dispatched = False
    if pat and repo:
        url = f"https://api.github.com/repos/{repo}/actions/workflows/server.yml/dispatches"
        payload = json.dumps({"ref": ref}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "Bot-Server-Management-Panel"
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                if res.status in [200, 204]:
                    dispatched = True
        except Exception as e:
            print(f"GitHub API dispatch error: {e}")

    if not dispatched:
        try:
            subprocess.run(["gh", "workflow", "run", "server.yml", "--ref", ref], check=True, timeout=10)
            dispatched = True
        except Exception as e:
            print(f"gh CLI fallback error: {e}")

    if dispatched:
        # Mark trigger file ONLY if dispatch succeeded
        with open("/tmp/redeploy.trigger", "w") as f:
            f.write(f"triggered by {source} at {time.time()}\n")
        msg = f"🔄 <b>Server Redeploy Triggered ({source})</b>\n\nA fresh GitHub Actions runner instance is starting. The current server will shut down shortly."
        send_telegram_msg(msg)
    else:
        msg = f"❌ <b>Redeploy Failed ({source})</b>\n\nCould not trigger new workflow run. Please verify that <code>GH_PAT</code> secret is configured with 'repo' and 'workflow' permissions."
        send_telegram_msg(msg)

    return dispatched

def telegram_poll_worker():
    token = os.environ.get("STATUS_BOT_TOKEN")
    allowed_chat_id = str(os.environ.get("STATUS_CHAT_ID", ""))
    if not token or not allowed_chat_id:
        print("Telegram bot listener skipped: STATUS_BOT_TOKEN or STATUS_CHAT_ID missing.")
        return

    offset = 0
    print("Starting Telegram command listener...")
    while True:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout=20"
            req = urllib.request.Request(url, headers={"User-Agent": "Bot-Server-Listener"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if data.get("ok"):
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    chat = msg.get("chat", {})
                    chat_id = str(chat.get("id", ""))
                    text = msg.get("text", "").strip()

                    if chat_id != allowed_chat_id:
                        send_telegram_msg("🔒 This bot is private.", target_chat_id=chat_id)
                        continue

                    cmd = text.split()[0].lower() if text else ""

                    if cmd in ["/redeploy", "/restart"]:
                        send_telegram_msg("⏳ <b>Triggering Server Redeploy...</b>\nStarting fresh GitHub runner instance with latest repository code.")
                        trigger_github_redeploy(source="Telegram /redeploy")

                    elif cmd in ["/status", "/ping"]:
                        cpu = psutil.cpu_percent(interval=0.2)
                        ram = psutil.virtual_memory()
                        cf_url = get_panel_url()
                        ssh_cmd = get_ssh_cmd()
                        
                        b1 = "🟢 RUNNING" if get_bot_proc("love-whispers") else "🔴 STOPPED"
                        b2 = "🟢 RUNNING" if get_bot_proc("packtogether") else "🔴 STOPPED"
                        
                        status_msg = (
                            f"<b>🖥️ Server Status</b>\n\n"
                            f"⏱ Uptime: {format_uptime(time.time() - START_TIME)}\n"
                            f"💻 CPU: {cpu}%\n"
                            f"🧠 RAM: {round(ram.used/(1024**3), 2)} / {round(ram.total/(1024**3), 2)} GB\n\n"
                            f"<b>Bots:</b>\n"
                            f"❤️ Love Whispers: {b1}\n"
                            f"🎒 PackTogether: {b2}\n\n"
                            f"🌐 <b>Panel:</b> <a href=\"{cf_url}\">{cf_url}</a>\n"
                            f"💻 <b>SSH:</b> <code>{ssh_cmd}</code>"
                        )
                        send_telegram_msg(status_msg)

                    elif cmd == "/panel":
                        cf_url = get_panel_url()
                        send_telegram_msg(f"🌐 <b>Web Control Panel:</b>\n<a href=\"{cf_url}\">{cf_url}</a>")

                    elif cmd in ["/ssh", "/terminal", "/sshx"]:
                        ssh_cmd = get_ssh_cmd()
                        send_telegram_msg(f"💻 <b>SSH Terminal Access:</b>\n<code>{ssh_cmd}</code>")

                    elif cmd in ["/help", "/start"]:
                        help_msg = (
                            "<b>🤖 Server Control Commands:</b>\n\n"
                            "🔄 <code>/redeploy</code> - Trigger fresh workflow run & update server\n"
                            "📊 <code>/status</code> - Current CPU, RAM, and bot health\n"
                            "🌐 <code>/panel</code> - Open Web Management Panel\n"
                            "💻 <code>/ssh</code> - View SSH terminal access command"
                        )
                        send_telegram_msg(help_msg)

        except Exception as e:
            time.sleep(5)

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    expected_user, expected_pass = get_auth_credentials()
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if pwd == expected_pass:
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Invalid password."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/api/status")
@login_required
def status():
    cpu = psutil.cpu_percent(interval=0.2)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    
    cf_url = get_panel_url()
    ssh_cmd = get_ssh_cmd()
    
    bot_status = {}
    online_count = 0
    for key, info in BOTS.items():
        proc = get_bot_proc(key)
        running = proc is not None
        if running:
            online_count += 1
            proc_uptime = format_uptime(time.time() - proc.create_time())
            pid = proc.pid
        else:
            proc_uptime = "—"
            pid = "—"

        bot_status[key] = {
            "name": info["name"],
            "icon": info["icon"],
            "running": running,
            "pid": pid,
            "uptime": proc_uptime,
            "restarts": RESTART_COUNTS[key]
        }

    return jsonify({
        "system": {
            "cpu_percent": cpu,
            "ram_used_gb": round(ram.used / (1024**3), 2),
            "ram_total_gb": round(ram.total / (1024**3), 2),
            "ram_percent": ram.percent,
            "disk_percent": disk.percent,
            "uptime": format_uptime(time.time() - START_TIME),
            "panel_url": cf_url,
            "ssh_command": ssh_cmd
        },
        "fleet": {
            "online": online_count,
            "total": len(BOTS)
        },
        "bots": bot_status
    })

def get_hermes_telegram_bot():
    if os.path.exists("/tmp/hermes_bot.txt"):
        try:
            with open("/tmp/hermes_bot.txt", "r") as f:
                handle = f.read().strip()
                if handle.startswith("@"):
                    return handle
        except OSError:
            pass
    return None

@app.route("/api/assistant/health")
@login_required
def assistant_health():
    if not HERMES_API_KEY:
        return jsonify({"available": False, "error": "Hermes API key is not configured"}), 503
    try:
        response = requests.get(
            f"{HERMES_API_URL}/health",
            headers=hermes_headers(),
            timeout=3
        )
        if not response.ok:
            return jsonify({"available": False, "error": "Hermes health check failed"}), 503
        try:
            version = response.json().get("version")
        except (ValueError, AttributeError):
            version = None
        return jsonify({
            "available": True,
            "model": get_hermes_model(),
            "version": version if isinstance(version, str) else None,
            "telegram_bot": get_hermes_telegram_bot()
        })
    except requests.RequestException:
        return jsonify({"available": False, "error": "Hermes is unavailable"}), 503

@app.route("/api/assistant/providers")
@login_required
def assistant_providers():
    values = read_hermes_env()
    return jsonify({
        "providers": [
            {"name": name, "configured": bool(values.get(env_key))}
            for name, env_key in HERMES_PROVIDER_KEYS.items()
        ]
    })

@app.route("/api/assistant/providers", methods=["POST"])
@login_required
def configure_assistant_provider():
    payload = request.get_json(silent=True) or {}
    provider = payload.get("provider")
    value = payload.get("value")
    model = payload.get("model", "")
    base_url = payload.get("base_url", "")
    if provider not in HERMES_PROVIDER_KEYS or not isinstance(value, str) or not value.strip():
        return jsonify({"error": "Select a supported provider and enter a value"}), 400
    if not isinstance(model, str) or len(model) > 300 or "\n" in model or "\r" in model:
        return jsonify({"error": "Model is invalid"}), 400
    if provider == "ninerouter":
        base_url = "http://127.0.0.1:20128/v1"
    if provider in ["custom", "ninerouter"] and not valid_provider_url(base_url):
        return jsonify({"error": "Custom provider URL must be a valid HTTP or HTTPS URL"}), 400
    if len(value) > 5000 or "\n" in value or "\r" in value:
        return jsonify({"error": "Provider value is invalid"}), 400

    values = read_hermes_env()
    values[HERMES_PROVIDER_KEYS[provider]] = value.strip()
    try:
        write_hermes_env(values)
        if model:
            write_hermes_model(provider, model, base_url if provider in ["custom", "ninerouter"] else None)
            global HERMES_MODEL
            HERMES_MODEL = model
        restart_hermes()
    except (OSError, subprocess.SubprocessError) as error:
        return jsonify({"error": f"Provider saved but Hermes could not restart: {error}"}), 503
    return jsonify({"success": True, "provider": provider, "restarted": True})

def build_model_list_headers(base_url, api_key):
    headers = {"Accept": "application/json"}
    host = (urlparse(base_url).hostname or "").lower()
    if host.endswith("anthropic.com"):
        if api_key:
            headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif host.endswith("googleapis.com"):
        if api_key:
            headers["x-goog-api-key"] = api_key
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers

def extract_model_ids(data):
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list) and isinstance(data, dict):
        items = data.get("models")
    if not isinstance(items, list):
        return []
    models = []
    for item in items:
        model_id = item if isinstance(item, str) else (item.get("id") or item.get("name")) if isinstance(item, dict) else None
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        model_id = model_id.strip()
        if model_id.startswith("models/"):
            model_id = model_id[len("models/"):]
        if model_id not in models:
            models.append(model_id)
    return models

@app.route("/api/assistant/models", methods=["POST"])
@login_required
def fetch_assistant_models():
    payload = request.get_json(silent=True) or {}
    provider = payload.get("provider", "")
    base_url = payload.get("base_url", "")
    api_key = payload.get("api_key", "")
    if not isinstance(base_url, str) or not isinstance(provider, str):
        return jsonify({"error": "Invalid request"}), 400
    if not isinstance(api_key, str) or len(api_key) > 5000 or "\n" in api_key or "\r" in api_key:
        return jsonify({"error": "Provider key is invalid"}), 400
    base_url = base_url.strip().rstrip("/")
    api_key = api_key.strip()

    # Built-in providers use their fixed endpoint. A saved key is only reused for those
    # fixed endpoints, never forwarded to a user-supplied custom URL.
    known = SETTINGS_PROVIDERS.get(provider)
    if known and provider != "custom":
        base_url = known[2].rstrip("/")
        if not api_key:
            api_key = read_hermes_env().get(known[1], "").strip().strip("\"'")
    if not valid_provider_url(base_url):
        return jsonify({"error": "Enter a valid HTTP or HTTPS provider URL"}), 400
    try:
        response = requests.get(
            f"{base_url}/models",
            headers=build_model_list_headers(base_url, api_key),
            timeout=15
        )
        if not response.ok:
            hint = " - check the API key" if response.status_code in (401, 403) else ""
            return jsonify({"error": f"Provider returned HTTP {response.status_code}{hint}"}), 502
        models = extract_model_ids(response.json())
        return jsonify({"models": models[:500]})
    except (requests.RequestException, ValueError):
        return jsonify({"error": "Could not fetch models from provider"}), 502

@app.route("/api/assistant/chat", methods=["POST"])
@login_required
def assistant_chat():
    if not HERMES_API_KEY:
        return jsonify({"error": "Hermes API key is not configured"}), 503

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return jsonify({"error": "messages must be a JSON array"}), 400
    if len(payload["messages"]) > 100:
        return jsonify({"error": "Too many messages"}), 413

    messages = []
    for message in payload["messages"]:
        if not isinstance(message, dict):
            return jsonify({"error": "Invalid message"}), 400
        role = message.get("role")
        content = message.get("content")
        if role not in ["user", "assistant"] or not isinstance(content, str):
            return jsonify({"error": "Invalid message shape"}), 400
        if len(content) > 20000:
            return jsonify({"error": "Message is too large"}), 413
        messages.append({"role": role, "content": content})

    upstream_payload = {
        "model": get_hermes_model(),
        "messages": messages,
        "stream": True
    }
    request_id = request.headers.get("X-Assistant-Request-Id", "")
    if not request_id or len(request_id) > 128:
        return jsonify({"error": "Missing assistant request id"}), 400

    upstream_headers = {**hermes_headers(), "Accept": "text/event-stream", "Content-Type": "application/json"}
    session_id = payload.get("session_id")
    if session_id is not None:
        # Persists the turn in this Hermes session so it shows up in the chat history list.
        if not isinstance(session_id, str) or not re.match(r"^[A-Za-z0-9._:-]{1,128}$", session_id):
            return jsonify({"error": "Invalid session id"}), 400
        upstream_headers["X-Hermes-Session-Id"] = session_id

    try:
        upstream = requests.post(
            f"{HERMES_API_URL}/v1/chat/completions",
            headers=upstream_headers,
            json=upstream_payload,
            stream=True,
            timeout=(5, 90)
        )
    except requests.Timeout:
        return jsonify({"error": "Hermes request timed out while waiting for the gateway"}), 504
    except requests.RequestException:
        return jsonify({"error": "Hermes request failed while connecting to the gateway"}), 502

    if not upstream.ok:
        error_detail = "Hermes rejected the request"
        try:
            upstream_data = upstream.json()
            if isinstance(upstream_data, dict) and upstream_data.get("error"):
                error_value = upstream_data["error"]
                if isinstance(error_value, dict):
                    error_detail = str(error_value.get("message") or error_value.get("code") or error_detail)
                else:
                    error_detail = str(error_value)
        except (ValueError, requests.RequestException):
            pass
        upstream.close()
        return jsonify({"error": error_detail}), 502

    content_type = upstream.headers.get("Content-Type", "").lower()
    if "text/event-stream" not in content_type:
        upstream.close()
        return jsonify({"error": "Hermes returned an unexpected response"}), 502

    with ACTIVE_HERMES_REQUESTS_LOCK:
        ACTIVE_HERMES_REQUESTS[request_id] = upstream

    @stream_with_context
    def relay_events():
        try:
            for line in upstream.iter_lines(decode_unicode=True, chunk_size=1):
                if line:
                    yield f"{line}\n\n"
        finally:
            upstream.close()
            with ACTIVE_HERMES_REQUESTS_LOCK:
                ACTIVE_HERMES_REQUESTS.pop(request_id, None)

    return Response(relay_events(), content_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no"
    })

@app.route("/api/assistant/stop", methods=["POST"])
@login_required
def assistant_stop():
    payload = request.get_json(silent=True) or {}
    request_id = payload.get("request_id", "")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        return jsonify({"error": "Invalid assistant request id"}), 400
    with ACTIVE_HERMES_REQUESTS_LOCK:
        upstream = ACTIVE_HERMES_REQUESTS.get(request_id)
    if upstream:
        upstream.close()
        return jsonify({"stopped": True})
    return jsonify({"stopped": False})

@app.route("/api/server/redeploy", methods=["POST"])
@login_required
def redeploy_server():
    if not trigger_github_redeploy(source="Web Panel"):
        return jsonify({
            "success": False,
            "error": "Could not trigger the workflow. Check that GH_PAT has 'repo' and 'workflow' permissions."
        }), 502
    return jsonify({
        "success": True,
        "message": "Server redeploy initiated. Fresh GitHub runner is launching with the latest code."
    })

@app.route("/api/logs/<bot_key>")
@login_required
def get_logs(bot_key):
    log_paths = {
        "panel": "/tmp/panel.log",
        "hermes": "/tmp/hermes.log",
        "9router": "/tmp/9router.log"
    }
    if bot_key not in BOTS and bot_key not in log_paths:
        return jsonify({"error": "Unknown log target"}), 404

    log_path = log_paths[bot_key] if bot_key in log_paths else BOTS[bot_key]["log_path"]
    try:
        lines = max(1, min(int(request.args.get("lines", 200)), 1000))
    except ValueError:
        return jsonify({"error": "Invalid lines value"}), 400
    
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", errors="replace") as f:
                content = f.readlines()
                return jsonify({"logs": "".join(content[-lines:]), "total_lines": len(content)})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"logs": "Log file not found or empty.", "total_lines": 0})

@app.route("/api/env/<env_key>")
@login_required
def get_env(env_key):
    if env_key == "server-secrets":
        values = {key: os.environ.get(key, "") for key in SERVER_SECRET_KEYS if os.environ.get(key) is not None}
        return jsonify({"env": values, "count": len(values)})

    if env_key not in BOTS:
        return jsonify({"error": "Unknown environment target"}), 404

    env_path = os.path.join(BOTS[env_key]["dir"], ".env")
    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip()
    return jsonify({"env": env_vars, "count": len(env_vars)})

@app.route("/api/bot/<bot_key>/<action>", methods=["POST"])
@login_required
def control_bot(bot_key, action):
    if bot_key not in BOTS:
        return jsonify({"error": "Unknown bot"}), 404
    
    info = BOTS[bot_key]
    proc = get_bot_proc(bot_key)

    if action in ["stop", "restart"]:
        if action == "stop":
            set_bot_enabled(bot_key, False)
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if os.path.exists(info["pid_path"]):
            try:
                os.remove(info["pid_path"])
            except Exception:
                pass
        
        if action == "stop":
            return jsonify({"success": True, "message": f"{info['name']} stopped"})

    if action in ["start", "restart"]:
        set_bot_enabled(bot_key, True)
        if action == "restart":
            RESTART_COUNTS[bot_key] += 1

        venv_python = os.path.join(info["dir"], ".venv/bin/python")
        cmd = f"cd {info['dir']} && nohup {venv_python} -u {info['entry']} > {info['log_path']} 2>&1 & echo $! > {info['pid_path']}"
        subprocess.Popen(cmd, shell=True)
        return jsonify({"success": True, "message": f"{info['name']} started"})

    return jsonify({"error": "Invalid action"}), 400

@app.route("/api/files")
@login_required
def list_files():
    req_path = request.args.get("path")
    if not req_path:
        target_path = BASE_DIR
    else:
        target_path = os.path.abspath(req_path)
    
    allowed_roots = [os.path.expanduser("~"), "/tmp"]
    if not any(target_path.startswith(os.path.abspath(r)) for r in allowed_roots):
        target_path = BASE_DIR

    if not os.path.exists(target_path):
        return jsonify({"error": "Path does not exist"}), 404

    if os.path.isfile(target_path):
        return jsonify({"is_file": True, "path": target_path})

    try:
        entries = []
        raw_items = os.listdir(target_path)
        valid_items = [item for item in raw_items if not item.startswith(".venv") and item != "__pycache__"]
        
        for item in valid_items:
            full_path = os.path.join(target_path, item)
            is_dir = os.path.isdir(full_path)
            size = os.path.getsize(full_path) if not is_dir else 0
            entries.append({
                "name": item,
                "path": full_path,
                "is_dir": is_dir,
                "size_bytes": size
            })
        
        entries.sort(key=lambda x: (0 if x["is_dir"] else 1, x["name"].lower()))
        
        parent = os.path.dirname(target_path)
        return jsonify({
            "is_file": False,
            "current_path": target_path,
            "parent_path": parent if parent != target_path else None,
            "entries": entries
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/file/content")
@login_required
def file_content():
    req_path = request.args.get("path", "")
    target_path = os.path.abspath(req_path)

    allowed_roots = [os.path.expanduser("~"), "/tmp"]
    if not any(target_path.startswith(os.path.abspath(r)) for r in allowed_roots):
        return jsonify({"error": "Access denied"}), 403

    if not os.path.isfile(target_path):
        return jsonify({"error": "File not found"}), 404

    try:
        if os.path.getsize(target_path) > 2 * 1024 * 1024:
            return jsonify({"content": "File too large to preview (>2MB)."}), 400

        with open(target_path, "r", errors="replace") as f:
            content = f.read()
            return jsonify({"content": content, "path": target_path})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    t = threading.Thread(target=telegram_poll_worker, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8080)