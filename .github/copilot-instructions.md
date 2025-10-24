## Purpose
Quick on-boarding notes for AI coding agents working on this repository. Focus on the runtime entrypoints, environment configuration, integration points (RCON + MySQL), and common project patterns discovered in the code.

## Where to look first
- `frontline-pass.py` — single-file application and the canonical source of runtime behavior. Read top-to-bottom to understand DB initialization, Discord UI flows, and the RCON client implementation.
- `README.md` — setup and environment variable expectations; reproduces most run instructions.
- `hall-frontline-pass.service.dist` — example systemd unit used in production. Shows how the script is executed on a server.
- `requirements.txt` — pinned runtime dependencies.

## Big picture
- This is a small Discord bot: a single Python process started with `python frontline-pass.py` (entrypoint is `bot.run(DISCORD_TOKEN)`).
- On import the script: 1) reads `.env` via `dotenv`, 2) opens a MySQL connection and ensures the `DATABASE_TABLE` exists, and 3) constructs bot commands/UI. That means database connectivity happens at module-import time — reloading or importing the module in tests will try to connect to MySQL immediately.
- Primary integrations:
  - MySQL (via `mysql-connector-python`) for mapping Discord IDs to player IDs. The table is created with the columns `id, discord_id, steam_id`.
  - Hell Let Loose RCON V2 — custom `RconClient` implemented in the same file. The main RCON command used is `AddVip`.

## Key environment variables (used in `frontline-pass.py`)
- `DISCORD_TOKEN` — Discord bot token (required).
- `VIP_DURATION_HOURS` — numeric, used directly to compute expiration and label text.
- `CHANNEL_ID` — integer channel where the bot posts the interactive message.
- `LOCAL_TIMEZONE` — IANA timezone string (e.g. `Europe/Berlin`), parsed with `pytz`.
- `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_USER`, `DATABASE_PASSWORD`, `DATABASE_NAME`, `DATABASE_TABLE` — MySQL connection and table name. The script creates the table if missing.
- `RCON_HOST`, `RCON_PORT`, `RCON_PASSWORD`, `RCON_VERSION` — RCON connection details. The code expects RCON v2 by default.

Example `.env` contents are in `README.md` — follow that format.

## Runtime & developer workflows
- Install deps: `pip install -r requirements.txt`.
- Run locally: `python frontline-pass.py` (blocks in `bot.run`).
- Service deployment: copy `hall-frontline-pass.service.dist` to `/etc/systemd/system/frontline-pass.service`, update `User` and `WorkingDirectory`, then `systemctl enable --now frontline-pass.service`.

## Important implementation notes for edits
- DB connection is established at module import (`conn = mysql.connector.connect(...)`). To avoid opening a real DB connection in unit tests, refactor to use a factory or lazy initialization (e.g., create and export a `get_db_conn()` function) before writing tests.
- The script executes `CREATE TABLE IF NOT EXISTS ...` at import time. Any change to schema should account for live migrations or move this logic into a small migration helper.
- The `RconClient` is a synchronous, blocking implementation using sockets and a simple XOR-based encryption handshake returned by `ServerConnect`. It's exercised by `grant_vip()` which is run in an executor from the async Discord handler. When changing RCON logic preserve the context manager API (`with RconClient(...) as client:`).
- Errors from RCON are wrapped in `RconError`; the UI catches `RconError` and reports to the user. Keep that exception type stable when changing RCON behavior.

## Patterns and examples in code
- Discord UI: `CombinedView` provides two `@discord.ui.button` handlers — `Register` opens `PlayerIDModal`, `get VIP (...)` reads DB and delegates to `grant_vip()`.
- Storing player IDs example:
  cursor.execute("INSERT INTO `{}` (discord_id, steam_id) VALUES (%s, %s) ON DUPLICATE KEY UPDATE steam_id = %s", (str(interaction.user.id), steam_id, steam_id))
- Time handling: the script computes expiration in the local timezone then converts to UTC for the RCON comment. See `expiration_time_local = local_time + timedelta(hours=VIP_DURATION_HOURS)`.

## Debugging tips
- If the bot fails on startup, check the following in order:
  1. `.env` values — missing `DISCORD_TOKEN` or DB/RCON values cause immediate failures.
  2. MySQL connection — the process opens a connection at import; confirm credentials and that `mysql-connector-python` is installed.
  3. RCON connectivity — RCON errors manifest when users request VIP; use logs around `RconError` and the `logging` module.
- To test UI behavior without a real RCON server: stub `grant_vip()` or monkeypatch `RconClient.add_vip` to return a fake success message. Because `grant_vip` is called via `loop.run_in_executor`, patch the synchronous function used there.

## Small maintenance notes
- Python version: README suggests Python 3.8+. Keep typing and usage compatible with 3.8+.
- Dependencies are minimal and listed in `requirements.txt`.

## When changing behavior
- Preserve the public shapes used by the Discord UI: `grant_vip(player_id, comment) -> str` and `RconError` for failure signaling.
- Avoid long blocking operations on the async event loop — the code currently offloads RCON calls to a thread executor; keep that pattern for any blocking external calls.

## Footer
If anything in these notes is unclear or you want more examples (tests, refactor suggestions, or CI actions), tell me which area to expand and I will iterate.
