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


def build_announcement_embed(config: AppConfig, database: "Database", vip_duration_hours: float) -> discord.Embed:
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
            f"VIP duration: **{vip_duration_hours:g} hours**.\n"
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
    rcon_host: str
    rcon_port: int
    rcon_password: str
    rcon_version: int
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
    rcon_host = require_str("RCON_HOST")
    rcon_port = require_int("RCON_PORT")
    rcon_password = require_str("RCON_PASSWORD")
    rcon_version = optional_int("RCON_VERSION") or 2

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

    http_base_url_raw = get_value("CRCON_HTTP_BASE_URL")
    http_bearer_token = get_value("CRCON_HTTP_BEARER_TOKEN")
    http_username = get_value("CRCON_HTTP_USERNAME")
    http_password = get_value("CRCON_HTTP_PASSWORD")
    http_verify = optional_bool("CRCON_HTTP_VERIFY", default=True)
    http_timeout = optional_float("CRCON_HTTP_TIMEOUT", default=20.0) or 20.0

    http_credentials: Optional[HttpCredentials] = None
    trimmed_base = str(http_base_url_raw).strip() if http_base_url_raw else ""
    trimmed_token = str(http_bearer_token).strip() if http_bearer_token else ""
    trimmed_username = str(http_username).strip() if http_username else ""
    password_provided = bool(http_password and str(http_password).strip())

    if any((trimmed_base, trimmed_token, trimmed_username, http_password)):
        normalized_base = trimmed_base.rstrip("/")
        if not normalized_base:
            errors.append("CRCON_HTTP_BASE_URL is required when using the HTTP API integration")
        else:
            lowered_base = normalized_base.lower()
            if lowered_base.endswith("/api") or "/api/" in lowered_base:
                errors.append(
                    "CRCON_HTTP_BASE_URL should not include '/api'. Provide the host only "
                    "(e.g. https://example.com:8010)."
                )
        if not trimmed_token and not (trimmed_username and password_provided):
            errors.append(
                "Provide either CRCON_HTTP_BEARER_TOKEN or both CRCON_HTTP_USERNAME and CRCON_HTTP_PASSWORD "
                "when enabling the HTTP API integration"
            )
        if trimmed_username and not password_provided:
            errors.append("CRCON_HTTP_PASSWORD is required when CRCON_HTTP_USERNAME is provided")

        if normalized_base and (trimmed_token or (trimmed_username and password_provided)):
            http_credentials = HttpCredentials(
                base_url=normalized_base,
                bearer_token=trimmed_token or None,
                username=trimmed_username or None,
                password=str(http_password).strip() if password_provided else None,
                verify=http_verify if http_verify is not None else True,
                timeout=http_timeout,
            )

    crcon_database_url_raw = get_value("CRCON_DATABASE_URL")
    crcon_database_url = str(crcon_database_url_raw).strip() if crcon_database_url_raw else None

    if errors:
        raise RuntimeError("Configuration error(s): " + "; ".join(errors))

    assert vip_duration_hours is not None
    assert channel_id is not None
    assert rcon_port is not None
    assert rcon_version is not None

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
        except (binascii.Error, AttributeError) as exc:
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
    def __init__(self, config: AppConfig, player_directory: Optional[PlayerDirectory] = None) -> None:
        self._config = config
        self._http_client = VipHttpClient(config.http_credentials) if config.http_credentials else None
        self._player_directory = player_directory

    def grant_vip(
        self,
        player_id: str,
        comment: str,
        expiration_iso: Optional[str],
        *,
        player_name: Optional[str] = None,
    ) -> VipGrantResult:
        status_lines: List[str] = []
        detail = ""
        overall_success = False
        resolved_player_name = player_name
        if resolved_player_name is None and self._player_directory:
            resolved_player_name = self._player_directory.lookup_player_name(player_id)

        if self._http_client:
            try:
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

    def search_players(self, prefix: str, *, limit: int = 20) -> List[Tuple[str, str]]:
        if not prefix:
            return []
        results: List[Tuple[str, str]] = []
        if self._player_directory:
            try:
                results.extend(self._player_directory.search_players(prefix, limit=limit))
            except Exception:
                logging.exception("PlayerDirectory search failed for prefix %s", prefix)

        if self._http_client:
            try:
                http_results = self._http_client.search_players(prefix, limit=limit)
                results.extend(http_results)
            except VipHTTPError:
                logging.exception("HTTP search failed for prefix %s", prefix)

        # Deduplicate while preserving order
        seen: Set[str] = set()
        deduped: List[Tuple[str, str]] = []
        for player_id, name in results:
            key = f"{player_id}:{name}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append((player_id, name))
            if len(deduped) >= limit:
                break
        return deduped


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

        # Ensure the channel supports send()
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.DMChannel)):
            logging.error("Configured moderation channel %s is not a messageable channel.", self._channel_id)
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
                name: Optional[str]
                if row is None:
                    name = None
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
            player_name = str(name_raw).strip()
            if steam_id and player_name:
                results.append((steam_id, player_name))
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


@dataclass
class VipGrantResult:
    status_lines: List[str]
    detail: str


class PlayerIDModal(Modal):
    def __init__(self, parent_view: "CombinedView"):
        super().__init__(title="Please input Player-ID", custom_id="frontline-pass-player-id-modal")
        self._parent_view = parent_view
        self.database = parent_view.database
        self.notifier = parent_view.notifier
        self.player_id = TextInput(
            label="T17 / Steam ID",
            placeholder=(
                "Link your HLL player-id# to your discord account to get the VIP pass. "
                "Go to https://hllrecords.com/ - and search for your player name. "
                "Open up your player details and copy the Xbox Game Pass / Epic Games string of numbers into the box below."
            ),
            custom_id="frontline-pass-player-id-input",
        )
        self.add_item(self.player_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        steam_id = self.player_id.value
        await self._parent_view.complete_registration(
            interaction,
            steam_id,
            player_name=self._parent_view.bot.lookup_player_name(steam_id.strip()),
        )


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

    @discord.ui.button(label="Register", style=ButtonStyle.danger, custom_id="frontline-pass-register")
    async def register_button(self, interaction: discord.Interaction, _: Button) -> None:
        discord_id = str(interaction.user.id)
        existing = self.database.fetch_player(discord_id)
        if existing:
            stored_name = self.database.fetch_player_name(discord_id)
            display = f"{existing} ({stored_name})" if stored_name else existing
            await interaction.response.send_message(
                f"You're already registered. Your T17 ID `{display}` is linked to your Discord account.",
                ephemeral=True,
            )
            schedule_ephemeral_cleanup(interaction)
            return

        await interaction.response.send_modal(PlayerIDModal(self))

    @discord.ui.button(label="Get VIP", style=ButtonStyle.green, custom_id="frontline-pass-get-vip")
    async def give_vip_button(self, interaction: discord.Interaction, _: Button) -> None:
        discord_id = str(interaction.user.id)
        steam_id = self.database.fetch_player(discord_id)

        if not steam_id:
            await interaction.response.send_message(
                "You are not linked to a T17 account. Please register first.",
                ephemeral=True,
            )
            schedule_ephemeral_cleanup(interaction)
            return

        player_name = self.database.fetch_player_name(discord_id)
        player_display = f"{steam_id} ({player_name})" if player_name else steam_id
        duration_hours = self.bot.vip_duration_hours
        local_time = datetime.now(self.config.timezone)
        expiration_time_local = local_time + timedelta(hours=duration_hours)
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
                player_name=player_name,
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
            player_display,
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
                f"Linked ID: {player_display}\n"
                f"Expiration: {readable_expiration}\n\n**Status**:\n{status_summary}"
            ),
            ephemeral=True,
            wait=True,
        )
        schedule_ephemeral_cleanup(interaction, message=followup_message)
        # If the user had a temporary VIP Discord role for registration/claim, remove it now.
        await self._maybe_remove_temp_vip_role(interaction)


    def _refresh_button_label(self) -> None:
        self.give_vip_button.label = f"Get VIP ({self.bot.vip_duration_hours:g} hours)"

    def refresh_vip_label(self) -> None:
        self._refresh_button_label()

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
    if not config.crcon_database_url:
        return None
    try:
        directory = PlayerDirectory(config.crcon_database_url)
    except Exception:
        logging.exception("Failed to initialise PlayerDirectory for %s", config.crcon_database_url)
        return None
    return directory


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
            name="register_player",
            description="Link your T17 ID to your Discord account.",
        )
        async def register_player(interaction: discord.Interaction) -> None:
            parent_view = self.persistent_view
            if not parent_view:
                await interaction.response.send_message(
                    "The registration system is not ready yet. Please try again shortly.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            discord_id = str(interaction.user.id)
            existing = parent_view.database.fetch_player(discord_id)
            if existing:
                stored_name = parent_view.database.fetch_player_name(discord_id)
                display = f"{existing} ({stored_name})" if stored_name else existing
                await interaction.response.send_message(
                    f"You're already registered. Your T17 ID `{display}` is linked to your Discord account.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            await interaction.response.send_modal(PlayerIDModal(parent_view))

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
                        f"Please go to {channel_mention} and use the Register button (or /register_player), then press Get VIP.\n"
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
                            f"Assigned {role.mention} to {member.mention}. Go to {channel_mention} to register and claim VIP."
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
