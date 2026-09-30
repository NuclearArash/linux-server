# Bot Server & Control Center

Disposable GitHub Actions environment for running Telegram bots with a web-based GUI management panel, Cloudflare-hosted panel access, private SSH access through **Tailscale**, interactive Telegram bot commands, and 24/7 auto-renewing runner architecture.

## Structure

```text
repository/
├── .github/
│   └── workflows/
│       └── server.yml
│
├── panel/
│   ├── app.py
│   └── templates/
│       ├── index.html
│       └── login.html
│
├── scripts/
│   ├── setup.sh
│   ├── start-bots.sh
│   ├── heartbeat.sh
│   └── stop-bots.sh
│
└── README.md
```

## Features

- **Public Bot Fleet**:
  - The server starts with an empty bot registry and no default Telegram bots.
  - Users can add their own bots by providing a GitHub repo URL, branch/ref, entry point, PAT for private repos, and environment variables in `KEY=value` format.
  - Bot installs run in the background with a progress bar and become available in the panel only after the clone + install completes successfully.
  - The registry and encrypted bot credentials are persisted across fresh runner replacements so the fleet survives a disposable VM redeploy.
- **Web Control Panel (Cloudflare Tunnel)**:
  - Real-time CPU & RAM metrics.
  - Bot lifecycle management (Add, Start, Restart, Stop, Remove) from a dynamic fleet list.
  - Password-protected with `SERVER_USERNAME` and `SERVER_PASSWORD`.
  - One-click **🔄 Redeploy Server** button to trigger a fresh GitHub runner with the latest code.
  - Live log streaming with search filter and pause/resume.
  - Environment variables viewer with secret masking toggle.
  - File Explorer for browsing directory contents and viewing source files.
  - **Server Assistant** powered by Hermes Agent, with streamed chat, compact tool activity, Stop control, and curated command autocomplete.
  - Provider setup from the authenticated panel; provider credentials are stored in Hermes state and restored on redeploy.
- **9Router local AI gateway**:
  - Runs privately on `127.0.0.1:20128` using the official `decolua/9router:latest` Docker image.
  - Dashboard: `http://127.0.0.1:20128/dashboard` through SSH/local access.
  - Dashboard password is aligned with `SERVER_PASSWORD`; no separate 9Router password is required.
  - OpenAI-compatible API: `http://127.0.0.1:20128/v1`.
  - Persistent database and configuration are stored in `~/.9router` and restored through the `9router-state-*` Actions cache.
  - Select **9Router (local)** in Server Assistant → Providers, enter the 9Router API key from its dashboard, fetch models, choose one, and save.
- **Private SSH Access (Tailscale)**:
  - Exposes the VM's OpenSSH server on port 22 only through its Tailscale address.
  - Requires Tailscale to be installed and logged into the same tailnet on the client:
    ```bash
    ssh <SERVER_USERNAME>@<tailscale-ip>
    ```
  - Prompts for your `SERVER_PASSWORD` in the terminal before granting shell access.
- **Interactive Telegram Bot Commands**:
  - On boot, sends the Web Panel URL and the SSH command to your Telegram status chat. The startup message's command includes a `-L 20129:127.0.0.1:20128` forward, so the loopback-only 9Router dashboard opens at `http://localhost:20129/dashboard` while that SSH session is connected. `/ssh` and the panel's SSH card still show the plain command.
  - `🔄 /redeploy` or `/restart` - Trigger a fresh GitHub Actions workflow run and update the server instantly.
  - `📊 /status` - Real-time CPU/RAM stats and bot health.
  - `🌐 /panel` - Direct link to the Web Management Panel.
  - `💻 /ssh` - The exact private Tailscale SSH command.
- **Hermes Telegram Control**:
  - Uses a separate bot token so the heartbeat bot and Hermes never compete for Telegram updates.
  - Restricts access to `STATUS_CHAT_ID` and persists the update offset and chat sessions in Hermes state.
  - Supports `/sessions`, `/resume`, `/new`, `/reset`, `/models`, `/model`, `/status`, and normal Hermes chat.
  - Dangerous tool approvals, provider mutation, and advanced tool controls remain disabled until the installed Hermes control API is verified and the approval adapter is enabled.
- **Continuous 24/7 Uptime**:
  - 5-hour and 45-minute runner cycle with automated handoff triggering the next GitHub Actions workflow.

## Remote Access

When the workflow boots:
1. **Cloudflare Tunnel** publishes the Web Control Panel at a temporary `https://<random>.trycloudflare.com` URL.
2. **Tailscale** connects the runner to your tailnet using `TAILSCALE_AUTHKEY` (hostname `bot-server`).
3. **Tailscale** exposes SSH on port 22 through MagicDNS and binds `sshd` to the Tailscale address.
4. The exact SSH command and Cloudflare Panel URL are sent to Telegram and printed in the workflow log.

> **Note**: MagicDNS must be enabled for the advertised `.ts.net` hostname. Tailscale ACLs still control which logged-in tailnet devices may connect.

## GitHub Configuration

### Variables (**Settings → Secrets and variables → Actions → Variables tab**)

| Variable | Description | Default (if unset) |
|---|---|---|
| `SERVER_USERNAME` | Web Panel & SSH login username | `admin` |

### Secrets (**Settings → Secrets and variables → Actions → Secrets tab**)

| Secret | Description | Default (if unset) |
|---|---|---|
| `SERVER_PASSWORD` | Web Panel & SSH login password | `admin` |
| `TAILSCALE_AUTHKEY` | Tailscale auth key used to join the runner to your tailnet | None |
| `GH_PAT` | Personal Access Token used to trigger workflow redeploys | None |
| `STATUS_BOT_TOKEN` | Telegram bot token for status notifications & commands | None |
| `STATUS_CHAT_ID` | Telegram chat ID for notifications & commands | None |
| `HERMES_TELEGRAM_BOT_TOKEN` | Separate Telegram bot token used by the Hermes bridge | Required for Hermes Telegram control |
| `HERMES_API_SERVER_KEY` | Strong bearer key used internally between the panel and Hermes | Required |

User-added bots can optionally provide a GitHub PAT for private repos and a `.env` block in `KEY=value` format through the web panel. Those values are stored encrypted and persisted with the bot registry across redeploys.

### Hermes Provider Setup

Provider API keys are intentionally **not required during workflow setup**. After the server is online:

1. Open the Web Control Panel and select **Server Assistant**.
2. Open **Providers**.
3. Choose a provider, enter its API key or token, and select **Save & Restart Hermes**.

The value is written to the runner's `~/.hermes/.env` with restricted permissions. Hermes state, including configured providers, is saved to and restored from the `hermes-state-*` Actions cache during runner replacement. The API key used by the panel itself remains the separate `HERMES_API_SERVER_KEY` GitHub secret.

Supported provider entries currently include OpenRouter, OpenAI, Anthropic, Google Gemini, xAI, DeepSeek, Groq, GitHub Copilot, and the Hermes Telegram bot token.

The Hermes Telegram bridge token is a transport credential, not an AI provider credential. Create a separate Telegram bot with BotFather and store its token as `HERMES_TELEGRAM_BOT_TOKEN`. Do not reuse `STATUS_BOT_TOKEN`. The bridge currently accepts messages only from `STATUS_CHAT_ID`; other chats are ignored.

Hermes Telegram provider commands currently include:

```text
/provider
/provider set custom https://api.example.com/v1 model-id API_KEY
/models https://api.example.com/v1 API_KEY
/model model-id
```

Credential-bearing messages are deleted on a best-effort basis after processing. Using the authenticated web panel is preferred for long-lived provider keys. A provider HTTP 401 means the upstream endpoint rejected the provider credential; verify the exact base URL, required authentication format, account/model access, and that the key was saved under the provider selected in Hermes. `HERMES_API_SERVER_KEY` is unrelated to upstream provider authentication.

9Router is a local OpenAI-compatible gateway rather than an Hermes provider credential. Configure its upstream providers and API key from the 9Router dashboard, then use the **9Router (local)** provider in the Hermes panel.

## Starting

Go to: **GitHub → Actions → Bot Server → Run workflow**
