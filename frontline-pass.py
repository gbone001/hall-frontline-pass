import asyncio
import base64
import json
import logging
import os
import socket
import sqlite3
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple, Union

import discord
import pytz
from discord import ButtonStyle
from discord.ext import commands
from discord.ui import Button, Modal, TextInput, View
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)


class DuplicateSteamIDError(Exception):
    def __init__(self, steam_id: str, existing_discord_id: Optional[str]) -> None:
        super().__init__(f"Player-ID {steam_id} already registered.")
        self.steam_id = steam_id
        self.existing_discord_id = existing_discord_id


def _resolve_database_path(explicit_path: Optional[str]) -> str:
    if explicit_path:
        return explicit_path

    fallback_dirs = [
        os.getenv("RAILWAY_VOLUME_MOUNT_PATH"),
        os.getenv("RAILWAY_VOLUME_DIR"),
        os.getenv("RAILWAY_DATA_DIR"),
        os.getenv("DATA_DIR"),
    ]

    for base_dir in fallback_dirs:
        if base_dir:
            return os.path.join(base_dir, "frontline-pass.db")

    return "frontline-pass.db"


def _optional_int(name: str) -> Optional[int]:
    value = os.getenv(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid integer for {name}: {value}") from exc


@dataclass(frozen=True)
class AppConfig:
    discord_token: str
    vip_duration_hours: float
    channel_id: int
    timezone: pytz.BaseTzInfo
    timezone_name: str
    rcon_host: str
    rcon_port: int
    rcon_password: str
    rcon_version: int
    database_path: str
    database_table: str
    moderation_channel_id: Optional[int]
    moderator_role_id: Optional[int]
    announcement_message_id: Optional[int]

    @property
    def vip_duration_label(self) -> str:
        return f"{self.vip_duration_hours:g}"


def load_config() -> AppConfig:
    load_dotenv()
    errors = []

    def require(name: str) -> str:
        value = os.getenv(name)
        if value is None or not value.strip():
            errors.append(f"{name} is required")
            return ""
        return value.strip()

    discord_token = require("DISCORD_TOKEN")
    vip_duration_raw = require("VIP_DURATION_HOURS")
    channel_id_raw = require("CHANNEL_ID")
    timezone_name = require("LOCAL_TIMEZONE")
    rcon_host = require("RCON_HOST")
    rcon_port_raw = require("RCON_PORT")
    rcon_password = require("RCON_PASSWORD")
    rcon_version_raw = os.getenv("RCON_VERSION", "2").strip()

    vip_duration_hours = None
    channel_id = None
    rcon_port = None
    rcon_version = None

    try:
        vip_duration_hours = float(vip_duration_raw)
        if vip_duration_hours <= 0:
            errors.append("VIP_DURATION_HOURS must be greater than zero")
    except ValueError:
        errors.append(f"VIP_DURATION_HOURS must be a number (got {vip_duration_raw!r})")

    try:
        channel_id = int(channel_id_raw)
    except ValueError:
        errors.append(f"CHANNEL_ID must be an integer (got {channel_id_raw!r})")

    try:
        timezone = pytz.timezone(timezone_name)
    except pytz.UnknownTimeZoneError:
        errors.append(f"LOCAL_TIMEZONE must be a valid IANA timezone (got {timezone_name!r})")
        timezone = pytz.UTC

    try:
        rcon_port = int(rcon_port_raw)
    except ValueError:
        errors.append(f"RCON_PORT must be an integer (got {rcon_port_raw!r})")

    try:
        rcon_version = int(rcon_version_raw)
    except ValueError:
        errors.append(f"RCON_VERSION must be an integer (got {rcon_version_raw!r})")

    database_table = (os.getenv("DATABASE_TABLE") or "vip_players").strip()
    if not database_table:
        errors.append("DATABASE_TABLE must not be empty")

    database_path = _resolve_database_path(os.getenv("DATABASE_PATH"))
    moderation_channel_id = _optional_int("MODERATION_CHANNEL_ID")
    moderator_role_id = _optional_int("MODERATOR_ROLE_ID")
    announcement_message_id = _optional_int("ANNOUNCEMENT_MESSAGE_ID")

    if errors:
        raise RuntimeError("Configuration error(s): " + "; ".join(errors))

    return AppConfig(
        discord_token=discord_token,
        vip_duration_hours=vip_duration_hours,
        channel_id=channel_id,
        timezone=timezone,
        timezone_name=timezone_name,
        rcon_host=rcon_host,
        rcon_port=rcon_port,
        rcon_password=rcon_password,
        rcon_version=rcon_version,
        database_path=database_path,
        database_table=database_table,
        moderation_channel_id=moderation_channel_id,
        moderator_role_id=moderator_role_id,
        announcement_message_id=announcement_message_id,
    )


class Database:
    def __init__(self, path: str, table: str) -> None:
        self.table = table
        self._identifier = self._quote_identifier(self.table)
        self._path = path
        self._ensure_database_directory()
        self._conn = sqlite3.connect(
            self._path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
        )
        logging.info("Using database file at %s", self._path)
        self._initialise_schema()

    def _quote_identifier(self, name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("Table name must not be empty.")
        if any(ch in name for ch in ('`', '"', "'", ";")):
            raise ValueError("Invalid characters in table name.")
        return f'"{name}"'

    def _ensure_database_directory(self) -> None:
        directory = os.path.dirname(os.path.abspath(self._path))
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)

    def _initialise_schema(self) -> None:
        self._conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._identifier} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT UNIQUE NOT NULL,
                steam_id TEXT UNIQUE NOT NULL
            )
            """
        )
        self._conn.commit()

    def upsert_player(self, discord_id: str, steam_id: str) -> None:
        try:
            self._conn.execute(
                f"""
                INSERT INTO {self._identifier} (discord_id, steam_id)
                VALUES (?, ?)
                ON CONFLICT(discord_id) DO UPDATE SET steam_id=excluded.steam_id
                """,
                (discord_id, steam_id),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            message = str(exc).lower()
            if "steam_id" in message:
                existing_owner = self.fetch_discord_id_for_steam(steam_id)
                raise DuplicateSteamIDError(steam_id, existing_owner) from exc
            raise

    def fetch_player(self, discord_id: str) -> Optional[str]:
        cursor = self._conn.execute(
            f"SELECT steam_id FROM {self._identifier} WHERE discord_id = ?",
            (discord_id,),
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def fetch_discord_id_for_steam(self, steam_id: str) -> Optional[str]:
        cursor = self._conn.execute(
            f"SELECT discord_id FROM {self._identifier} WHERE steam_id = ?",
            (steam_id,),
        )
        row = cursor.fetchone()
        return row[0] if row else None


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


class VipService:
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def grant_vip(self, player_id: str, comment: str) -> str:
        with RconClient(
            self._config.rcon_host,
            self._config.rcon_port,
            self._config.rcon_password,
            version=self._config.rcon_version,
        ) as client:
            return client.add_vip(player_id, comment)


class ModeratorNotifier:
    def __init__(self, config: AppConfig) -> None:
        self._channel_id = config.moderation_channel_id
        self._role_id = config.moderator_role_id

    async def notify_duplicate(self, interaction: discord.Interaction, steam_id: str, existing_discord_id: Optional[str]) -> None:
        if not self._channel_id:
            logging.info("Duplicate Player-ID detected but MODERATION_CHANNEL_ID is not configured.")
            return

        channel = interaction.client.get_channel(self._channel_id)
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(self._channel_id)
            except discord.DiscordException:
                logging.exception("Failed to fetch moderation channel with id %s", self._channel_id)
                return

        role_mention = f"<@&{self._role_id}>" if self._role_id else ""
        existing_mention = f"<@{existing_discord_id}>" if existing_discord_id else "an unknown user"

        content = (
            f"{role_mention} Duplicate Player-ID attempt detected.\n"
            f"Player-ID `{steam_id}` is already associated with {existing_mention}. "
            f"Attempted by {interaction.user.mention}."
        ).strip()

        try:
            await channel.send(content)
        except discord.DiscordException:
            logging.exception("Failed to notify moderators about duplicate Player-ID %s", steam_id)


class PlayerIDModal(Modal):
    def __init__(self, database: Database, notifier: ModeratorNotifier):
        super().__init__(title="Please input Player-ID", custom_id="frontline-pass-player-id-modal")
        self.database = database
        self.notifier = notifier
        self.player_id = TextInput(
            label="Player-ID (Steam-ID or Gamepass-ID)",
            placeholder="12345678901234567",
            custom_id="frontline-pass-player-id-input",
        )
        self.add_item(self.player_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        steam_id = self.player_id.value.strip()
        if not steam_id:
            await interaction.response.send_message("Player-ID cannot be empty.", ephemeral=True)
            return

        discord_id = str(interaction.user.id)
        previous_steam_id = self.database.fetch_player(discord_id)

        try:
            self.database.upsert_player(discord_id, steam_id)
        except DuplicateSteamIDError as exc:
            await interaction.response.send_message(
                "Error: Player-ID already exists. A moderator has been notified.",
                ephemeral=True,
            )
            await self.notifier.notify_duplicate(interaction, exc.steam_id, exc.existing_discord_id)
            return

        if previous_steam_id and previous_steam_id != steam_id:
            message = f"Your Player-ID was updated to {steam_id}."
        else:
            message = f"Your Player-ID {steam_id} has been saved!"

        await interaction.response.send_message(message, ephemeral=True)


class CombinedView(View):
    def __init__(self, config: AppConfig, database: Database, vip_service: VipService, notifier: ModeratorNotifier) -> None:
        super().__init__(timeout=None)
        self.config = config
        self.database = database
        self.vip_service = vip_service
        self.notifier = notifier
        self.give_vip_button.label = f"Get VIP ({self.config.vip_duration_label} hours)"

    @discord.ui.button(label="Register", style=ButtonStyle.danger, custom_id="frontline-pass-register")
    async def register_button(self, interaction: discord.Interaction, _: Button) -> None:
        await interaction.response.send_modal(PlayerIDModal(self.database, self.notifier))

    @discord.ui.button(label="Get VIP", style=ButtonStyle.green, custom_id="frontline-pass-get-vip")
    async def give_vip_button(self, interaction: discord.Interaction, _: Button) -> None:
        steam_id = self.database.fetch_player(str(interaction.user.id))

        if not steam_id:
            await interaction.response.send_message(
                "You are not registered! Please use the register button first.",
                ephemeral=True,
            )
            return

        local_time = datetime.now(self.config.timezone)
        expiration_time_local = local_time + timedelta(hours=self.config.vip_duration_hours)
        expiration_time_utc = expiration_time_local.astimezone(pytz.utc)
        expiration_time_utc_str = expiration_time_utc.strftime("%Y-%m-%d %H:%M:%S")
        comment = f"Discord VIP for {interaction.user.display_name} until {expiration_time_utc_str} UTC"

        await interaction.response.defer(ephemeral=True)
        try:
            status_message = await asyncio.to_thread(
                self.vip_service.grant_vip,
                steam_id,
                comment,
            )
        except RconError as exc:
            logging.exception("Failed to grant VIP for player %s", steam_id)
            await interaction.followup.send(f"Error: VIP status could not be set: {exc}", ephemeral=True)
            return
        except Exception as exc:  # pragma: no cover
            logging.exception("Unexpected error while granting VIP for player %s: %s", steam_id, exc)
            await interaction.followup.send("An unexpected error occurred while setting VIP status.", ephemeral=True)
            return

        readable_expiration = expiration_time_local.strftime("%Y-%m-%d %H:%M:%S %Z")
        logging.info(
            "Granted VIP for player %s until %s UTC (%s)",
            steam_id,
            expiration_time_utc_str,
            status_message,
        )
        await interaction.followup.send(
            f"You now have VIP for {self.config.vip_duration_label} hours! Expiration: {readable_expiration}",
            ephemeral=True,
        )


def create_database(config: AppConfig) -> Database:
    return Database(config.database_path, config.database_table)


class FrontlinePassBot(commands.Bot):
    def __init__(self, config: AppConfig, database: Database, vip_service: VipService) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.database = database
        self.vip_service = vip_service
        self.notifier = ModeratorNotifier(config)
        self.persistent_view: Optional[CombinedView] = None

    async def setup_hook(self) -> None:
        self.persistent_view = CombinedView(self.config, self.database, self.vip_service, self.notifier)
        self.add_view(self.persistent_view)

    async def on_ready(self) -> None:
        logging.info("Bot is ready: %s", self.user)
        await self.ensure_announcement_message()

    async def ensure_announcement_message(self) -> None:
        if not self.persistent_view:
            logging.error("Persistent view not initialised; cannot attach announcement message.")
            return

        destination = self.get_channel(self.config.channel_id)
        if destination is None:
            try:
                destination = await self.fetch_channel(self.config.channel_id)
            except discord.DiscordException:
                logging.exception("Failed to access channel with id %s", self.config.channel_id)
                return

        if not isinstance(destination, (discord.TextChannel, discord.Thread, discord.DMChannel)):
            logging.error("Channel %s is not a text-based destination.", self.config.channel_id)
            return

        message_content = (
            "Welcome! Use the buttons below to register your player ID and receive VIP status on all connected servers. "
            f"You only need to register once, but afterward, you can claim temporary VIP status for {self.config.vip_duration_label} hours."
        )

        message = None
        if self.config.announcement_message_id:
            try:
                message = await destination.fetch_message(self.config.announcement_message_id)
            except discord.NotFound:
                logging.warning(
                    "Configured ANNOUNCEMENT_MESSAGE_ID %s was not found in channel %s.",
                    self.config.announcement_message_id,
                    self.config.channel_id,
                )
            except discord.DiscordException:
                logging.exception("Failed to fetch configured announcement message %s", self.config.announcement_message_id)

        if message is None:
            async for candidate in destination.history(limit=50):
                if candidate.author == self.user:
                    message = candidate
                    break

        if message:
            await message.edit(content=message_content, view=self.persistent_view)
            logging.info("Reattached control view to existing message %s", message.id)
        else:
            sent_message = await destination.send(message_content, view=self.persistent_view)
            logging.info(
                "Posted new announcement message with id %s. Set ANNOUNCEMENT_MESSAGE_ID to reuse it on future restarts.",
                sent_message.id,
            )


def create_bot(config: AppConfig, database: Database, vip_service: VipService) -> commands.Bot:
    return FrontlinePassBot(config, database, vip_service)


def main() -> None:
    config = load_config()
    database = create_database(config)
    vip_service = VipService(config)
    bot = create_bot(config, database, vip_service)
    bot.run(config.discord_token)


if __name__ == "__main__":
    main()
