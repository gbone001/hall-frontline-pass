# Frontline Pass

Frontline Pass is a Discord bot that lets Hell Let Loose players link their Discord account to a T17/Steam ID and self-grant temporary VIP status. Player links live in a lightweight JSON file, the bot prefers the CRCON HTTP API when available, and it always falls back to the in-game RCON `AddVip` command.

## Highlights

- **Self-service VIPs** - players register once and press **Get VIP** whenever they need a slot.
- **Smart transport** - CRCON HTTP (with automatic login/token refresh) first, RCON fallback second.
- **Always-on control panel** - the Discord message survives restarts and can be reposted via `/repost_frontline_controls`.
- **Duplicate protection** - blocks reused T17 IDs and can ping a moderator channel/role.
- **Timezone-aware** - expiry timestamps are rendered in your configured locale.

## Install & Run

```bash
git clone https://github.com/yourusername/hall-frontline-pass.git
cd hall-frontline-pass
pip install -r requirements.txt

copy .env.dist .env   # Windows
# or
cp .env.dist .env     # macOS / Linux
```

Configure `.env`, then start the bot:

```bash
python frontline-pass.py
```

On first launch the bot posts the control panel in your target channel and creates `vip-data.json` (or the path specified by `DATABASE_PATH`).

## Configuration Cheat Sheet

| Variable | Required | Purpose |
| --- | --- | --- |
| `DISCORD_TOKEN` | Yes | Discord bot token. |
| `VIP_DURATION_HOURS` | Yes | Duration of each VIP grant. |
| `CHANNEL_ID` | Yes | Channel hosting the control panel buttons. |
| `LOCAL_TIMEZONE` | Yes | Timezone for human-readable expiry timestamps (e.g. `Australia/Sydney`). |
| `RCON_HOST`, `RCON_PORT`, `RCON_PASSWORD` | Yes | Hell Let Loose RCON V2 connection details. |
| `RCON_VERSION` | Optional | RCON protocol version (default `2`). |
| `DATABASE_PATH` | Optional | JSON file storing Discord <-> T17 links. Leave blank to use `vip-data.json` beside the script (works with Railway volumes). |
| `DATABASE_TABLE` | Optional | Legacy option kept for backwards compatibility; ignored by the JSON backend. |
| `ANNOUNCEMENT_MESSAGE_ID` | Optional | Reuse an existing Discord message for the control panel. |
| `MODERATION_CHANNEL_ID`, `MODERATOR_ROLE_ID` | Optional | Where (and who) to ping when duplicate T17 IDs are detected. |
| `CRCON_HTTP_BASE_URL` | Optional | CRCON host (no `/api` suffix needed; the bot appends it). |
| `CRCON_HTTP_BEARER_TOKEN` | Optional | Pre-generated CRCON token. |
| `CRCON_HTTP_USERNAME`, `CRCON_HTTP_PASSWORD` | Optional | CRCON login credentials. Supply these instead of a bearer token if you want automatic logins and token refreshes. |

The bot validates required settings on startup and exits with a clear error when something is missing or malformed.

### Sample `.env`

```bash
DISCORD_TOKEN=your-discord-bot-token
VIP_DURATION_HOURS=24
CHANNEL_ID=123456789012345678
LOCAL_TIMEZONE=Australia/Sydney

# Storage (JSON)
DATABASE_PATH=/data/vip-data.json
DATABASE_TABLE=vip_players   # ignored, kept for compatibility

# Moderator notifications (optional)
ANNOUNCEMENT_MESSAGE_ID=
MODERATION_CHANNEL_ID=
MODERATOR_ROLE_ID=

# CRCON HTTP API (optional - /api is appended automatically)
CRCON_HTTP_BASE_URL=https://crcon.example.com:8010
CRCON_HTTP_BEARER_TOKEN=
CRCON_HTTP_USERNAME=vipbot
CRCON_HTTP_PASSWORD=supersecret

# RCON
RCON_HOST=203.0.113.10
RCON_PORT=21115
RCON_PASSWORD=your-rcon-password
RCON_VERSION=2
```

## Bot Experience

1. **Register** - clicking **Register** prompts the user for their T17/Steam ID. The bot stores the mapping in JSON, prevents duplicates, and acknowledges success.
2. **Get VIP** - clicking **Get VIP** looks up the user's Discord ID. If a T17 ID is linked, the bot grants VIP via CRCON/RCON and reports the expiry. If not, it reminds the user to register first.

Admins can refresh the message at any time with `/repost_frontline_controls`.

## Deployment Notes

- **Local / bare metal** - run `python frontline-pass.py` under your favorite supervisor (systemd, pm2, tmux). Ensure the working directory is writable so the JSON file can be updated.
- **Railway** - the repo ships with `Procfile` and `railway.toml`. Attach a volume (e.g. `/data`) and set `DATABASE_PATH=/data/vip-data.json` to persist player links between deploys. Configure environment variables through the Railway dashboard or CLI.

## Requirements

- Python 3.8+
- `discord.py`
- `pytz`
- `python-dotenv`
- `requests` (needed only when CRCON HTTP is enabled)

## License

Frontline Pass is released under the MIT License. See [LICENSE](LICENSE) for details.
