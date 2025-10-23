# Discord VIP Bot

This Discord bot allows users to register their player ID (Steam or Gamepass) and request temporary VIP status for a certain amount of time on connected Hell Let Loose servers. The bot connects directly to the Hell Let Loose RCON V2 interface to manage VIP status for players and stores player information in a MySQL database.

ToDo:
Execute the following commands after downloading:
1. Copy the `.env.dist` file to `.env` and enter your values.
2. Run the command `pip install python-dotenv`.
3. Copy `frontline-pass.service.dist` to `/etc/systemd/system/frontline-pass.service`
4. Activate and start the service with `sudo systemctl enable frontline-pass.service` and `sudo systemctl start frontline-pass.service`.

## Features

- **Player Registration**: Users can register their player ID (Steam-ID or Gamepass-ID) through a modal window.
- **VIP Request**: Users can request VIP status for a predefined number of hours. The bot communicates with the Hell Let Loose RCON V2 protocol to grant VIP status.
- **Persistent Player Data**: Player information is stored in a MySQL database, allowing for easy retrieval and VIP management.
- **Localized Time Support**: The bot handles time zone conversion to display VIP expiration times in local time (set to Europe/Berlin by default).
  
## Prerequisites

- Python 3.8+
- Discord account and server where the bot will be used
- Access to the Hell Let Loose RCON V2 endpoint (hostname/IP, port, and RCON password)
- MySQL instance to store Discord ⇔ player ID mappings
- `.env` file with the following keys:
  - `DISCORD_TOKEN`: Your Discord bot token
  - `VIP_DURATION_HOURS`: Duration of the VIP status in hours
  - `CHANNEL_ID`: The ID of the Discord channel where the bot will post the initial message
  - `LOCAL_TIMEZONE`: IANA time zone identifier used for display (e.g. `Europe/Berlin`)
  - `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_USER`, `DATABASE_PASSWORD`, `DATABASE_NAME`, `DATABASE_TABLE`: MySQL connection details
  - `RCON_HOST`, `RCON_PORT`, `RCON_PASSWORD` (and optionally `RCON_VERSION`): Hell Let Loose RCON V2 connection details

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

    DATABASE_HOST=localhost
    DATABASE_PORT=3306
    DATABASE_USER=db-user
    DATABASE_PASSWORD=db-password
    DATABASE_NAME=frontline
    DATABASE_TABLE=vip_players

    RCON_HOST=127.0.0.1
    RCON_PORT=21115
    RCON_PASSWORD=your-rcon-password
    RCON_VERSION=2
    ```

4. Run the bot:
    ```bash
    python frontline-pass.py
    ```

## Usage

1. **Registering Player ID**: Once the bot is running, it will post a message in the specified Discord channel with two buttons. To register, users click the "Register" button and enter their player ID (Steam or Gamepass).
   
2. **Requesting VIP Status**: After registration, users can request VIP status by clicking the "Get VIP" button. The bot performs the RCON V2 `AddVip` command against the configured server(s) to grant the user VIP status for the configured duration.

3. **VIP Expiration**: The bot will display the expiration time of the VIP status in the local time zone (Europe/Berlin by default).

## Dependencies

- `discord.py`: For Discord bot functionality
- `mysql-connector-python`: For MySQL database management
- `pytz`: For time zone management
- `dotenv`: For loading environment variables

## Contributing

Feel free to fork this repository and create a pull request if you want to contribute to the project. You can also open issues if you encounter any problems or have suggestions for new features.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for more details.
