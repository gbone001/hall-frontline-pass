import asyncio
import base64
import json
import logging
import os
import socket
import struct
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple, Union

import discord
import mysql.connector
import pytz
from discord import ButtonStyle
from discord.ext import commands
from discord.ui import Button, Modal, TextInput, View
from dotenv import load_dotenv

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
VIP_DURATION_HOURS = float(os.getenv("VIP_DURATION_HOURS"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
LOCAL_TIMEZONE = pytz.timezone(os.getenv('LOCAL_TIMEZONE'))
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = os.getenv("RCON_PORT")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")
RCON_VERSION = int(os.getenv("RCON_VERSION", "2"))

conn = mysql.connector.connect(
    host=os.getenv('DATABASE_HOST'),
    port=int(os.getenv('DATABASE_PORT')),
    user=os.getenv('DATABASE_USER'),
    password=os.getenv('DATABASE_PASSWORD'),
    database=os.getenv('DATABASE_NAME')
)
cursor = conn.cursor()
logging.basicConfig(level=logging.INFO)
cursor.execute(f'''
    CREATE TABLE IF NOT EXISTS `{os.getenv('DATABASE_TABLE')}` (
        id INT AUTO_INCREMENT PRIMARY KEY,
        discord_id VARCHAR(255) UNIQUE NOT NULL,
        steam_id VARCHAR(255) UNIQUE NOT NULL
    )
''')

conn.commit()
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


class RconError(Exception):
    """Raised when an RCON operation fails."""


class RconClient:
    def __init__(self, host: str, port: int, password: str, *, version: int = 2, timeout: float = 10.0) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.version = version
        self.timeout = timeout
        self._socket: Optional[socket.socket] = None
        self._xor_key: Optional[bytes] = None
        self._auth_token: str = ""
        self._packet_id: int = 0

    def __enter__(self) -> "RconClient":
        self.connect()
        self.login()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        try:
            self._socket = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._socket.settimeout(self.timeout)
        except OSError as exc:
            raise RconError(f"Unable to connect to RCON server {self.host}:{self.port}.") from exc

        self._packet_id = 0
        self._xor_key = None
        self._auth_token = ""

        self._send_packet(
            self._build_payload("ServerConnect", "", auth_token=""),
            encrypt=False,
        )
        _, response = self._read_packet(encrypted=False)
        self._assert_success(response, "ServerConnect")

        xor_content = self._get_content(response)
        if not isinstance(xor_content, str):
            raise RconError("Invalid XOR key returned from server.")

        try:
            self._xor_key = base64.b64decode(xor_content.strip())
        except (base64.binascii.Error, AttributeError) as exc:
            raise RconError("Failed to decode XOR key from ServerConnect response.") from exc

        if not self._xor_key:
            raise RconError("Empty XOR key received from ServerConnect response.")

    def close(self) -> None:
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            finally:
                self._socket = None

    def login(self) -> None:
        self._send_packet(
            self._build_payload("Login", self.password, auth_token=""),
        )
        _, response = self._read_packet()
        self._assert_success(response, "Login")

        token = self._get_content(response)
        if not isinstance(token, str) or not token:
            raise RconError("Login response did not include an authentication token.")
        self._auth_token = token

    def add_vip(self, player_id: str, comment: str = "") -> str:
        response = self.execute("AddVip", {"PlayerId": player_id, "Comment": comment})
        return self._get_status_message(response) or "VIP added successfully."

    def execute(self, command: str, content_body: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
        self._send_packet(self._build_payload(command, content_body))
        _, response = self._read_packet()
        self._assert_success(response, command)
        return response

    def _build_payload(
        self,
        command: str,
        content_body: Union[str, Dict[str, Any]],
        *,
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "AuthToken": auth_token if auth_token is not None else self._auth_token,
            "Version": self.version,
            "Name": command,
            "ContentBody": content_body,
        }

    def _send_packet(self, payload: Dict[str, Any], *, encrypt: bool = True) -> int:
        if not self._socket:
            raise RconError("Not connected to the RCON server.")

        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if encrypt:
            data = self._xor(data)

        packet_id = self._packet_id
        self._packet_id += 1

        header = struct.pack("<II", packet_id, len(data))
        try:
            self._socket.sendall(header + data)
        except OSError as exc:
            raise RconError("Failed to send data to the RCON server.") from exc
        return packet_id

    def _read_packet(self, *, encrypted: bool = True) -> Tuple[int, Dict[str, Any]]:
        if not self._socket:
            raise RconError("Not connected to the RCON server.")

        header = self._recv_exact(8)
        packet_id, length = struct.unpack("<II", header)
        body = self._recv_exact(length)

        if encrypted:
            body = self._xor(body)

        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RconError("Failed to decode response from the RCON server.") from exc
        return packet_id, payload

    def _recv_exact(self, size: int) -> bytes:
        if not self._socket:
            raise RconError("Not connected to the RCON server.")

        buffer = bytearray()
        while len(buffer) < size:
            try:
                chunk = self._socket.recv(size - len(buffer))
            except OSError as exc:
                raise RconError("Connection to the RCON server was interrupted.") from exc
            if not chunk:
                raise RconError("RCON server closed the connection unexpectedly.")
            buffer.extend(chunk)
        return bytes(buffer)

    def _xor(self, data: bytes) -> bytes:
        if not self._xor_key:
            return data
        key = self._xor_key
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))

    @staticmethod
    def _get_status_code(payload: Dict[str, Any]) -> Optional[int]:
        code = payload.get("StatusCode")
        if code is None:
            code = payload.get("statusCode")
        return code

    @staticmethod
    def _get_status_message(payload: Dict[str, Any]) -> Optional[str]:
        message = payload.get("StatusMessage")
        if message is None:
            message = payload.get("statusMessage")
        return message

    @staticmethod
    def _get_content(payload: Dict[str, Any]) -> Any:
        if "ContentBody" in payload:
            return payload["ContentBody"]
        return payload.get("contentBody")

    def _assert_success(self, payload: Dict[str, Any], command: str) -> None:
        status_code = self._get_status_code(payload)
        if status_code != 200:
            message = self._get_status_message(payload) or f"{command} failed with status {status_code}."
            raise RconError(message)


def _get_rcon_config() -> Tuple[str, int, str]:
    if not RCON_HOST or not RCON_PORT or not RCON_PASSWORD:
        raise RconError("Missing RCON configuration. Ensure RCON_HOST, RCON_PORT, and RCON_PASSWORD are set.")
    try:
        port = int(RCON_PORT)
    except ValueError as exc:
        raise RconError("RCON_PORT must be an integer.") from exc
    return RCON_HOST, port, RCON_PASSWORD


def grant_vip(player_id: str, comment: str) -> str:
    host, port, password = _get_rcon_config()
    with RconClient(host, port, password, version=RCON_VERSION) as client:
        return client.add_vip(player_id, comment)

@bot.event
async def on_ready():
    print(f'Bot is ready: {bot.user}')
    channel = bot.get_channel(CHANNEL_ID)
    
    if channel:
        view = CombinedView()
        await channel.send(f"Welcome! Use the buttons below to register your player ID and receive VIP status on all connected servers. You only need to register once, but afterward, you can claim temporary VIP status for {VIP_DURATION_HOURS} hours.", view=view)
    else:
        print(f"Error: Channel ID {CHANNEL_ID} not found.")

class PlayerIDModal(Modal):
    def __init__(self):
        super().__init__(title="Please input Player-ID")
        self.player_id = TextInput(label="Player-ID (Steam-ID or Gamepass-ID)", placeholder="12345678901234567")
        self.add_item(self.player_id)
    
    async def on_submit(self, interaction: discord.Interaction):
        steam_id = self.player_id.value
        cursor.execute(f"INSERT INTO `{os.getenv('DATABASE_TABLE')}` (discord_id, steam_id) VALUES (%s, %s) ON DUPLICATE KEY UPDATE steam_id = %s", (str(interaction.user.id), steam_id, steam_id))
        conn.commit()
        await interaction.response.send_message(f'Your Player-ID {steam_id} has been saved!', ephemeral=True)

class CombinedView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Register", style=ButtonStyle.danger)
    async def register_button(self, interaction: discord.Interaction, button: Button):
        modal = PlayerIDModal()
        await interaction.response.send_modal(modal)

    @discord.ui.button(label=f"get VIP ({VIP_DURATION_HOURS} hours)", style=ButtonStyle.green)
    async def give_vip_button(self, interaction: discord.Interaction, button: Button):
        cursor.execute(f"SELECT steam_id FROM `{os.getenv('DATABASE_TABLE')}` WHERE discord_id=%s", (str(interaction.user.id),))
        player = cursor.fetchone()

        if not player:
            await interaction.response.send_message("You are not registered! Please use the register button first.", ephemeral=True)
            return

        steam_id = str(player[0])
        local_time = datetime.now(LOCAL_TIMEZONE)
        expiration_time_local = local_time + timedelta(hours=VIP_DURATION_HOURS)
        expiration_time_utc = expiration_time_local.astimezone(pytz.utc)
        expiration_time_utc_str = expiration_time_utc.strftime('%Y-%m-%d %H:%M:%S')
        comment = f"Discord VIP for {interaction.user.display_name} until {expiration_time_utc_str} UTC"

        await interaction.response.defer()
        try:
            loop = asyncio.get_running_loop()
            status_message = await loop.run_in_executor(None, grant_vip, steam_id, comment)
        except RconError as exc:
            logging.exception("Failed to grant VIP for player %s", steam_id)
            await interaction.followup.send(f"Error: VIP status could not be set: {exc}")
            return
        except Exception as exc:  # pragma: no cover - defensive catch for unexpected errors
            logging.exception("Unexpected error while granting VIP for player %s: %s", steam_id, exc)
            await interaction.followup.send("An unexpected error occurred while setting VIP status.")
            return

        readable_expiration = expiration_time_local.strftime('%Y-%m-%d %H:%M:%S %Z')
        logging.info(
            "Granted VIP for player %s until %s UTC (%s)",
            steam_id,
            expiration_time_utc_str,
            status_message,
        )
        await interaction.followup.send(
            f"You now have VIP for {VIP_DURATION_HOURS} hours! Expiration: {readable_expiration}"
        )

bot.run(DISCORD_TOKEN)
