# Discord VIP Bot

Frontline Pass is a Discord bot that lets players self-serve temporary VIP access on Hell Let Loose servers. Users register their Player-ID once, then press a button whenever they need VIP status. The bot writes Player-IDs to SQLite, talks to the HLL RCON interface (and optionally the CRCON HTTP API), and keeps a persistent Discord message online with live stats.

## Features

- **Self-service registration** with a Discord modal for Steam- or Gamepass Player-IDs.
- **One-click VIP provisioning** that first tries the CRCON HTTP API (if configured) and falls back to the in-game RCON `AddVip` command.
- **Persistent controls**: the control panel survives bot restarts, auto-refreshes after each VIP grant, and exposes an `/repost_frontline_controls` slash command for administrators.
- **Duplicate protection** that blocks reused Player-IDs and (optionally) pings moderators in a dedicated channel.
- **Localized expiries**: expiration timestamps are shown in the timezone of your choice.

## Quick Start

1. **Clone and install**
   ```bash
   git clone https://github.com/yourusername/your-repository.git
   cd your-repository
   pip install -r requirements.txt
   ```
2. **Create configuration**
   ```bash
   copy .env.dist .env  # Windows
   # or
   cp .env.dist .env    # macOS / Linux
   ```
   Fill in the required environment variables (see [Configuration](#configuration)).
3. **Run the bot**
   ```bash
   python frontline-pass.py
   ```

The persistent control embed will appear in the configured channel the first time the bot connects.

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `DISCORD_TOKEN` | ✅ | Discord bot token. |
| `VIP_DURATION_HOURS` | ✅ | Number of hours a VIP grant should last. |
| `CHANNEL_ID` | ✅ | Channel where the persistent control embed should live. |
| `LOCAL_TIMEZONE` | ✅ | IANA timezone (e.g. `Europe/Berlin`) for user-facing timestamps. |
| `RCON_HOST`, `RCON_PORT`, `RCON_PASSWORD` | ✅ | Hell Let Loose RCON connection details. |
| `RCON_VERSION` | Optional | RCON protocol version (default `2`). |
| `DATABASE_PATH` | Optional | SQLite path. Leave blank to auto-select a file (supports Railway volumes). |
| `DATABASE_TABLE` | Optional | Table name for VIP records (default `vip_players`). |
| `ANNOUNCEMENT_MESSAGE_ID` | Optional | Existing Discord message ID to reuse for the control panel. |
| `MODERATION_CHANNEL_ID` | Optional | Channel ID to notify when a duplicate Player-ID is detected. |
| `MODERATOR_ROLE_ID` | Optional | Role to mention in duplicate notifications. |
| `CRCON_HTTP_BASE_URL`, `CRCON_HTTP_BEARER_TOKEN`, `CRCON_HTTP_USERNAME`, `CRCON_HTTP_PASSWORD` | Optional | Enable the CRCON HTTP API `add_vip` endpoint before falling back to RCON. Provide the CRCON base URL (e.g. `https://123.123.123.123:8010`; the `/api` segment is appended automatically) and either a bearer token or a username/password that has the necessary API permissions (the bot logs in and refreshes tokens automatically). |

The bot validates all required settings at startup and exits with a descriptive error message if something is missing or malformed.

### Sample `.env`

```bash
DISCORD_TOKEN=your-discord-bot-token
VIP_DURATION_HOURS=24
CHANNEL_ID=123456789012345678
LOCAL_TIMEZONE=Europe/Berlin

# Storage
DATABASE_PATH=
DATABASE_TABLE=vip_players

# Announcement controls
ANNOUNCEMENT_MESSAGE_ID=

# Moderator notifications
MODERATION_CHANNEL_ID=
MODERATOR_ROLE_ID=

# Optional CRCON HTTP API (host only; /api is appended automatically)
CRCON_HTTP_BASE_URL=https://crcon.example.com:8010
CRCON_HTTP_BEARER_TOKEN=      # Leave empty when using username/password login
CRCON_HTTP_USERNAME=
CRCON_HTTP_PASSWORD=

# RCON
RCON_HOST=127.0.0.1
RCON_PORT=21115
RCON_PASSWORD=your-rcon-password
RCON_VERSION=2
```

## Usage

1. **Register** – users click the **Register** button and provide their Player-ID. The bot stores the value in SQLite.
2. **Request VIP** – pressing **Get VIP** grants temporary VIP access. The status message includes whether the HTTP API or RCON path succeeded.
3. **Monitor** – the persistent embed shows the total number of registered players and the timestamp of the last VIP grant.
4. **Admin tools** – run `/repost_frontline_controls` to delete and recreate the embed (e.g. after pruning messages). The bot also refreshes the embed automatically after every successful VIP grant.

## Deployment on Railway

This repository ships with a `Procfile` and `railway.toml` suited for Railway’s Nixpacks builder.

1. Install the [Railway CLI](https://docs.railway.app/develop/cli) and authenticate with `railway login`, or connect the repository from the dashboard.
2. Provision a new service backed by this repo. Railway auto-installs dependencies from `requirements.txt`.
3. Set environment variables:
   ```bash
   railway variables set DISCORD_TOKEN=... VIP_DURATION_HOURS=4 CHANNEL_ID=... \
     LOCAL_TIMEZONE=Europe/Berlin RCON_HOST=... RCON_PORT=21115 RCON_PASSWORD=...
   ```
   Add optional variables (`RCON_VERSION`, `DATABASE_TABLE`, `DATABASE_PATH`, `ANNOUNCEMENT_MESSAGE_ID`, `MODERATION_CHANNEL_ID`, `MODERATOR_ROLE_ID`, `CRCON_HTTP_*`) as needed.
4. (Recommended) Attach a persistent volume:
   ```bash
   railway volume create frontline-pass-data --mountPath /data
   ```
   When mounted, the bot automatically stores `frontline-pass.db` inside the provided path.
5. Deploy with `railway up` or trigger a deploy from the dashboard. Follow logs with `railway logs`.

## Dependencies

- `discord.py`
- `pytz`
- `python-dotenv`
- `requests` (only needed when using the CRCON HTTP API integration)

## License

Frontline Pass is released under the MIT License. See [LICENSE](LICENSE).
