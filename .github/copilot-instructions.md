## Purpose
Short onboarding notes for AI coding agents working on this repository. Focus on the runtime entrypoint, config, external integrations (CRCON/RCON), and developer workflows you’ll need to modify or run the bot.

### Quick facts
- Single-file Python app: `frontline-pass.py` is the canonical source and runtime entrypoint (it calls `bot.run(DISCORD_TOKEN)`).
- Config: `config.jsonc` (JSON5) is primary; `.env` is read via `dotenv` as overrides. The loader also accepts `FRONTLINE_CONFIG_PATH`.
- Data: player links stored in `vip-data.json` (location resolved by `_resolve_database_path()`).
- Deployment helper: `manage_frontline_pass.sh` and `hall-frontline-pass.service.dist` (systemd template) for production.

### Where to look first
- `frontline-pass.py` — read top-to-bottom. It: loads config, initializes the database (creates table or JSON file), constructs Discord UI, and implements `RconClient` + `grant_vip()`.
- `manage_frontline_pass.sh` / `hall-frontline-pass.service.dist` — how the service is installed and run under systemd (note the `@<user>` template naming).
- `requirements.txt`, `.venv/` usage, and `README.md` for install & deploy notes.

### Integration & runtime notes (concrete)
- CRCON HTTP + RCON fallback: the bot prefers CRCON HTTP API (if configured) and falls back to in-game RCON V2 (`AddVip`). See CRCON keys in `config.jsonc`.
- RCON implementation is synchronous (socket-based); `grant_vip()` runs the blocking call inside an executor — preserve this pattern when refactoring.
- DB behavior: the app may open DB connections at import time. In tests, either refactor to a lazy `get_db_conn()` or monkeypatch DB calls to avoid real MySQL connections.
- Config parsing: uses `json5` so `config.jsonc` accepts unquoted keys and comments — but syntax errors will cause startup to fail (see log message "Loaded configuration from ..." on success).

### Developer workflows & commands (practical)
- Create venv & install deps:
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
- Run in foreground (dev):
  ./manage_frontline_pass.sh run
  or
  .venv/bin/python frontline-pass.py
- Systemd-managed deployment (recommended for production):
  ./manage_frontline_pass.sh install   # installs template and enables hall-frontline-pass@$(whoami)
  ./manage_frontline_pass.sh start|stop|restart|status
- Logs: when running under systemd use `journalctl -u hall-frontline-pass@$(whoami).service`; when running manually check stdout or the log file you redirect to (e.g. /tmp/frontline-pass.log).

### Tests & troubleshooting
- Tests live in `tests/` and use pytest. Because the app initializes external resources at import, run tests with DB/RCON calls stubbed or refactor initialization to be lazy.
- Common failure on startup: missing required config variables — the loader raises a RuntimeError listing missing keys (e.g., `DISCORD_TOKEN is required`). Check `.env` or `config.jsonc` first.

### Useful code references (examples)
- Granting VIP: `grant_vip(player_id, comment) -> str` (used by the Discord UI). Keep the `RconError` exception shape stable for callers.
- Announcement/UI: `CombinedView`, `PlayerIDModal`, and `AnnouncementManager` in `frontline-pass.py` show how the bot reuses and reposts the control message.
- DB insert example: the SQL uses `ON DUPLICATE KEY UPDATE` when storing player links — see the cursor.execute call near the registration flow.

If you want this shortened further or to include small sample snippets/tests, tell me which area to expand (tests, RCON mock examples, or systemd debugging). 
