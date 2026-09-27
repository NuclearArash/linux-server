#!/usr/bin/env bash
# Configures Hermes' built-in Telegram gateway from GitHub secrets. Run before `hermes gateway`.
#
#   HERMES_TELEGRAM_BOT_TOKEN       bot token from @BotFather (required to enable Telegram)
#   HERMES_TELEGRAM_ALLOWED_USERS   comma-separated numeric Telegram user IDs (default: STATUS_CHAT_ID)
#   HERMES_TELEGRAM_HOME_CHANNEL    chat that receives cron results/alerts (default: first allowed user)
#
# Exit status: 0 = configured, 2 = skipped (missing/invalid settings). It is never fatal for the caller:
# a Telegram problem must not stop the panel, tunnel or SSH from coming up.
set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_BIN="${HERMES_BIN:-hermes}"
ENV_FILE="$HERMES_HOME/.env"

skip() { echo "Telegram: $1 Built-in Telegram left unconfigured."; exit 2; }

# Replaces (or appends) KEY=value in ~/.hermes/.env, keeping comments and other lines. Values never hit argv.
upsert_env() {
    local key="$1" value="$2" tmp
    tmp="$(mktemp "$ENV_FILE.XXXXXX")" || return 1
    if [ -f "$ENV_FILE" ]; then
        grep -v -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$ENV_FILE" > "$tmp" || true
    fi
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$ENV_FILE"
}

token="${HERMES_TELEGRAM_BOT_TOKEN:-}"
[ -n "$token" ] || skip "HERMES_TELEGRAM_BOT_TOKEN is not set."
[[ "$token" =~ ^[0-9]{5,}:[A-Za-z0-9_-]{30,}$ ]] || skip "HERMES_TELEGRAM_BOT_TOKEN does not look like a BotFather token."

allowed="${HERMES_TELEGRAM_ALLOWED_USERS:-}"
if [ -z "$allowed" ]; then
    # In a private chat the chat ID equals the user ID, so the status chat identifies the owner.
    [[ "${STATUS_CHAT_ID:-}" =~ ^[0-9]+$ ]] || skip "no owner user ID: set HERMES_TELEGRAM_ALLOWED_USERS (STATUS_CHAT_ID is missing or not a private chat)."
    allowed="$STATUS_CHAT_ID"
fi
allowed="${allowed// /}"
[[ "$allowed" =~ ^[0-9]+(,[0-9]+)*$ ]] || skip "HERMES_TELEGRAM_ALLOWED_USERS must be comma-separated numeric user IDs."

home_channel="${HERMES_TELEGRAM_HOME_CHANNEL:-${allowed%%,*}}"
[[ "$home_channel" =~ ^-?[0-9]+$ ]] || skip "HERMES_TELEGRAM_HOME_CHANNEL must be a numeric chat ID."

mkdir -p "$HERMES_HOME" && chmod 700 "$HERMES_HOME"
upsert_env TELEGRAM_BOT_TOKEN "$token" || skip "could not write $ENV_FILE."
upsert_env TELEGRAM_ALLOWED_USERS "$allowed"
upsert_env TELEGRAM_HOME_CHANNEL "$home_channel"
upsert_env TELEGRAM_REACTIONS true   # 👀 while working, 👍 when done

# Progressive replies (edit-in-place is the most predictable transport). Some Hermes builds do not
# accept the legacy `gateway.streaming.*` keys, so we skip them instead of hard-failing the startup.
# Keep the Telegram platform config in `~/.hermes/.env` and let Hermes load it on startup.
for setting in "gateway.streaming.enabled true" "gateway.streaming.transport edit"; do
    # shellcheck disable=SC2086
    if ! "$HERMES_BIN" config set $setting >/dev/null 2>&1; then
        echo "Telegram: legacy setting ${setting% *} is unsupported on this Hermes build (continuing)."
    fi
done

# Ensure the API server config is present in the Hermes env file. Newer Hermes builds honor this
# explicitly and fail the health check when it is missing even if the gateway itself starts.
for pair in \
    "API_SERVER_ENABLED=${API_SERVER_ENABLED:-true}" \
    "API_SERVER_HOST=${API_SERVER_HOST:-127.0.0.1}" \
    "API_SERVER_PORT=${API_SERVER_PORT:-8642}" \
    "API_SERVER_KEY=${API_SERVER_KEY:-${HERMES_API_SERVER_KEY:-}}"; do
    key="${pair%%=*}"
    value="${pair#*=}"
    [ -n "$value" ] || continue
    tmp="$(mktemp "$ENV_FILE.XXXXXX")" || continue
    if [ -f "$ENV_FILE" ]; then
        grep -v -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$ENV_FILE" > "$tmp" || true
    fi
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$ENV_FILE"
done

echo "Telegram: built-in adapter configured (owner IDs: $allowed, home channel: $home_channel)."
exit 0
