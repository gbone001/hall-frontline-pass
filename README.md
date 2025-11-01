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

copy config.example.jsonc config.jsonc   # Windows
# or
cp config.example.jsonc config.jsonc     # macOS / Linux
```

Environment-specific overrides are optional. Create an `.env` only if you need to override values defined in `config.jsonc`.

Configure `config.jsonc`, then start the bot:

```bash
python frontline-pass.py
```

On first launch the bot posts the control panel in your target channel and creates `vip-data.json` (or the path specified by `DATABASE_PATH`).

## Persistent systemd service

The repo ships with a systemd template and helper script so the bot restarts automatically after crashes or reboots.

1. Create the virtual environment and install dependencies (`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`).
2. Adjust `config.jsonc` for your deployment (set `FRONTLINE_CONFIG_PATH` if you keep it outside the repository). Optional runtime overrides are still read from `/opt/hall-frontline-pass/.env` when present.
3. Install the unit and enable it at boot:
   ```bash
   ./manage_frontline_pass.sh install        # copies hall-frontline-pass.service.dist -> /etc/systemd/system/hall-frontline-pass@.service
   ```
   By default the script enables `hall-frontline-pass@$(whoami).service`. Override `BOT_SERVICE_USER` if you run under a different account.
4. Control the bot with the helper script (wraps `systemctl`):
   ```bash
   ./manage_frontline_pass.sh start
   ./manage_frontline_pass.sh status
   ./manage_frontline_pass.sh restart
   ```
   Use `stop` to take it offline, or `run` to execute the bot in the foreground for development (`./manage_frontline_pass.sh run`).

The systemd unit uses `Restart=on-failure`, so systemd automatically relaunches the bot if it exits unexpectedly. `WantedBy=multi-user.target` ensures it starts on every boot.

## Configuration Cheat Sheet

All primary settings live in `config.jsonc` (JSON5 syntax). Environment variables are optional overrides for deployments where you can’t use files (CI, containers, etc.).

| Key | Required | Purpose |
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
| `COMMAND_GUILD_IDS` / `COMMAND_GUILD_ID` | Optional | Comma-separated guild IDs (or a single ID) to sync slash commands instantly to those servers. If unset, commands are synced globally (may take up to ~1 hour to propagate). |
| `CRCON_HTTP_BASE_URL` | Optional | CRCON host (omit `/api`; the bot appends it automatically). |
| `CRCON_HTTP_BEARER_TOKEN` | Optional | Pre-generated CRCON token. |
| `CRCON_HTTP_USERNAME`, `CRCON_HTTP_PASSWORD` | Optional | CRCON login credentials. Supply these instead of a bearer token if you want automatic logins and token refreshes. |
| `CRCON_HTTP_VERIFY` | Optional | `true` by default. Set to `false` when using self-signed certificates. |
| `CRCON_HTTP_TIMEOUT` | Optional | CRCON HTTP timeout in seconds (default `20`). |

The bot validates required settings on startup and exits with a clear error when something is missing or malformed.

### Sample `config.jsonc`

```json5
{
  DISCORD_TOKEN: "your-discord-token",
  CHANNEL_ID: 123456789012345678,
  VIP_DURATION_HOURS: 24,
  LOCAL_TIMEZONE: "Australia/Sydney",

  RCON_HOST: "203.0.113.10",
  RCON_PORT: 21115,
  RCON_PASSWORD: "your-rcon-password",
  RCON_VERSION: 2,

  CRCON_HTTP_BASE_URL: "https://crcon.example.com:8010",
  CRCON_HTTP_BEARER_TOKEN: "",
  CRCON_HTTP_USERNAME: "vipbot",
  CRCON_HTTP_PASSWORD: "supersecret",
  CRCON_HTTP_VERIFY: true,
  CRCON_HTTP_TIMEOUT: 20,

  CRCON_DATABASE_URL: "postgresql://rcon:password@host:5432/rcon",
  DATABASE_PATH: "/data/vip-data.json",
  DATABASE_TABLE: "vip_players",

  MODERATION_CHANNEL_ID: null,
  MODERATOR_ROLE_ID: null,
  ANNOUNCEMENT_MESSAGE_ID: null
}
```

## Bot Experience

1. **Register** - clicking **Register** reminds the user to run the `/register_player` slash command. The command features autocomplete: start typing a player name and it filters live from the CRCON database; pick the correct entry (or the “Use T17 ID …” option) and the bot links your Discord ID. A manual “Enter ID” button is available for edge cases.
2. **Get VIP** - clicking **Get VIP** looks up the user's Discord ID. If a T17 ID is linked, the bot grants VIP via CRCON/RCON and reports the expiry. If not, it reminds the user to register first.

Admins can refresh the message at any time with `/repost_frontline_controls`.

## Deployment Notes

- **Local / bare metal** - run `python frontline-pass.py` under your favorite supervisor (systemd, pm2, tmux). Ensure the working directory is writable so the JSON file can be updated.
- **Railway** - the repo ships with `Procfile` and `railway.toml`. A persistent volume is mounted at `/data` by default; the bot auto-detects Railway mounts and will store the JSON at `/data/vip-data.json` automatically. You can also set `DATABASE_PATH=/data/vip-data.json` explicitly if you prefer. Configure environment variables through the Railway dashboard or CLI.

## Quick Troubleshooting Checklist

Run these from the same host that runs the bot to confirm CRCON connectivity:

1. **Login endpoint**
   ```bash
   curl -sS -X POST https://<crcon-host>:8010/api/login \
     -H 'Content-Type: application/json' \
     -d '{"username":"<user>","password":"<pass>"}'
   ```
   Expect a JSON payload containing a `token`, `jwt`, or `access_token` field.

2. **Authorized VIP grant**
   ```bash
   curl -sS -X POST https://<crcon-host>:8010/api/add_vip \
     -H 'Authorization: Bearer <token>' \
     -H 'Content-Type: application/json' \
     -d '{"player_id":"76561198000000000","description":"frontline-pass","expiration":"2025-11-01T12:00:00Z"}'
   ```
   A 200 response confirms the account has the `api.can_add_vip` permission.

3. **TLS issues?** If either request fails with certificate errors and you trust the endpoint, repeat with `curl -k` and set `CRCON_HTTP_VERIFY=false` in `.env` so the bot also skips certificate validation.

## Requirements

- Python 3.8+
- `discord.py`
- `pytz`
- `python-dotenv`
- `requests` (needed only when CRCON HTTP is enabled)

## License

Frontline Pass is released under the MIT License. See [LICENSE](LICENSE) for details.
