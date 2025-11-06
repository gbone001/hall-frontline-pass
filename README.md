# Frontline Pass

Frontline Pass is a Discord bot that lets Hell Let Loose players link their Discord account to a T17/Steam ID and self-grant temporary VIP status. Player links live in a lightweight JSON file and VIP grants are issued through the CRCON HTTP API using a bearer token or login credentials.

## Highlights

- **Self-service VIPs** - players register once and press **Get VIP** whenever they need a slot.
- **Moderator assist: /assignvip** - moderators can grant a temporary Discord role to a member so they can access the VIP channel to register and claim; the role is removed automatically after claiming.
- **HTTP transport** - all VIP grants are issued through the CRCON HTTP API (bearer token preferred, login fallback optional).
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
| `DATABASE_PATH` | Optional | JSON file storing Discord <-> T17 links. Leave blank to use `vip-data.json` beside the script (works with Railway volumes). |
| `DATABASE_TABLE` | Optional | Legacy option kept for backwards compatibility; ignored by the JSON backend. |
| `ANNOUNCEMENT_MESSAGE_ID` | Optional | Reuse an existing Discord message for the control panel. |
| `MODERATION_CHANNEL_ID`, `MODERATOR_ROLE_ID` | Optional | Where (and who) to ping when duplicate T17 IDs are detected. |
| `VIP_TEMP_ROLE_ID`, `VIP_CLAIM_CHANNEL_ID` | Optional | Used by `/assignvip`. `VIP_TEMP_ROLE_ID` is a temporary Discord role that grants access to your VIP claim channel. `VIP_CLAIM_CHANNEL_ID` is the channel ID where the control panel lives (falls back to `CHANNEL_ID` if unset). |
| `COMMAND_GUILD_IDS` / `COMMAND_GUILD_ID` | Optional | Comma-separated guild IDs (or a single ID) to sync slash commands instantly to those servers. If unset, commands are synced globally (may take up to ~1 hour to propagate). |
| `CRCON_HTTP_BASE_URL` | Yes | CRCON host (omit `/api`; the bot appends it automatically). |
| `CRCON_HTTP_BEARER_TOKEN` | Yes\* | Pre-generated CRCON token. Required unless you supply username/password. |
| `CRCON_HTTP_USERNAME`, `CRCON_HTTP_PASSWORD` | Conditional | CRCON login credentials. Provide both instead of a bearer token if you want automatic logins and token refreshes. |
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

  CRCON_HTTP_BASE_URL: "https://crcon.example.com:8010",
  CRCON_HTTP_BEARER_TOKEN: "your-pre-generated-token",
  # Alternatively provide username/password instead of a bearer token:
  # CRCON_HTTP_USERNAME: "vipbot",
  # CRCON_HTTP_PASSWORD: "supersecret",
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

1. **Get VIP** - clicking **Get VIP** opens a modal that collects the player's T17/Steam ID. The bot stores the value and issues the VIP grant through the CRCON HTTP API, then reports the expiry time back to the user. The previously saved ID is pre-filled for future requests.

Admins can refresh the message at any time with `/repost_frontline_controls`.

### New: Moderator flow with `/assignvip`

1. A moderator runs `/assignvip` and selects a member from the server-wide autocomplete picker.
2. The bot assigns the `VIP_TEMP_ROLE_ID` role to that member and points them to `VIP_CLAIM_CHANNEL_ID` (or `CHANNEL_ID`).
3. The member presses **Get VIP**, pastes their Player-ID, and submits the modal.
4. After a successful claim, the bot automatically removes the temporary Discord role so access reverts to normal.

## Deployment Notes

- **Local / bare metal** - run `python frontline-pass.py` under your favorite supervisor (systemd, pm2, tmux). Ensure the working directory is writable so the JSON file can be updated.
- **Railway** - the repo ships with `Procfile`, `railway.toml`, and a Dockerfile. Railway builds with the Dockerfile (set in `railway.toml`) and runs `python frontline-pass.py`. A persistent volume is mounted at `/data` by default; the bot auto-detects Railway mounts and will store the JSON at `/data/vip-data.json` automatically. Set the required variables (see `.env.dist`) in the Railway dashboard/CLI before deploying—at minimum `DISCORD_TOKEN`, `VIP_DURATION_HOURS`, `CHANNEL_ID`, `LOCAL_TIMEZONE`, `CRCON_HTTP_BASE_URL`, and either `CRCON_HTTP_BEARER_TOKEN` or (`CRCON_HTTP_USERNAME` + `CRCON_HTTP_PASSWORD`).

## Quick Troubleshooting Checklist

Run these commands from a trusted host to confirm CRCON HTTP connectivity:

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
- `requests`

## License

Frontline Pass is released under the MIT License. See [LICENSE](LICENSE) for details.
