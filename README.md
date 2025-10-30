# Discord VIP Bot

This Discord bot allows users to register their player ID (Steam or Gamepass) and request temporary VIP status for a certain amount of time on connected Hell Let Loose servers. The bot connects directly to the Hell Let Loose RCON V2 interface to manage VIP status for players and stores player information in a local SQLite database.

ToDo:
Execute the following commands after downloading:
1. Copy the `.env.dist` file to `.env` and enter your values.
2. Run the command `pip install python-dotenv`.
3. Copy `frontline-pass.service.dist` to `/etc/systemd/system/frontline-pass.service`
4. Activate and start the service with `sudo systemctl enable frontline-pass.service` and `sudo systemctl start frontline-pass.service`.

## Features

- **Player Registration**: Users can register their player ID (Steam-ID or Gamepass-ID) through a modal window.
- **VIP Request**: Users can request VIP status for a predefined number of hours. The bot communicates with the Hell Let Loose RCON V2 protocol to grant VIP status.
- **Persistent Player Data**: Player information is stored in a SQLite database, allowing for easy retrieval and VIP management.
- **Duplicate Protection**: Attempts to reuse an existing Player-ID trigger an in-app warning and notify moderators so they can investigate.
- **Localized Time Support**: The bot handles time zone conversion to display VIP expiration times in local time (set to Europe/Berlin by default).
  
## Prerequisites

- Python 3.8+
- Discord account and server where the bot will be used
- Access to the Hell Let Loose RCON V2 endpoint (hostname/IP, port, and RCON password)
- Local filesystem access to store the SQLite database file
- `.env` file with the following keys:
  - `DISCORD_TOKEN`: Your Discord bot token
  - `VIP_DURATION_HOURS`: Duration of the VIP status in hours
  - `CHANNEL_ID`: The ID of the Discord channel where the bot will post the initial message
  - `LOCAL_TIMEZONE`: IANA time zone identifier used for display (e.g. `Europe/Berlin`)
  - `DATABASE_PATH`: (Optional) SQLite file path. Leave blank to auto-select a path (including Railway volumes)
  - `DATABASE_TABLE`: (Optional) Name of the SQLite table (defaults to `vip_players`)
  - `ANNOUNCEMENT_MESSAGE_ID`: (Optional) Existing message ID to re-use on startup
  - `MODERATION_CHANNEL_ID`: (Optional) Channel to notify when duplicate Player-IDs are attempted
  - `MODERATOR_ROLE_ID`: (Optional) Role to @mention when sending moderator notifications
  - `RCON_HOST`, `RCON_PORT`, `RCON_PASSWORD` (and optionally `RCON_VERSION`): Hell Let Loose RCON V2 connection details
  
The bot validates these settings on startup and exits with a clear error message if any required value is missing or invalid.

## Installation

1. Clone the repository:
    ```bash
    git clone https://github.com/yourusername/your-repository.git
    cd your-repository
    ```

2. Install the required Python dependencies:
    ```bash
    pip install -r requirements.txt
    ```

3. Create a `.env` file in the root directory with the following content:
    ```bash
    DISCORD_TOKEN=your-discord-bot-token
    VIP_DURATION_HOURS=24  # Example: 24 hours of VIP
    CHANNEL_ID=your-discord-channel-id
    LOCAL_TIMEZONE=Europe/Berlin

    # Optional overrides
    DATABASE_PATH=
    DATABASE_TABLE=vip_players
    ANNOUNCEMENT_MESSAGE_ID=
    MODERATION_CHANNEL_ID=
    MODERATOR_ROLE_ID=

    RCON_HOST=127.0.0.1
    RCON_PORT=21115
    RCON_PASSWORD=your-rcon-password
    RCON_VERSION=2
    ```

4. Run the bot:
    ```bash
    python frontline-pass.py
    ```

## Deployment on Railway

The repository includes a `Procfile` and `railway.toml` so the bot can run as a long-lived worker process on [Railway](https://railway.app/). To deploy:

1. Install the [Railway CLI](https://docs.railway.app/develop/cli) and authenticate with `railway login`, or connect the repository through the Railway dashboard.
2. Create a new project/service and point it at this repository. Railway's Python Nixpacks builder will automatically install the packages from `requirements.txt`.
3. Add the required environment variables (`DISCORD_TOKEN`, `VIP_DURATION_HOURS`, `CHANNEL_ID`, `LOCAL_TIMEZONE`, `RCON_HOST`, `RCON_PORT`, `RCON_PASSWORD`). Optional variables include `RCON_VERSION`, `DATABASE_TABLE`, `DATABASE_PATH`, `ANNOUNCEMENT_MESSAGE_ID`, `MODERATION_CHANNEL_ID`, and `MODERATOR_ROLE_ID`. The CLI example is:
    ```bash
    railway variables set DISCORD_TOKEN=... VIP_DURATION_HOURS=4 CHANNEL_ID=...
    ```
4. (Recommended) Attach a Railway volume so the SQLite database survives restarts:
    ```bash
    railway volume create frontline-pass-data --mountPath /data
    ```
    When a volume is attached, the bot automatically writes `frontline-pass.db` into the provided mount path (via the `RAILWAY_VOLUME_MOUNT_PATH` environment variable). If you prefer a different location, set `DATABASE_PATH` explicitly.
5. Deploy with `railway up` (CLI) or trigger a deploy from the dashboard. Railway will start the service with `python frontline-pass.py` as defined in the Procfile/`railway.toml`.

You can tail logs with `railway logs` once the service is running.

## Usage

1. **Registering Player ID**: Once the bot is running, it will post a message in the specified Discord channel with two buttons. To register, users click the "Register" button and enter their player ID (Steam or Gamepass).
   
2. **Requesting VIP Status**: After registration, users can request VIP status by clicking the "Get VIP" button. The bot performs the RCON V2 `AddVip` command against the configured server(s) to grant the user VIP status for the configured duration.

3. **VIP Expiration**: The bot will display the expiration time of the VIP status in the local time zone (Europe/Berlin by default).

4. **Duplicate Player-IDs**: If a user reuses an existing Player-ID, the bot blocks the update, informs the user, and (optionally) alerts moderators in the configured channel.

## Dependencies

- `discord.py`: For Discord bot functionality
- `pytz`: For time zone management
- `dotenv`: For loading environment variables

## Contributing

Feel free to fork this repository and create a pull request if you want to contribute to the project. You can also open issues if you encounter any problems or have suggestions for new features.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for more details.
