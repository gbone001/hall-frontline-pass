from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import json5
import logging
import os
import socket
import sqlite3
import struct
import time

import requests
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import discord
import pytz
from discord import ButtonStyle, app_commands
try:
    from discord.abc import MessageableChannel
except ImportError:  # discord.py>=2.4 renamed MessageableChannel -> Messageable
    from discord.abc import Messageable as MessageableChannel
from discord.ext import commands
from discord.ui import Button, Modal, TextInput, View
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

logging.basicConfig(level=logging.INFO)

ANNOUNCEMENT_TITLE = "VIP Control Center"
ANNOUNCEMENT_METADATA_KEY = "announcement_message_id"
LAST_GRANT_METADATA_KEY = "last_vip_grant"
VIP_DURATION_METADATA_KEY = "vip_duration_hours"
PLAYER_ID_PLACEHOLDER = (
    "Link your HLL player-id# to your discord account to get the VIP pass. "
    "Go to https://hllrecords.com/ - and search for your player name. "
    "Open up your player details and copy the Xbox Game Pass / Epic Games string of numbers into the box below."
)


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


class VipHTTPError(Exception):
    """Raised when CRCON HTTP operations fail."""


def build_announcement_embed(config: AppConfig, database: "Database", vip_duration_hours: float) -> discord.Embed:
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
            "Use the button below to activate your VIP access.\n"
            f"VIP duration: **{vip_duration_hours:g} hours**.\n"
            'When registering you need to add your player_id number string i.e. "2805d5bbe14b6ec432f82e5cb859d012" from https://hllrecords.com'
        ),
        color=0x2F3136,
        timestamp=datetime.now(timezone.utc),
    )
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
        vip_duration_hours: float,
        *,
        force_new: bool = False,
    ) -> Optional[discord.Message]:
        destination = await self._resolve_destination(bot)
        if destination is None:
            return None

        embed = build_announcement_embed(self._config, self._database, vip_duration_hours)
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
    def _default_path() -> str:
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

    if explicit_path:
        lowered = explicit_path.lower()
        if lowered.startswith(("postgres://", "postgresql://", "mysql://", "mssql://", "oracle://")):
            logging.info(
                "Remote database URL %s ignored for manual registration; using JSON file instead.",
                explicit_path,
            )
            return _default_path()
        return explicit_path

    return _default_path()


def _parse_bool_env(name: str, raw: Optional[str], errors: List[str]) -> Optional[bool]:
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    errors.append(f"{name} must be a boolean (true/false); got {raw!r}")
    return None


def _load_raw_config() -> Tuple[Dict[str, Any], Optional[Path]]:
    candidate_paths: List[Path] = []
    env_path = os.getenv("FRONTLINE_CONFIG_PATH") or os.getenv("CRCON_CONFIG_PATH")
    if env_path:
        candidate_paths.append(Path(env_path))
    base_dir = Path(__file__).resolve().parent
    for name in ("config.jsonc", "config.json"):
        candidate_paths.append(base_dir / name)
        candidate_paths.append(Path.cwd() / name)

    for path in candidate_paths:
        if not path:
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json5.load(handle)
        except FileNotFoundError:
            continue
        except Exception:
            logging.exception("Failed to load configuration from %s", path)
            continue
        if isinstance(data, dict):
            logging.info("Loaded configuration from %s", path)
            return data, path
    logging.warning("No configuration file found; relying on environment variables.")
    return {}, None


@dataclass(frozen=True)
class HttpCredentials:
    base_url: str
    bearer_token: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    verify: bool = True
    timeout: float = 20.0


@dataclass(frozen=True)
class AppConfig:
    discord_token: str
    vip_duration_hours: float
    channel_id: int
    timezone: pytz.BaseTzInfo
    timezone_name: str
    database_path: str
    database_table: str
    moderation_channel_id: Optional[int] = None
    moderator_role_id: Optional[int] = None
    announcement_message_id: Optional[int] = None
    http_credentials: Optional[HttpCredentials] = None
    crcon_database_url: Optional[str] = None
    vip_temp_role_id: Optional[int] = None
    vip_claim_channel_id: Optional[int] = None

    @property
    def vip_duration_label(self) -> str:
        return f"{self.vip_duration_hours:g}"


def load_config() -> AppConfig:
    load_dotenv()
    raw_config, config_path = _load_raw_config()
    config_values = {str(key).upper(): value for key, value in raw_config.items()}
    errors: List[str] = []

    config_aliases: Dict[str, Tuple[str, ...]] = {
        "CRCON_HTTP_BASE_URL": ("API_BASE_URL",),
        "CRCON_HTTP_BEARER_TOKEN": ("API_BEARER_TOKEN",),
        "CRCON_HTTP_USERNAME": ("API_USERNAME",),
        "CRCON_HTTP_PASSWORD": ("API_PASSWORD",),
        "CRCON_HTTP_VERIFY": ("API_VERIFY",),
        "CRCON_HTTP_TIMEOUT": ("API_TIMEOUT",),
        "CRCON_DATABASE_URL": ("DB_URL", "DATABASE_URL"),
        "DATABASE_PATH": ("JSON_DATABASE_PATH",),
        "DATABASE_TABLE": ("JSON_DATABASE_TABLE",),
    }

    def config_lookup(name: str) -> Any:
        upper = name.upper()
        if upper in config_values and config_values[upper] not in (None, ""):
            return config_values[upper]
        for alias in config_aliases.get(upper, ()): 
            alias_upper = alias.upper()
            if alias_upper in config_values and config_values[alias_upper] not in (None, ""):
                return config_values[alias_upper]
        return None

    def get_value(name: str, default: Any = None) -> Any:
        env_value = os.getenv(name)
        if env_value is not None and env_value.strip() != "":
            return env_value.strip()
        lookup = config_lookup(name)
        if lookup is not None:
            return lookup
        return default

    def require_str(name: str) -> str:
        value = get_value(name)
        if value is None or str(value).strip() == "":
            errors.append(f"{name} is required")
            return ""
        return str(value).strip()

    def require_float(name: str) -> Optional[float]:
        value = get_value(name)
        if value is None or str(value).strip() == "":
            errors.append(f"{name} is required")
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            errors.append(f"{name} must be a number (got {value!r})")
            return None

    def require_int(name: str) -> Optional[int]:
        value = get_value(name)
        if value is None or str(value).strip() == "":
            errors.append(f"{name} is required")
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            errors.append(f"{name} must be an integer (got {value!r})")
            return None

    def optional_int(name: str) -> Optional[int]:
        value = get_value(name)
        if value is None or str(value).strip() == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            errors.append(f"{name} must be an integer (got {value!r})")
            return None

    def optional_float(name: str, default: Optional[float] = None) -> Optional[float]:
        value = get_value(name)
        if value is None or str(value).strip() == "":
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            errors.append(f"{name} must be a number (got {value!r})")
            return default

    def optional_bool(name: str, default: Optional[bool] = None) -> Optional[bool]:
        value = get_value(name)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        parsed = _parse_bool_env(name, str(value), errors)
        if parsed is not None:
            return parsed
        return default

    discord_token = require_str("DISCORD_TOKEN")
    vip_duration_hours = require_float("VIP_DURATION_HOURS")
    channel_id = require_int("CHANNEL_ID")
    timezone_name = require_str("LOCAL_TIMEZONE")

    if vip_duration_hours is not None and vip_duration_hours <= 0:
        errors.append("VIP_DURATION_HOURS must be greater than zero")

    try:
        timezone = pytz.timezone(timezone_name)
    except pytz.UnknownTimeZoneError:
        errors.append(f"LOCAL_TIMEZONE must be a valid IANA timezone (got {timezone_name!r})")
        timezone = pytz.UTC

    database_path_value = get_value("DATABASE_PATH")
    database_path = _resolve_database_path(str(database_path_value) if database_path_value else None)

    database_table_value = get_value("DATABASE_TABLE", default="vip_players")
    if not database_table_value or not str(database_table_value).strip():
        errors.append("DATABASE_TABLE must not be empty")
    database_table = str(database_table_value).strip() if database_table_value else "vip_players"

    moderation_channel_id = optional_int("MODERATION_CHANNEL_ID")
    moderator_role_id = optional_int("MODERATOR_ROLE_ID")
    announcement_message_id = optional_int("ANNOUNCEMENT_MESSAGE_ID")
    vip_temp_role_id = optional_int("VIP_TEMP_ROLE_ID")
    vip_claim_channel_id = optional_int("VIP_CLAIM_CHANNEL_ID")

    http_base_url = require_str("CRCON_HTTP_BASE_URL")
    http_bearer_token = get_value("CRCON_HTTP_BEARER_TOKEN")
    http_username = get_value("CRCON_HTTP_USERNAME")
    http_password = get_value("CRCON_HTTP_PASSWORD")
    http_verify = optional_bool("CRCON_HTTP_VERIFY", default=True)
    http_timeout = optional_float("CRCON_HTTP_TIMEOUT", default=20.0) or 20.0
    trimmed_base = http_base_url.strip()
    trimmed_token = str(http_bearer_token).strip() if http_bearer_token else ""
    trimmed_username = str(http_username).strip() if http_username else ""
    trimmed_password = str(http_password).strip() if http_password else ""

    if not trimmed_base:
        errors.append("CRCON_HTTP_BASE_URL must not be empty")
    elif trimmed_base.lower().endswith("/api"):
        errors.append(
            "CRCON_HTTP_BASE_URL should not include '/api'. Provide the host only "
            "(e.g. https://example.com:8010)."
        )

    if not trimmed_token and not (trimmed_username and trimmed_password):
        errors.append(
            "Provide either CRCON_HTTP_BEARER_TOKEN or both CRCON_HTTP_USERNAME and CRCON_HTTP_PASSWORD."
        )
    if trimmed_username and not trimmed_password:
        errors.append("CRCON_HTTP_PASSWORD is required when CRCON_HTTP_USERNAME is provided")

    normalized_base = trimmed_base.rstrip("/") if trimmed_base else ""
    http_credentials: Optional[HttpCredentials] = None
    if normalized_base and (trimmed_token or (trimmed_username and trimmed_password)):
        http_credentials = HttpCredentials(
            base_url=normalized_base,
            bearer_token=trimmed_token or None,
            username=trimmed_username or None,
            password=trimmed_password or None,
            verify=http_verify if http_verify is not None else True,
            timeout=http_timeout,
        )

    crcon_database_url_raw = get_value("CRCON_DATABASE_URL")
    crcon_database_url = str(crcon_database_url_raw).strip() if crcon_database_url_raw else None

    if errors:
        raise RuntimeError("Configuration error(s): " + "; ".join(errors))

    assert vip_duration_hours is not None
    assert channel_id is not None
    return AppConfig(
        discord_token=discord_token,
        vip_duration_hours=vip_duration_hours,
        channel_id=channel_id,
        timezone=timezone,
        timezone_name=timezone_name,
        database_path=database_path,
        database_table=database_table,
        moderation_channel_id=moderation_channel_id,
        moderator_role_id=moderator_role_id,
        announcement_message_id=announcement_message_id,
        http_credentials=http_credentials,
        crcon_database_url=crcon_database_url,
        vip_temp_role_id=vip_temp_role_id,
        vip_claim_channel_id=vip_claim_channel_id,
    )


class Database:
    def __init__(self, path: str, table: str) -> None:
        self.table = table
        self._path = path
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {"players": {}, "metadata": {}}
        self._ensure_database_directory()
        self._backend = "json"
        if self._is_sqlite_database():
            self._backend = "sqlite"
            self._setup_sqlite_backend()
            logging.info("Using legacy SQLite database at %s", self._path)
        else:
            self._load()
            self._save()  # ensure a file exists on disk
            logging.info("Using JSON database file at %s", self._path)

    def info(self) -> Dict[str, str]:
        """Return basic info for diagnostics and health/status commands."""
        return {"path": self._path, "backend": getattr(self, "_backend", "json")}

    def _ensure_database_directory(self) -> None:
        directory = os.path.dirname(os.path.abspath(self._path))
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)

    def _load(self) -> None:
        if self._using_sqlite():
            return
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

    def _is_sqlite_database(self) -> bool:
        if not self._path or not os.path.exists(self._path) or os.path.isdir(self._path):
            return False
        try:
            with open(self._path, "rb") as handle:
                header = handle.read(16)
        except OSError:
            return False
        return header.startswith(b"SQLite format 3")

    def _setup_sqlite_backend(self) -> None:
        connection = self._sqlite_connection()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS "{self.table}" (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        discord_id TEXT UNIQUE NOT NULL,
                        steam_id TEXT UNIQUE NOT NULL
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                    """
                )
                connection.commit()
            finally:
                cursor.close()
        finally:
            connection.close()

    def _sqlite_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _using_sqlite(self) -> bool:
        return getattr(self, "_backend", "json") == "sqlite"

    def _sqlite_fetch_value(
        self,
        query: str,
        params: Tuple[Any, ...],
        *,
        column: Optional[str] = None,
    ) -> Optional[str]:
        with self._lock:
            connection = self._sqlite_connection()
            try:
                cursor = connection.cursor()
                try:
                    cursor.execute(query, params)
                    row = cursor.fetchone()
                    if not row:
                        return None
                    if isinstance(row, sqlite3.Row):
                        value = row[column] if column else row[0]
                    else:
                        if column:
                            value = row[column]  # type: ignore[index]
                        else:
                            value = row[0]
                    if value is None:
                        return None
                    if isinstance(value, str):
                        return value
                    return str(value)
                finally:
                    cursor.close()
            finally:
                connection.close()

    def _sqlite_upsert_player(self, discord_id: str, steam_id: str) -> None:
        discord_id_str = str(discord_id).strip()
        steam_id_str = str(steam_id).strip()
        if not discord_id_str or not steam_id_str:
            raise ValueError("Discord ID and Steam ID must not be empty.")

        with self._lock:
            connection = self._sqlite_connection()
            try:
                cursor = connection.cursor()
                try:
                    cursor.execute(
                        f'SELECT discord_id FROM "{self.table}" WHERE steam_id = ?',
                        (steam_id_str,),
                    )
                    row = cursor.fetchone()
                    if row:
                        existing_discord = row["discord_id"] if isinstance(row, sqlite3.Row) else row[0]
                        if existing_discord is not None and str(existing_discord) != discord_id_str:
                            raise DuplicateSteamIDError(steam_id_str, str(existing_discord))
                    cursor.execute(
                        f'''
                        INSERT INTO "{self.table}" (discord_id, steam_id)
                        VALUES (?, ?)
                        ON CONFLICT(discord_id) DO UPDATE SET steam_id=excluded.steam_id
                        ''',
                        (discord_id_str, steam_id_str),
                    )
                    connection.commit()
                finally:
                    cursor.close()
            finally:
                connection.close()

    def _sqlite_store_player_name(self, discord_id: str, player_name: str) -> None:
        if not player_name:
            return
        key = f"player_name:{discord_id}"
        with self._lock:
            connection = self._sqlite_connection()
            try:
                cursor = connection.cursor()
                try:
                    cursor.execute(
                        """
                        INSERT INTO metadata (key, value)
                        VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value
                        """,
                        (key, player_name),
                    )
                    connection.commit()
                finally:
                    cursor.close()
            finally:
                connection.close()

    def _save_locked(self) -> None:
        tmp_path = f"{self._path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2, sort_keys=True)
        for attempt in range(3):
            try:
                os.replace(tmp_path, self._path)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.1)

    def _save(self) -> None:
        if self._using_sqlite():
            return
        with self._lock:
            self._save_locked()

    def _players(self) -> Dict[str, str]:
        return self._data.setdefault("players", {})

    def upsert_player(self, discord_id: str, steam_id: str, *, player_name: Optional[str] = None) -> None:
        if self._using_sqlite():
            self._sqlite_upsert_player(discord_id, steam_id)
            if player_name:
                self._sqlite_store_player_name(discord_id, player_name)
            return
        with self._lock:
            players = self._players()
            for existing_discord_id, existing_steam in players.items():
                if existing_steam == steam_id and existing_discord_id != discord_id:
                    raise DuplicateSteamIDError(steam_id, existing_discord_id)
            players[discord_id] = steam_id
            if player_name:
                metadata = self._data.setdefault("metadata", {})
                metadata[f"player_name:{discord_id}"] = player_name
            self._save_locked()

    def fetch_player(self, discord_id: str) -> Optional[str]:
        if self._using_sqlite():
            return self._sqlite_fetch_value(
                f'SELECT steam_id FROM "{self.table}" WHERE discord_id = ?',
                (discord_id,),
            )
        with self._lock:
            return self._players().get(discord_id)

    def fetch_player_name(self, discord_id: str) -> Optional[str]:
        key = f"player_name:{discord_id}"
        if self._using_sqlite():
            return self._sqlite_fetch_value("SELECT value FROM metadata WHERE key = ?", (key,))
        with self._lock:
            metadata = self._data.get("metadata", {})
            if isinstance(metadata, dict):
                value = metadata.get(key)
                if value:
                    return value
        return None
    def fetch_discord_id_for_steam(self, steam_id: str) -> Optional[str]:
        if self._using_sqlite():
            return self._sqlite_fetch_value(
                f'SELECT discord_id FROM "{self.table}" WHERE steam_id = ?',
                (steam_id,),
            )
        with self._lock:
            for discord_id, stored_steam in self._players().items():
                if stored_steam == steam_id:
                    return discord_id
        return None

    def set_metadata(self, key: str, value: str) -> None:
        if self._using_sqlite():
            rows: List[Tuple[Any, ...]] = []
            with self._lock:
                connection = self._sqlite_connection()
                try:
                    cursor = connection.cursor()
                    try:
                        cursor.execute(
                            """
                            INSERT INTO metadata (key, value)
                            VALUES (?, ?)
                            ON CONFLICT(key) DO UPDATE SET value=excluded.value
                            """,
                            (key, value),
                        )
                        connection.commit()
                    finally:
                        cursor.close()
                finally:
                    connection.close()
            return
        with self._lock:
            self._data.setdefault("metadata", {})[key] = value
            self._save_locked()

    def get_metadata(self, key: str) -> Optional[str]:
        if self._using_sqlite():
            return self._sqlite_fetch_value("SELECT value FROM metadata WHERE key = ?", (key,))
        with self._lock:
            return self._data.get("metadata", {}).get(key)

    def delete_metadata(self, key: str) -> None:
        if self._using_sqlite():
            with self._lock:
                connection = self._sqlite_connection()
                try:
                    cursor = connection.cursor()
                    try:
                        cursor.execute("DELETE FROM metadata WHERE key = ?", (key,))
                        connection.commit()
                    finally:
                        cursor.close()
                finally:
                    connection.close()
            return
        with self._lock:
            metadata = self._data.setdefault("metadata", {})
            metadata.pop(key, None)
            self._save_locked()

    def count_players(self) -> int:
        if self._using_sqlite():
            with self._lock:
                connection = self._sqlite_connection()
                try:
                    cursor = connection.cursor()
                    try:
                        cursor.execute(f'SELECT COUNT(*) AS total FROM "{self.table}"')
                        row = cursor.fetchone()
                        if not row:
                            return 0
                        value = row["total"] if isinstance(row, sqlite3.Row) else row[0]
                        return int(value or 0)
                    finally:
                        cursor.close()
                finally:
                    connection.close()
        with self._lock:
            return len(self._players())

    def search_registered_players(self, prefix: str, *, limit: int = 20) -> List[Tuple[str, Optional[str]]]:
        if not prefix:
            return []
        limit = max(1, min(limit, 100))
        prefix_lower = prefix.lower()

        results: List[Tuple[str, Optional[str]]] = []
        if self._using_sqlite():
            with self._lock:
                connection = self._sqlite_connection()
                try:
                    cursor = connection.cursor()
                    try:
                        cursor.execute(
                            f'''
                            SELECT p.steam_id, m.value
                            FROM "{self.table}" p
                            LEFT JOIN metadata m ON m.key = 'player_name:' || p.discord_id
                            WHERE LOWER(COALESCE(m.value, p.steam_id)) LIKE ?
                            LIMIT ?
                            ''',
                            (f"%{prefix_lower}%", limit),
                        )
                        rows = cursor.fetchall()
                    finally:
                        cursor.close()
                finally:
                    connection.close()
            for row in rows:
                if not row:
                    continue
                steam_id = row[0]
                name = row[1] if len(row) > 1 else None
                if isinstance(steam_id, str) and steam_id:
                    results.append((steam_id, name if isinstance(name, str) else None))
            return results

        with self._lock:
            players = list(self._players().items())
            metadata = self._data.get("metadata", {}) if isinstance(self._data, dict) else {}

        for discord_id, steam_id in players:
            if not isinstance(steam_id, str) or not steam_id:
                continue
            name = metadata.get(f"player_name:{discord_id}") if isinstance(metadata, dict) else None
            candidate = name if isinstance(name, str) and name else steam_id
            if prefix_lower in candidate.lower():
                results.append((steam_id, name if isinstance(name, str) and name else None))
                if len(results) >= limit:
                    break
        return results


class VipHttpClient:
    def __init__(
        self,
        credentials: HttpCredentials,
        timeout: Optional[float] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.credentials = credentials
        if timeout is None:
            timeout = credentials.timeout
        if timeout is None:
            timeout = 10.0
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.verify = credentials.verify
        self._token: Optional[str] = None
        self._authenticated = False
        self._bearer_failed = False

    def _endpoint(self, name: str) -> str:
        base = self.credentials.base_url.rstrip("/")
        if not base.lower().endswith("/api"):
            base = f"{base}/api"
        return f"{base}/{name.lstrip('/')}"

    def _headers(self, *, include_auth: bool = True) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        }
        token: Optional[str] = None
        if include_auth:
            token = self._authorization_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        if include_auth and not token:
            headers["Referer"] = self.credentials.base_url
            csrf_token = self.session.cookies.get("csrftoken")
            if csrf_token:
                headers["X-CSRFToken"] = csrf_token
        return headers

    def _authorization_token(self) -> Optional[str]:
        if self.credentials.bearer_token and not self._bearer_failed:
            return self.credentials.bearer_token
        return self._token

    def _has_login_credentials(self) -> bool:
        return bool(self.credentials.username and self.credentials.password)

    def _ensure_authenticated(self) -> None:
        if self._authorization_token():
            return
        if self._authenticated:
            return
        if not self._has_login_credentials():
            return
        self._login()

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
            raise VipHTTPError(
                f"Login failed with status {response.status_code}: {response.text}"
            )

        data = self._parse_json(response)
        if data.get("failed"):
            raise VipHTTPError(f"Login failed: {data.get('error') or data}")

        token = self._extract_token(data)
        self._token = token
        has_session_cookie = bool(self.session.cookies.get("sessionid"))
        if not has_session_cookie and not token:
            raise VipHTTPError("Login succeeded but no session cookie or auth token was provided.")
        self._authenticated = True

    def _refresh_token_if_possible(self) -> bool:
        if not self._has_login_credentials():
            return False
        self._token = None
        self._authenticated = False
        try:
            self._login()
        except VipHTTPError:
            return False
        return True

    def _request_with_reauth(
        self,
        method: str,
        endpoint: str,
        *,
        json_payload: Optional[Dict[str, Any]] = None,
        query_params: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        url = self._endpoint(endpoint)
        self._ensure_authenticated()
        headers = self._headers()
        request_kwargs: Dict[str, Any] = {
            "headers": headers,
            "timeout": self.timeout,
        }
        if json_payload is not None:
            request_kwargs["json"] = json_payload
        if query_params is not None:
            request_kwargs["params"] = query_params
        response = self.session.request(method, url, **request_kwargs)
        if response.status_code == 401:
            if self.credentials.bearer_token:
                self._bearer_failed = True
            logging.warning(
                "HTTP %s %s returned 401: %s",
                method,
                endpoint,
                response.text,
            )
            if self._refresh_token_if_possible():
                headers = self._headers()
                request_kwargs = {
                    "headers": headers,
                    "timeout": self.timeout,
                }
                if json_payload is not None:
                    request_kwargs["json"] = json_payload
                if query_params is not None:
                    request_kwargs["params"] = query_params
                response = self.session.request(method, url, **request_kwargs)
        return response

    @staticmethod
    def _extract_token(payload: Any) -> Optional[str]:
        if isinstance(payload, str):
            return payload or None
        if not isinstance(payload, dict):
            return None

        for key in ("token", "jwt", "access_token", "accessToken"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value

        for key in ("result", "data"):
            nested = payload.get(key)
            token = VipHttpClient._extract_token(nested)
            if token:
                return token
        return None

    def add_vip(
        self,
        player_id: str,
        description: str,
        expiration_iso: Optional[str],
        *,
        player_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "player_id": player_id,
            "description": description,
        }
        if expiration_iso:
            payload["expiration"] = expiration_iso
        if player_name:
            payload["player_name"] = player_name

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

    def search_players(self, prefix: str, *, limit: int = 20) -> List[Tuple[str, str]]:
        if not prefix:
            return []
        params = {"search": prefix, "page": 1, "per_page": max(1, min(limit, 100))}
        try:
            response = self._request_with_reauth("GET", "get_players", query_params=params)
        except requests.exceptions.RequestException as exc:
            raise VipHTTPError(f"HTTP API request failed: {exc}") from exc

        if response.status_code != 200:
            raise VipHTTPError(f"get_players failed with status {response.status_code}: {response.text}")

        data = self._parse_json(response)
        results = []
        for entry in data.get("result", []):
            player_id = entry.get("player_id")
            name = entry.get("name")
            if isinstance(player_id, str) and player_id and isinstance(name, str) and name:
                results.append((player_id, name))
        return results

    @staticmethod
    def _parse_json(response: requests.Response) -> Dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise VipHTTPError(f"Failed to parse JSON response: {response.text}") from exc
        if isinstance(data, dict):
            return data
        raise VipHTTPError("Unexpected response format; expected JSON object.")


class VipService:
    def __init__(self, config: AppConfig, player_directory: Optional[PlayerDirectory] = None) -> None:
        self._config = config
        self._player_directory = player_directory
        self._http_client = VipHttpClient(config.http_credentials) if config.http_credentials else None

    def grant_vip(
        self,
        player_id: str,
        comment: str,
        expiration_iso: Optional[str],
        *,
        player_name: Optional[str] = None,
    ) -> VipGrantResult:
        resolved_player_name = player_name
        if resolved_player_name is None and self._player_directory:
            try:
                resolved_player_name = self._player_directory.lookup_player_name(player_id)
            except Exception:
                logging.exception("PlayerDirectory lookup failed for %s", player_id)

        if not self._http_client:
            raise VipHTTPError("CRCON HTTP credentials are not configured.")

        response = self._http_client.add_vip(
            player_id,
            comment,
            expiration_iso,
            player_name=resolved_player_name,
        )

        message = response.get("result")
        if isinstance(message, dict):
            message = message.get("result") or message
        if message is None:
            message = "HTTP API add_vip succeeded."
        detail = str(message)
        status_lines = [f"HTTP API: {message}"]
        return VipGrantResult(status_lines=status_lines, detail=detail)

    def search_players(self, prefix: str, *, limit: int = 20) -> List[Tuple[str, str]]:
        if not prefix:
            return []

        combined: List[Tuple[str, str]] = []
        remaining = max(1, limit)

        if self._player_directory and remaining > 0:
            try:
                results = self._player_directory.search_players(prefix, limit=remaining)
                combined.extend(results)
                remaining = max(0, limit - len(combined))
            except Exception:
                logging.exception("PlayerDirectory search failed for prefix %s", prefix)

        if self._http_client and remaining > 0:
            try:
                http_results = self._http_client.search_players(prefix, limit=remaining)
                combined.extend(http_results)
            except VipHTTPError:
                logging.exception("HTTP search failed for prefix %s", prefix)

        deduped: List[Tuple[str, str]] = []
        seen: Set[str] = set()
        for player_id, name in combined:
            key = f"{player_id}:{name}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append((player_id, name))
            if len(deduped) >= limit:
                break
        return deduped


class PlayerDirectory:
    def __init__(self, database_url: str, *, cache_ttl: float = 300.0) -> None:
        self._database_url = database_url
        self._engine: Engine = create_engine(database_url, pool_pre_ping=True, future=True)
        self._cache_ttl = cache_ttl
        self._cache: Dict[str, Tuple[float, Optional[str]]] = {}
        self._lock = threading.Lock()
        self._latest_name_query = text(
            """
            SELECT name
            FROM player_names
            WHERE playersteamid_id = :steam_id
            ORDER BY last_seen DESC
            LIMIT 1
            """
        )
        self._search_query = text(
            """
            SELECT DISTINCT ON (pn.playersteamid_id)
                pn.playersteamid_id,
                pn.name
            FROM player_names pn
            WHERE pn.name ILIKE :search
               OR (:exact_id IS NOT NULL AND pn.playersteamid_id = :exact_id)
            ORDER BY pn.playersteamid_id, pn.last_seen DESC
            LIMIT :limit
            """
        )

    def lookup_player_name(self, steam_id: str) -> Optional[str]:
        if not steam_id:
            return None

        now = time.time()
        with self._lock:
            cached = self._cache.get(steam_id)
            if cached and now - cached[0] <= self._cache_ttl:
                return cached[1]

        try:
            with self._engine.connect() as connection:
                row = connection.execute(self._latest_name_query, {"steam_id": steam_id}).first()
                if row is None:
                    name: Optional[str] = None
                else:
                    value = row[0]
                    name = str(value).strip() if value is not None else None
        except SQLAlchemyError:
            logging.exception("Failed to look up player name for %s", steam_id)
            name = None

        with self._lock:
            self._cache[steam_id] = (now, name)
        return name

    def search_players(self, prefix: str, *, limit: int = 20) -> List[Tuple[str, str]]:
        if not prefix:
            return []
        limit = max(1, min(limit, 25))
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(
                    self._search_query,
                    {
                        "search": f"{prefix}%",
                        "exact_id": prefix if prefix.isdigit() else None,
                        "limit": limit,
                    },
                ).fetchall()
        except SQLAlchemyError:
            logging.exception("Failed to search player names for prefix %s", prefix)
            return []

        results: List[Tuple[str, str]] = []
        for row in rows:
            if len(row) < 2:
                continue
            steam_id_raw, name_raw = row[0], row[1]
            if steam_id_raw is None or name_raw is None:
                continue
            steam_id = str(steam_id_raw).strip()
            name = str(name_raw).strip()
            if not steam_id or not name:
                continue
            results.append((steam_id, name))
        return results

@dataclass
class VipGrantResult:
    status_lines: List[str]
    detail: str


class VipRequestModal(Modal):
    def __init__(self, parent_view: "CombinedView", *, default_player_id: Optional[str] = None):
        super().__init__(title="Request VIP Access", custom_id="frontline-pass-vip-modal")
        self._parent_view = parent_view
        self.player_id = TextInput(
            label="T17 / Steam ID",
            placeholder=PLAYER_ID_PLACEHOLDER,
            default=default_player_id,
            custom_id="frontline-pass-vip-player-id-input",
        )
        self.add_item(self.player_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._parent_view.handle_vip_modal_submission(interaction, self.player_id.value)


class PersistentView(View):
    def __init__(self) -> None:
        super().__init__(timeout=None)


class CombinedView(PersistentView):
    def __init__(
        self,
        bot: "FrontlinePassBot",
        config: AppConfig,
        database: Database,
        vip_service: VipService,
        notifier: ModeratorNotifier,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.config = config
        self.database = database
        self.vip_service = vip_service
        self.notifier = notifier
        self._refresh_button_label()

    @discord.ui.button(label="Get VIP", style=ButtonStyle.green, custom_id="frontline-pass-get-vip")
    async def give_vip_button(self, interaction: discord.Interaction, _: Button) -> None:
        discord_id = str(interaction.user.id)
        existing_id = self.database.fetch_player(discord_id)
        modal = VipRequestModal(self, default_player_id=existing_id)
        try:
            await interaction.response.send_modal(modal)
        except discord.HTTPException:
            logging.exception("Failed to open VIP request modal for %s", discord_id)
            error_message = (
                "I couldn't open the VIP request form. Please try again shortly or contact a moderator for assistance."
            )
            if interaction.response.is_done():
                followup = await interaction.followup.send(error_message, ephemeral=True, wait=True)
                schedule_ephemeral_cleanup(interaction, message=followup)
            else:
                await interaction.response.send_message(error_message, ephemeral=True)
                schedule_ephemeral_cleanup(interaction)


    def _refresh_button_label(self) -> None:
        self.give_vip_button.label = f"Get VIP ({self.bot.vip_duration_hours:g} hours)"

    def refresh_vip_label(self) -> None:
        self._refresh_button_label()

    async def handle_vip_modal_submission(self, interaction: discord.Interaction, steam_id: str) -> None:
        steam_id = steam_id.strip()
        if not steam_id:
            await interaction.response.send_message("T17 ID cannot be empty.", ephemeral=True)
            schedule_ephemeral_cleanup(interaction)
            return

        await interaction.response.defer(ephemeral=True)

        discord_id = str(interaction.user.id)
        previous_steam_id = self.database.fetch_player(discord_id)
        resolved_name = self.bot.lookup_player_name(steam_id)

        try:
            self.database.upsert_player(discord_id, steam_id, player_name=resolved_name)
        except DuplicateSteamIDError as exc:
            duplicate_message = (
                "Error: That T17 ID is already linked to another Discord account. "
                "A moderator has been notified."
            )
            followup_message = await interaction.followup.send(
                duplicate_message,
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)
            await self.notifier.notify_duplicate(interaction, exc.steam_id, exc.existing_discord_id)
            return

        display_id = f"{steam_id} ({resolved_name})" if resolved_name else steam_id
        if previous_steam_id and previous_steam_id != steam_id:
            status_note = f"Saved Player-ID updated to {display_id}."
        elif not previous_steam_id:
            status_note = f"Player-ID {display_id} recorded for fast access next time."
        else:
            status_note = None

        await self._grant_vip_for_player(
            interaction,
            steam_id,
            resolved_name or self.database.fetch_player_name(discord_id),
            status_note=status_note,
        )

    async def _grant_vip_for_player(
        self,
        interaction: discord.Interaction,
        steam_id: str,
        player_name: Optional[str],
        *,
        status_note: Optional[str] = None,
    ) -> None:
        player_name = player_name or self.database.fetch_player_name(str(interaction.user.id))
        player_display = f"{steam_id} ({player_name})" if player_name else steam_id
        duration_hours = self.bot.vip_duration_hours
        local_time = datetime.now(self.config.timezone)
        expiration_time_local = local_time + timedelta(hours=duration_hours)
        expiration_time_utc = expiration_time_local.astimezone(pytz.utc)
        expiration_time_utc_str = expiration_time_utc.strftime("%Y-%m-%d %H:%M:%S")
        expiration_time_iso = expiration_time_utc.isoformat()
        comment = f"Discord VIP for {interaction.user.display_name} until {expiration_time_utc_str} UTC"

        try:
            result = await asyncio.to_thread(
                self.vip_service.grant_vip,
                steam_id,
                comment,
                expiration_time_iso,
                player_name=player_name,
            )
        except VipHTTPError as exc:
            logging.exception("Failed to grant VIP for player %s", steam_id)
            error_lines = [f"Error: VIP status could not be set: {exc}"]
            if status_note:
                error_lines.append(status_note)
            if isinstance(interaction.client, FrontlinePassBot):
                await interaction.client.refresh_announcement_message()
            followup_message = await interaction.followup.send(
                "\n\n".join(error_lines),
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)
            return
        except Exception as exc:  # pragma: no cover
            logging.exception("Unexpected error while granting VIP for player %s: %s", steam_id, exc)
            error_lines = ["An unexpected error occurred while setting VIP status."]
            if status_note:
                error_lines.append(status_note)
            if isinstance(interaction.client, FrontlinePassBot):
                await interaction.client.refresh_announcement_message()
            followup_message = await interaction.followup.send(
                "\n\n".join(error_lines),
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)
            return

        readable_expiration = expiration_time_local.strftime("%Y-%m-%d %H:%M:%S %Z")
        logging.info(
            "Granted VIP for player %s until %s UTC (%s)",
            player_display,
            expiration_time_utc_str,
            "; ".join(result.status_lines),
        )
        self.database.set_metadata(LAST_GRANT_METADATA_KEY, datetime.now(timezone.utc).isoformat())
        if isinstance(interaction.client, FrontlinePassBot):
            await interaction.client.refresh_announcement_message()

        header_lines = []
        header_lines.append(f"You now have VIP for {self.config.vip_duration_label} hours!")
        header_lines.append(f"Linked ID: {player_display}")
        header_lines.append(f"Expiration: {readable_expiration}")
        if status_note:
            header_lines.append(status_note)
        status_summary = "\n".join(f"- {line}" for line in result.status_lines)
        message_body = "\n".join(header_lines) + "\n\n**Status**:\n" + status_summary
        followup_message = await interaction.followup.send(
            message_body,
            ephemeral=True,
            wait=True,
        )
        schedule_ephemeral_cleanup(interaction, message=followup_message)
        await self._maybe_remove_temp_vip_role(interaction)

    async def complete_registration(
        self,
        interaction: discord.Interaction,
        steam_id: str,
        *,
        player_name: Optional[str] = None,
    ) -> None:
        steam_id = steam_id.strip()
        if not steam_id:
            if interaction.response.is_done():
                followup_message = await interaction.followup.send(
                    "T17 ID cannot be empty.",
                    ephemeral=True,
                    wait=True,
                )
                schedule_ephemeral_cleanup(interaction, message=followup_message)
            else:
                await interaction.response.send_message("T17 ID cannot be empty.", ephemeral=True)
                schedule_ephemeral_cleanup(interaction)
            return

        discord_id = str(interaction.user.id)
        previous_steam_id = self.database.fetch_player(discord_id)
        resolved_name = player_name or self.bot.lookup_player_name(steam_id)

        try:
            self.database.upsert_player(discord_id, steam_id, player_name=resolved_name)
        except DuplicateSteamIDError as exc:
            duplicate_message = (
                "Error: That T17 ID is already linked to another Discord account. "
                "A moderator has been notified."
            )
            if interaction.response.is_done():
                followup_message = await interaction.followup.send(
                    duplicate_message,
                    ephemeral=True,
                    wait=True,
                )
                schedule_ephemeral_cleanup(interaction, message=followup_message)
            else:
                await interaction.response.send_message(duplicate_message, ephemeral=True)
                schedule_ephemeral_cleanup(interaction)
            await self.notifier.notify_duplicate(interaction, exc.steam_id, exc.existing_discord_id)
            return

        display_id = f"{steam_id} ({resolved_name})" if resolved_name else steam_id
        if previous_steam_id and previous_steam_id != steam_id:
            success_message = f"Your T17 ID was updated to {display_id}."
        else:
            success_message = f"Your T17 ID {display_id} has been saved!"

        if interaction.response.is_done():
            followup_message = await interaction.followup.send(
                success_message,
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)
        else:
            await interaction.response.send_message(success_message, ephemeral=True)
            schedule_ephemeral_cleanup(interaction)

        await self.bot.refresh_announcement_message()

    async def _maybe_remove_temp_vip_role(self, interaction: discord.Interaction) -> None:
        role_id = getattr(self.config, "vip_temp_role_id", None)
        if not role_id:
            return
        guild = interaction.guild
        if guild is None:
            return
        # interaction.user can be a Member in guild contexts
        user = interaction.user
        if not isinstance(user, discord.Member):
            try:
                # Try to fetch member if possible
                user = await guild.fetch_member(interaction.user.id)
            except discord.DiscordException:
                return
        role = guild.get_role(role_id)
        if role is None:
            return
        if role in getattr(user, "roles", []):
            try:
                await user.remove_roles(role, reason="Frontline Pass: remove temporary VIP role after claim")
                logging.info("Removed temporary VIP role %s from %s after claim", role_id, user.id)
            except discord.DiscordException:
                logging.exception("Failed to remove temporary VIP role %s from %s", role_id, user.id)


def create_database(config: AppConfig) -> Database:
    return Database(config.database_path, config.database_table)


def create_player_directory(config: AppConfig) -> Optional[PlayerDirectory]:
    if config.crcon_database_url:
        logging.info(
            "CRCON database URL %s provided but player directory lookups are disabled for manual registration.",
            config.crcon_database_url,
        )
    else:
        logging.info("CRCON database URL not configured; player directory search remains disabled.")
    return None


class FrontlinePassBot(commands.Bot):
    def __init__(self, config: AppConfig, database: Database, vip_service: VipService, player_directory: Optional[PlayerDirectory] = None) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.database = database
        self.vip_service = vip_service
        self.player_directory = player_directory
        self.notifier = ModeratorNotifier(config)
        self.persistent_view: Optional[CombinedView] = None
        self.announcement_manager = AnnouncementManager(config, database)
        self._vip_duration_hours = self._load_vip_duration()
        self._autocomplete_cache: Dict[str, str] = {}

    def lookup_player_name(self, steam_id: str) -> Optional[str]:
        if steam_id in self._autocomplete_cache:
            return self._autocomplete_cache.get(steam_id)
        if self.player_directory:
            name = self.player_directory.lookup_player_name(steam_id)
            if name:
                self._autocomplete_cache[steam_id] = name
            return name
        return self._autocomplete_cache.get(steam_id)

    def search_player_directory(self, prefix: str, *, limit: int = 20) -> List[Tuple[str, str]]:
        if not self.player_directory:
            return []
        return self.player_directory.search_players(prefix, limit=limit)

    async def setup_hook(self) -> None:
        self.persistent_view = CombinedView(self, self.config, self.database, self.vip_service, self.notifier)
        self.add_view(self.persistent_view)
        await self._register_commands()
        # Optional: sync commands to specific guilds for instant availability
        guild_ids_raw = os.getenv("COMMAND_GUILD_IDS") or os.getenv("COMMAND_GUILD_ID")
        synced_any_guild = False
        if guild_ids_raw:
            try:
                guild_ids = [int(x.strip()) for x in guild_ids_raw.split(",") if x.strip()]
            except ValueError:
                logging.warning("Invalid COMMAND_GUILD_IDS value %r; falling back to global sync.", guild_ids_raw)
                guild_ids = []
            for gid in guild_ids:
                try:
                    guild_obj = discord.Object(id=gid)
                    self.tree.copy_global_to(guild=guild_obj)
                    await self.tree.sync(guild=guild_obj)
                    synced_any_guild = True
                    logging.info("Slash commands synced to guild %s", gid)
                except discord.DiscordException:
                    logging.exception("Failed to sync slash commands to guild %s", gid)
        if not synced_any_guild:
            await self.tree.sync()
            logging.info("Slash commands globally synced (may take up to 1 hour to appear).")
        # Log registered commands
        try:
            cmd_names = ", ".join(sorted(cmd.name for cmd in self.tree.get_commands()))
            logging.info("Registered slash commands: %s", cmd_names)
        except Exception:
            logging.exception("Unable to list registered slash commands")

    async def on_ready(self) -> None:
        logging.info("Bot is ready: %s", self.user)
        db_info = self.database.info()
        logging.info(
            "Database backend=%s path=%s; current VIP duration=%.2f hours",
            db_info.get("backend"),
            db_info.get("path"),
            self.vip_duration_hours,
        )
        await self.refresh_announcement_message()

    async def refresh_announcement_message(self) -> None:
        if not self.persistent_view:
            logging.error("Persistent view not initialised; cannot refresh announcement message.")
            return
        await self.announcement_manager.ensure(self, self.persistent_view, self.vip_duration_hours)

    @property
    def vip_duration_hours(self) -> float:
        return self._vip_duration_hours

    def _user_has_moderator_privileges(self, user: discord.abc.User) -> bool:
        permissions = getattr(user, "guild_permissions", None)  # type: ignore[attr-defined]
        if permissions and permissions.administrator:
            return True
        role_id = self.config.moderator_role_id
        if role_id and hasattr(user, "roles"):
            for role in getattr(user, "roles", []):  # type: ignore[assignment]
                if getattr(role, "id", None) == role_id:
                    return True
        return False

    def _load_vip_duration(self) -> float:
        stored_value = self.database.get_metadata(VIP_DURATION_METADATA_KEY)
        if stored_value:
            try:
                parsed = float(stored_value)
                if parsed > 0:
                    logging.info("Loaded persisted VIP duration: %.2f hours", parsed)
                    return parsed
                logging.warning(
                    "Ignoring persisted VIP duration %.2f because it is not greater than zero.", parsed
                )
            except ValueError:
                logging.warning("Failed to parse stored VIP duration %r; falling back to config.", stored_value)
        # Persist default so future restarts know it
        self.database.set_metadata(VIP_DURATION_METADATA_KEY, str(self.config.vip_duration_hours))
        return self.config.vip_duration_hours

    async def set_vip_duration_hours(self, hours: float) -> None:
        self._vip_duration_hours = hours
        self.database.set_metadata(VIP_DURATION_METADATA_KEY, str(hours))
        if self.persistent_view:
            self.persistent_view.refresh_vip_label()
        await self.refresh_announcement_message()

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
                self.vip_duration_hours,
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


        @self.tree.command(
            name="set_vip_duration",
            description="Set the VIP duration in hours.",
        )
        @app_commands.describe(hours="Number of hours that VIP access should last")
        async def set_vip_duration(interaction: discord.Interaction, hours: float) -> None:
            if not self._user_has_moderator_privileges(interaction.user):
                await interaction.response.send_message(
                    "You need moderator permissions to use this command.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            if hours <= 0:
                await interaction.response.send_message(
                    "VIP duration must be greater than zero hours.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            await interaction.response.defer(ephemeral=True)
            await self.set_vip_duration_hours(hours)
            followup_message = await interaction.followup.send(
                f"VIP duration updated to {hours:g} hours.",
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)

        # Alias command with requested name: /setvipduration (same behavior)
        @self.tree.command(
            name="setvipduration",
            description="Set the VIP duration in hours (alias).",
        )
        @app_commands.describe(hours="Number of hours that VIP access should last")
        async def setvipduration(interaction: discord.Interaction, hours: float) -> None:
            if not self._user_has_moderator_privileges(interaction.user):
                await interaction.response.send_message(
                    "You need moderator permissions to use this command.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            if hours <= 0:
                await interaction.response.send_message(
                    "VIP duration must be greater than zero hours.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            await interaction.response.defer(ephemeral=True)
            await self.set_vip_duration_hours(hours)
            followup_message = await interaction.followup.send(
                f"VIP duration updated to {hours:g} hours.",
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)

        @self.tree.command(
            name="getvipduration",
            description="Show the current VIP duration in hours.",
        )
        async def getvipduration(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                f"Current VIP duration: {self.vip_duration_hours:g} hours.",
                ephemeral=True,
            )
            schedule_ephemeral_cleanup(interaction)

        @self.tree.command(
            name="health",
            description="Show bot health: VIP duration, player count, and DB backend/path.",
        )
        async def health(interaction: discord.Interaction) -> None:
            db_info = self.database.info()
            count = self.database.count_players()
            msg = (
                f"VIP duration: {self.vip_duration_hours:g} hours\n"
                f"Registered players: {count}\n"
                f"Database: {db_info.get('backend')} at {db_info.get('path')}"
            )
            await interaction.response.send_message(msg, ephemeral=True)
            schedule_ephemeral_cleanup(interaction)

        @self.tree.command(
            name="assignvip",
            description="Assign a temporary VIP Discord role to a member so they can register and claim VIP.",
        )
        @app_commands.describe(member="Select the server member to grant temporary VIP role to")
        async def assignvip(interaction: discord.Interaction, member: discord.Member) -> None:
            # Permission check
            if not self._user_has_moderator_privileges(interaction.user):
                await interaction.response.send_message(
                    "You need moderator permissions to use this command.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            role_id = self.config.vip_temp_role_id
            if not role_id:
                await interaction.response.send_message(
                    "VIP_TEMP_ROLE_ID is not configured. Set it in your environment or config.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            guild = interaction.guild
            if guild is None:
                await interaction.response.send_message(
                    "This command can only be used inside a server (guild).",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            role = guild.get_role(role_id)
            if role is None:
                await interaction.response.send_message(
                    f"Could not find role with ID {role_id} in this server.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            try:
                await member.add_roles(role, reason="Frontline Pass: temporary VIP role for registration/claim")
            except discord.Forbidden:
                await interaction.response.send_message(
                    "I don't have permission to assign that role. Ensure my role is above the VIP role.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return
            except discord.DiscordException:
                logging.exception("Failed to add temporary VIP role %s to %s", role_id, member.id)
                await interaction.response.send_message(
                    "Failed to assign the temporary VIP role due to an unexpected error.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            # Optionally post guidance in the VIP claim channel
            claim_channel_id = self.config.vip_claim_channel_id or self.config.channel_id
            channel_mention = f"<#{claim_channel_id}>" if claim_channel_id else "the VIP channel"
            try:
                await interaction.response.send_message(
                    (
                        f"Assigned {role.mention} to {member.mention}.\n"
                        f"Please go to {channel_mention} and press Get VIP, then enter the Player-ID when prompted.\n"
                        f"After you claim VIP, your temporary Discord role will be removed automatically."
                    ),
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
            except discord.InteractionResponded:
                # Fallback if already responded earlier (shouldn't happen in happy path)
                with contextlib.suppress(discord.DiscordException):
                    await interaction.followup.send(
                        (
                            f"Assigned {role.mention} to {member.mention}. Go to {channel_mention}, press Get VIP, and enter the Player-ID to claim VIP."
                        ),
                        ephemeral=True,
                        wait=False,
                    )


def create_bot(
    config: AppConfig,
    database: Database,
    vip_service: VipService,
    player_directory: Optional[PlayerDirectory] = None,
) -> commands.Bot:
    return FrontlinePassBot(config, database, vip_service, player_directory)


def main() -> None:
    config = load_config()
    database = create_database(config)
    player_directory = create_player_directory(config)
    vip_service = VipService(config, player_directory)
    bot = create_bot(config, database, vip_service, player_directory)
    bot.run(config.discord_token)


if __name__ == "__main__":
    main()
