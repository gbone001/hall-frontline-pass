from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import socket
import struct
import requests
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

import discord
import pytz
from discord import ButtonStyle
try:
    from discord.abc import MessageableChannel
except ImportError:  # discord.py>=2.4 renamed MessageableChannel -> Messageable
    from discord.abc import Messageable as MessageableChannel
from discord.ext import commands
from discord.ui import Button, Modal, TextInput, View
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)

ANNOUNCEMENT_TITLE = "VIP Control Center"
ANNOUNCEMENT_METADATA_KEY = "announcement_message_id"
LAST_GRANT_METADATA_KEY = "last_vip_grant"


def schedule_ephemeral_cleanup(
    interaction: discord.Interaction,
    *,
    delay: float = 10.0,
    message: Optional[discord.Message] = None,
) -> None:
    async def _cleanup() -> None:
        await asyncio.sleep(delay)
        with contextlib.suppress(discord.HTTPException, discord.NotFound):
            if message is None:
                await interaction.delete_original_response()
            else:
                await message.delete()

    asyncio.create_task(_cleanup())


class DuplicateSteamIDError(Exception):
    def __init__(self, steam_id: str, existing_discord_id: Optional[str]) -> None:
        super().__init__(f"Player-ID {steam_id} already registered.")
        self.steam_id = steam_id
        self.existing_discord_id = existing_discord_id


def build_announcement_embed(config: AppConfig, database: "Database") -> discord.Embed:
    total_players = database.count_players()
    last_grant_text = "No VIP grants yet."
    last_grant_value = database.get_metadata(LAST_GRANT_METADATA_KEY)
    if last_grant_value:
        try:
            last_grant_dt = datetime.fromisoformat(last_grant_value)
            if last_grant_dt.tzinfo is None:
                last_grant_dt = last_grant_dt.replace(tzinfo=timezone.utc)
            local_dt = last_grant_dt.astimezone(config.timezone)
            last_grant_text = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
        except ValueError:
            last_grant_text = "Unknown"

    embed = discord.Embed(
        title=ANNOUNCEMENT_TITLE,
        description=(
            "Use the buttons below to register your Player-ID and request VIP access.\n"
            f"VIP duration: **{config.vip_duration_label} hours**.\n"
            "Registration is required once; future VIP requests are instant."
        ),
        color=0x2F3136,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Registered Players", value=str(total_players), inline=True)
    embed.add_field(name="Last VIP Grant", value=last_grant_text, inline=True)
    embed.add_field(name="Local Timezone", value=config.timezone_name, inline=True)
    embed.set_footer(text="Buttons stay active across restarts.")
    return embed


class AnnouncementManager:
    def __init__(self, config: AppConfig, database: "Database") -> None:
        self._config = config
        self._database = database

    async def ensure(
        self,
        bot: commands.Bot,
        view: View,
        *,
        force_new: bool = False,
    ) -> Optional[discord.Message]:
        destination = await self._resolve_destination(bot)
        if destination is None:
            return None

        embed = build_announcement_embed(self._config, self._database)
        message = await self._locate_message(destination, bot, force_new=force_new)

        if message and not force_new:
            await message.edit(embed=embed, view=view)
            self._store_message_id(message.id)
            logging.info("Reattached control view to existing message %s", message.id)
            return message

        sent_message = await destination.send(embed=embed, view=view)
        self._store_message_id(sent_message.id)
        logging.info(
            "Posted announcement message with id %s. Metadata updated for future restarts.",
            sent_message.id,
        )
        return sent_message

    async def _resolve_destination(self, bot: commands.Bot) -> Optional[MessageableChannel]:
        destination = bot.get_channel(self._config.channel_id)
        if destination is None:
            try:
                destination = await bot.fetch_channel(self._config.channel_id)
            except discord.DiscordException:
                logging.exception("Failed to access channel with id %s", self._config.channel_id)
                return None

        if not isinstance(destination, (discord.TextChannel, discord.Thread, discord.DMChannel)):
            logging.error("Channel %s is not a text-based destination.", self._config.channel_id)
            return None

        return destination

    async def _locate_message(
        self,
        destination: MessageableChannel,
        bot: commands.Bot,
        *,
        force_new: bool,
    ) -> Optional[discord.Message]:
        candidate_ids = self._candidate_message_ids()
        for candidate_id in candidate_ids:
            try:
                message = await destination.fetch_message(candidate_id)
                if force_new:
                    await self._delete_message(message)
                    return None
                return message
            except discord.NotFound:
                self._maybe_clear_metadata(candidate_id)
            except discord.DiscordException:
                logging.exception("Failed to fetch announcement message %s", candidate_id)

        async for message in destination.history(limit=50):
            if message.author == bot.user and message.embeds:
                if message.embeds[0].title == ANNOUNCEMENT_TITLE:
                    if force_new:
                        await self._delete_message(message)
                        return None
                    return message
        return None

    async def _delete_message(self, message: discord.Message) -> None:
        try:
            await message.delete()
        except discord.DiscordException:
            logging.exception("Failed to delete announcement message %s", message.id)

    def _candidate_message_ids(self) -> List[int]:
        candidate_ids: List[int] = []
        stored_message_id = self._database.get_metadata(ANNOUNCEMENT_METADATA_KEY)
        if stored_message_id:
            try:
                candidate_ids.append(int(stored_message_id))
            except ValueError:
                self._database.delete_metadata(ANNOUNCEMENT_METADATA_KEY)

        if self._config.announcement_message_id:
            candidate_ids.append(self._config.announcement_message_id)
        return candidate_ids

    def _maybe_clear_metadata(self, candidate_id: int) -> None:
        stored_message_id = self._database.get_metadata(ANNOUNCEMENT_METADATA_KEY)
        if stored_message_id and stored_message_id == str(candidate_id):
            self._database.delete_metadata(ANNOUNCEMENT_METADATA_KEY)

    def _store_message_id(self, message_id: int) -> None:
        self._database.set_metadata(ANNOUNCEMENT_METADATA_KEY, str(message_id))


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
            return os.path.join(base_dir, "vip-data.json")

    return "vip-data.json"


def _optional_int(name: str) -> Optional[int]:
    value = os.getenv(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid integer for {name}: {value}") from exc


@dataclass(frozen=True)
class HttpCredentials:
    base_url: str
    bearer_token: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


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
    http_credentials: Optional[HttpCredentials]

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

    http_base_url = os.getenv("CRCON_HTTP_BASE_URL")
    http_bearer_token = os.getenv("CRCON_HTTP_BEARER_TOKEN")
    http_username = os.getenv("CRCON_HTTP_USERNAME")
    http_password = os.getenv("CRCON_HTTP_PASSWORD")
    http_credentials: Optional[HttpCredentials] = None

    trimmed_base = (http_base_url or "").strip()
    trimmed_token = (http_bearer_token or "").strip()
    trimmed_username = (http_username or "").strip()
    password_provided = bool(http_password and http_password.strip())

    if any(
        value
        for value in (
            trimmed_base,
            trimmed_token,
            trimmed_username,
            http_password if http_password else None,
        )
    ):
        trimmed_base = (http_base_url or "").strip()
        if not trimmed_base:
            errors.append("CRCON_HTTP_BASE_URL is required when using the HTTP API integration")
        if not trimmed_token and not (trimmed_username and password_provided):
            errors.append(
                "Provide either CRCON_HTTP_BEARER_TOKEN or both CRCON_HTTP_USERNAME and CRCON_HTTP_PASSWORD "
                "when enabling the HTTP API integration"
            )
        if trimmed_username and not password_provided:
            errors.append("CRCON_HTTP_PASSWORD is required when CRCON_HTTP_USERNAME is provided")

        if trimmed_base and (trimmed_token or (trimmed_username and password_provided)):
            http_credentials = HttpCredentials(
                base_url=trimmed_base.rstrip("/"),
                bearer_token=trimmed_token or None,
                username=trimmed_username or None,
                password=http_password if password_provided else None,
            )

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
        http_credentials=http_credentials,
    )


class Database:
    def __init__(self, path: str, table: str) -> None:
        self.table = table
        self._path = path
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {"players": {}, "metadata": {}}
        self._ensure_database_directory()
        self._load()
        self._save()  # ensure a file exists on disk
        logging.info("Using JSON database file at %s", self._path)

    def _ensure_database_directory(self) -> None:
        directory = os.path.dirname(os.path.abspath(self._path))
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)

    def _load(self) -> None:
        data: Dict[str, Any] = {}
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            data = {}
        except Exception:
            logging.exception("Failed to load JSON database from %s; starting with empty data.", self._path)
            data = {}

        if not isinstance(data, dict):
            data = {}

        players: Dict[str, str] = {}
        for discord_id, record in data.get("players", {}).items():
            steam_id: Optional[str]
            if isinstance(record, dict):
                steam_id = record.get("steam_id")
            else:
                steam_id = record
            if isinstance(discord_id, str) and isinstance(steam_id, str) and steam_id:
                players[discord_id] = steam_id

        metadata: Dict[str, str] = {}
        for key, value in data.get("metadata", {}).items():
            if isinstance(key, str) and isinstance(value, str):
                metadata[key] = value

        with self._lock:
            self._data = {"players": players, "metadata": metadata}

    def _save_locked(self) -> None:
        tmp_path = f"{self._path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2, sort_keys=True)
        os.replace(tmp_path, self._path)

    def _save(self) -> None:
        with self._lock:
            self._save_locked()

    def _players(self) -> Dict[str, str]:
        return self._data.setdefault("players", {})

    def upsert_player(self, discord_id: str, steam_id: str) -> None:
        with self._lock:
            players = self._players()
            for existing_discord_id, existing_steam in players.items():
                if existing_steam == steam_id and existing_discord_id != discord_id:
                    raise DuplicateSteamIDError(steam_id, existing_discord_id)
            players[discord_id] = steam_id
            self._save_locked()

    def fetch_player(self, discord_id: str) -> Optional[str]:
        with self._lock:
            return self._players().get(discord_id)

    def fetch_discord_id_for_steam(self, steam_id: str) -> Optional[str]:
        with self._lock:
            for discord_id, stored_steam in self._players().items():
                if stored_steam == steam_id:
                    return discord_id
        return None

    def set_metadata(self, key: str, value: str) -> None:
        with self._lock:
            self._data.setdefault("metadata", {})[key] = value
            self._save_locked()

    def get_metadata(self, key: str) -> Optional[str]:
        with self._lock:
            return self._data.get("metadata", {}).get(key)

    def delete_metadata(self, key: str) -> None:
        with self._lock:
            metadata = self._data.setdefault("metadata", {})
            metadata.pop(key, None)
            self._save_locked()

    def count_players(self) -> int:
        with self._lock:
            return len(self._players())


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
        response = self.execute("AddVip", {"PlayerId": player_id, "Description": comment})
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
        self._http_client = VipHttpClient(config.http_credentials) if config.http_credentials else None

    def grant_vip(self, player_id: str, comment: str, expiration_iso: Optional[str]) -> VipGrantResult:
        status_lines: List[str] = []
        detail = ""
        overall_success = False

        if self._http_client:
            try:
                response = self._http_client.add_vip(player_id, comment, expiration_iso)
                message = response.get("result")
                if isinstance(message, dict):
                    message = message.get("result") or message
                if message is None:
                    message = "HTTP API add_vip succeeded."
                status_lines.append(f"HTTP API: {message}")
                detail = str(message)
                overall_success = True
            except VipHTTPError as exc:
                status_lines.append(f"HTTP API add_vip failed: {exc}")

        if not overall_success:
            try:
                rcon_message = self._grant_vip_via_rcon(player_id, comment)
                status_lines.append(f"RCON AddVip succeeded: {rcon_message}")
                detail = rcon_message
                overall_success = True
            except RconError as exc:
                status_lines.append(f"RCON AddVip failed: {exc}")
                combined = "; ".join(status_lines) if status_lines else str(exc)
                raise RconError(combined) from exc
        elif self._http_client:
            status_lines.append("RCON fallback not required.")

        return VipGrantResult(status_lines=status_lines, detail=detail or "VIP added successfully.")

    def _grant_vip_via_rcon(self, player_id: str, comment: str) -> str:
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


class VipHTTPError(Exception):
    """Raised when the optional HTTP API integration fails."""


class VipHttpClient:
    def __init__(
        self,
        credentials: HttpCredentials,
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.credentials = credentials
        self.timeout = timeout
        self.session = session or requests.Session()
        self._token: Optional[str] = None

    def _endpoint(self, name: str) -> str:
        base = self.credentials.base_url.rstrip("/")
        if not base.lower().endswith("/api"):
            base = f"{base}/api"
        return f"{base}/{name.lstrip('/')}"

    def _headers(self, *, include_auth: bool = True) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if include_auth:
            token = self._authorization_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def _authorization_token(self) -> Optional[str]:
        if not self.credentials:
            return None
        if self.credentials.bearer_token:
            return self.credentials.bearer_token
        if self._has_login_credentials():
            if not self._token:
                self._login()
            return self._token
        return None

    def _has_login_credentials(self) -> bool:
        return bool(self.credentials.username and self.credentials.password)

    def _login(self) -> None:
        if not self._has_login_credentials():
            raise VipHTTPError("CRCON HTTP username/password are not configured.")

        response = self.session.post(
            self._endpoint("login"),
            json={
                "username": self.credentials.username,
                "password": self.credentials.password,
            },
            headers=self._headers(include_auth=False),
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise VipHTTPError(f"Login failed with status {response.status_code}: {response.text}")

        data = self._parse_json(response)
        token = data.get("result") or data.get("token") or data.get("access_token")
        if not isinstance(token, str) or not token:
            raise VipHTTPError("Login response did not include a token.")
        self._token = token

    def _refresh_token_if_possible(self) -> bool:
        if not self._has_login_credentials():
            return False
        self._token = None
        try:
            self._authorization_token()
        except VipHTTPError:
            return False
        return True

    def _request_with_reauth(
        self,
        method: str,
        endpoint: str,
        *,
        json_payload: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        url = self._endpoint(endpoint)
        headers = self._headers()
        response = self.session.request(
            method,
            url,
            headers=headers,
            json=json_payload,
            timeout=self.timeout,
        )
        if response.status_code == 401 and self._refresh_token_if_possible():
            headers = self._headers()
            response = self.session.request(
                method,
                url,
                headers=headers,
                json=json_payload,
                timeout=self.timeout,
            )
        return response

    def add_vip(
        self,
        player_id: str,
        description: str,
        expiration_iso: Optional[str],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "player_id": player_id,
            "description": description,
        }
        if expiration_iso:
            payload["expiration"] = expiration_iso

        try:
            response = self._request_with_reauth("POST", "add_vip", json_payload=payload)
        except requests.exceptions.RequestException as exc:
            raise VipHTTPError(f"HTTP API request failed: {exc}") from exc

        if response.status_code != 200:
            raise VipHTTPError(
                f"add_vip failed with status {response.status_code}: {response.text}"
            )

        data = self._parse_json(response)
        if data.get("failed"):
            raise VipHTTPError(f"add_vip reported failure: {data.get('error') or data}")
        return data

    @staticmethod
    def _parse_json(response: requests.Response) -> Dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise VipHTTPError(f"Failed to parse JSON response: {response.text}") from exc
        if isinstance(data, dict):
            return data
        raise VipHTTPError("Unexpected response format; expected JSON object.")


@dataclass
class VipGrantResult:
    status_lines: List[str]
    detail: str


class PlayerIDModal(Modal):
    def __init__(self, database: Database, notifier: ModeratorNotifier):
        super().__init__(title="Please input Player-ID", custom_id="frontline-pass-player-id-modal")
        self.database = database
        self.notifier = notifier
        self.player_id = TextInput(
            label="T17 / Steam ID",
            placeholder="12345678901234567",
            custom_id="frontline-pass-player-id-input",
        )
        self.add_item(self.player_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        steam_id = self.player_id.value.strip()
        if not steam_id:
            await interaction.response.send_message("T17 ID cannot be empty.", ephemeral=True)
            schedule_ephemeral_cleanup(interaction)
            return

        discord_id = str(interaction.user.id)
        previous_steam_id = self.database.fetch_player(discord_id)

        try:
            self.database.upsert_player(discord_id, steam_id)
        except DuplicateSteamIDError as exc:
            await interaction.response.send_message(
                "Error: That T17 ID is already linked to another Discord account. A moderator has been notified.",
                ephemeral=True,
            )
            schedule_ephemeral_cleanup(interaction)
            await self.notifier.notify_duplicate(interaction, exc.steam_id, exc.existing_discord_id)
            return

        if previous_steam_id and previous_steam_id != steam_id:
            message = f"Your T17 ID was updated to {steam_id}."
        else:
            message = f"Your T17 ID {steam_id} has been saved!"

        await interaction.response.send_message(message, ephemeral=True)
        schedule_ephemeral_cleanup(interaction)


class PersistentView(View):
    def __init__(self) -> None:
        super().__init__(timeout=None)


class CombinedView(PersistentView):
    def __init__(self, config: AppConfig, database: Database, vip_service: VipService, notifier: ModeratorNotifier) -> None:
        super().__init__()
        self.config = config
        self.database = database
        self.vip_service = vip_service
        self.notifier = notifier
        self.give_vip_button.label = f"Get VIP ({self.config.vip_duration_label} hours)"

    @discord.ui.button(label="Register", style=ButtonStyle.danger, custom_id="frontline-pass-register")
    async def register_button(self, interaction: discord.Interaction, _: Button) -> None:
        existing = self.database.fetch_player(str(interaction.user.id))
        if existing:
            await interaction.response.send_message(
                "You're already registered. Your T17 and Discord IDs are linked.",
                ephemeral=True,
            )
            schedule_ephemeral_cleanup(interaction)
            return

        await interaction.response.send_modal(PlayerIDModal(self.database, self.notifier))

    @discord.ui.button(label="Get VIP", style=ButtonStyle.green, custom_id="frontline-pass-get-vip")
    async def give_vip_button(self, interaction: discord.Interaction, _: Button) -> None:
        steam_id = self.database.fetch_player(str(interaction.user.id))

        if not steam_id:
            await interaction.response.send_message(
                "You are not linked to a T17 account. Please register first.",
                ephemeral=True,
            )
            schedule_ephemeral_cleanup(interaction)
            return

        local_time = datetime.now(self.config.timezone)
        expiration_time_local = local_time + timedelta(hours=self.config.vip_duration_hours)
        expiration_time_utc = expiration_time_local.astimezone(pytz.utc)
        expiration_time_utc_str = expiration_time_utc.strftime("%Y-%m-%d %H:%M:%S")
        expiration_time_iso = expiration_time_utc.isoformat()
        comment = f"Discord VIP for {interaction.user.display_name} until {expiration_time_utc_str} UTC"

        await interaction.response.defer(ephemeral=True)
        try:
            result = await asyncio.to_thread(
                self.vip_service.grant_vip,
                steam_id,
                comment,
                expiration_time_iso,
            )
        except RconError as exc:
            logging.exception("Failed to grant VIP for player %s", steam_id)
            followup_message = await interaction.followup.send(
                f"Error: VIP status could not be set: {exc}",
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)
            return
        except Exception as exc:  # pragma: no cover
            logging.exception("Unexpected error while granting VIP for player %s: %s", steam_id, exc)
            followup_message = await interaction.followup.send(
                "An unexpected error occurred while setting VIP status.",
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)
            return

        readable_expiration = expiration_time_local.strftime("%Y-%m-%d %H:%M:%S %Z")
        logging.info(
            "Granted VIP for player %s until %s UTC (%s)",
            steam_id,
            expiration_time_utc_str,
            "; ".join(result.status_lines),
        )
        self.database.set_metadata(LAST_GRANT_METADATA_KEY, datetime.now(timezone.utc).isoformat())
        if isinstance(interaction.client, FrontlinePassBot):
            await interaction.client.refresh_announcement_message()
        status_summary = "\n".join(f"- {line}" for line in result.status_lines)
        followup_message = await interaction.followup.send(
            (
                f"You now have VIP for {self.config.vip_duration_label} hours! "
                f"Expiration: {readable_expiration}\n\n**Status**:\n{status_summary}"
            ),
            ephemeral=True,
            wait=True,
        )
        schedule_ephemeral_cleanup(interaction, message=followup_message)


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
        self.announcement_manager = AnnouncementManager(config, database)

    async def setup_hook(self) -> None:
        self.persistent_view = CombinedView(self.config, self.database, self.vip_service, self.notifier)
        self.add_view(self.persistent_view)
        await self._register_commands()
        await self.tree.sync()

    async def on_ready(self) -> None:
        logging.info("Bot is ready: %s", self.user)
        await self.refresh_announcement_message()

    async def refresh_announcement_message(self) -> None:
        if not self.persistent_view:
            logging.error("Persistent view not initialised; cannot refresh announcement message.")
            return
        await self.announcement_manager.ensure(self, self.persistent_view)

    async def _register_commands(self) -> None:
        @self.tree.command(
            name="repost_frontline_controls",
            description="Repost the Frontline VIP control panel.",
        )
        async def repost_frontline_controls(interaction: discord.Interaction) -> None:
            permissions = getattr(interaction.user, "guild_permissions", None)  # type: ignore[attr-defined]
            if not permissions or not permissions.administrator:
                await interaction.response.send_message(
                    "You need administrator permissions to use this command.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            await interaction.response.defer(ephemeral=True)
            if not self.persistent_view:
                followup_message = await interaction.followup.send(
                    "The persistent view is not initialised yet. Try again shortly.",
                    ephemeral=True,
                    wait=True,
                )
                schedule_ephemeral_cleanup(interaction, message=followup_message)
                return

            message = await self.announcement_manager.ensure(
                self,
                self.persistent_view,
                force_new=True,
            )
            if message:
                followup_message = await interaction.followup.send(
                    f"Frontline VIP controls reposted successfully (message ID {message.id}).",
                    ephemeral=True,
                    wait=True,
                )
                schedule_ephemeral_cleanup(interaction, message=followup_message)
            else:
                followup_message = await interaction.followup.send(
                    "Unable to repost the VIP controls. Check the bot logs for details.",
                    ephemeral=True,
                    wait=True,
                )
                schedule_ephemeral_cleanup(interaction, message=followup_message)


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
