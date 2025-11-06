from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import discord
import json5
import pytz
import requests
from discord import ButtonStyle, app_commands

try:
    from discord.abc import MessageableChannel
except ImportError:  # discord.py>=2.4 renamed MessageableChannel -> Messageable
    from discord.abc import Messageable as MessageableChannel

from discord.ext import commands
from discord.ui import Button, Modal, TextInput, View
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)

ANNOUNCEMENT_TITLE = "VIP Control Center"
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


def build_announcement_embed(
    config: AppConfig,
    vip_duration_hours: float,
    last_grant_at: Optional[datetime],
) -> discord.Embed:
    if last_grant_at:
        local_dt = last_grant_at.astimezone(config.timezone)
        last_grant_text = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    else:
        last_grant_text = "No VIP grants yet."

    description_lines = [
        "Use the button below to activate your VIP access.",
        'When registering you need to add your player_id number string i.e. "2805d5bbe14b6ec432f82e5cb859d012" from https://hllrecords.com.',
        f"VIP duration: **{vip_duration_hours:g} hours**.",
    ]
    embed = discord.Embed(
        title=ANNOUNCEMENT_TITLE,
        description="\n".join(description_lines),
        color=0x2F3136,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Last VIP Grant", value=last_grant_text, inline=True)
    embed.add_field(name="Local Timezone", value=config.timezone_name, inline=True)
    embed.set_footer(text="Buttons stay active across restarts.")
    return embed


class AnnouncementManager:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._message_id: Optional[int] = None

    async def ensure(
        self,
        bot: commands.Bot,
        view: View,
        vip_duration_hours: float,
        last_grant_at: Optional[datetime],
        *,
        force_new: bool = False,
    ) -> Optional[discord.Message]:
        destination = await self._resolve_destination(bot)
        if destination is None:
            return None

        if force_new:
            await self._delete_existing(destination, bot)
            message = None
        else:
            message = await self._locate_message(destination, bot)

        embed = build_announcement_embed(self._config, vip_duration_hours, last_grant_at)
        if message:
            await message.edit(embed=embed, view=view)
            self._message_id = message.id
            logging.info("Reattached control view to existing message %s", message.id)
            return message

        sent_message = await destination.send(embed=embed, view=view)
        self._message_id = sent_message.id
        logging.info(
            "Posted announcement message with id %s. Set for future updates within this session.",
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
    ) -> Optional[discord.Message]:
        for message_id in self._candidate_message_ids():
            try:
                message = await destination.fetch_message(message_id)
                return message
            except discord.NotFound:
                continue
            except discord.DiscordException:
                logging.exception("Failed to fetch announcement message %s", message_id)

        async for message in destination.history(limit=50):
            if message.author == bot.user and message.embeds:
                if message.embeds[0].title == ANNOUNCEMENT_TITLE:
                    self._message_id = message.id
                    return message
        return None

    async def _delete_existing(self, destination: MessageableChannel, bot: commands.Bot) -> None:
        for message_id in self._candidate_message_ids():
            try:
                message = await destination.fetch_message(message_id)
            except discord.NotFound:
                continue
            except discord.DiscordException:
                logging.exception("Failed to fetch announcement message %s for deletion", message_id)
                continue
            await self._delete_message(message)

        async for message in destination.history(limit=50):
            if message.author == bot.user and message.embeds:
                if message.embeds[0].title == ANNOUNCEMENT_TITLE:
                    await self._delete_message(message)

        self._message_id = None

    async def _delete_message(self, message: discord.Message) -> None:
        try:
            await message.delete()
        except discord.DiscordException:
            logging.exception("Failed to delete announcement message %s", message.id)

    def _candidate_message_ids(self) -> List[int]:
        candidate_ids: List[int] = []
        if self._message_id:
            candidate_ids.append(self._message_id)
        if self._config.announcement_message_id:
            candidate_ids.append(self._config.announcement_message_id)
        return candidate_ids


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
    announcement_message_id: Optional[int] = None
    http_credentials: Optional[HttpCredentials] = None
    moderator_role_id: Optional[int] = None
    vip_temp_role_id: Optional[int] = None
    vip_claim_channel_id: Optional[int] = None

    @property
    def vip_duration_label(self) -> str:
        return f"{self.vip_duration_hours:g}"


def load_config() -> AppConfig:
    load_dotenv()
    raw_config, _ = _load_raw_config()
    config_values = {str(key).upper(): value for key, value in raw_config.items()}
    errors: List[str] = []

    config_aliases: Dict[str, Tuple[str, ...]] = {
        "CRCON_HTTP_BASE_URL": ("API_BASE_URL",),
        "CRCON_HTTP_BEARER_TOKEN": ("API_BEARER_TOKEN",),
        "CRCON_HTTP_USERNAME": ("API_USERNAME",),
        "CRCON_HTTP_PASSWORD": ("API_PASSWORD",),
        "CRCON_HTTP_VERIFY": ("API_VERIFY",),
        "CRCON_HTTP_TIMEOUT": ("API_TIMEOUT",),
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

    announcement_message_id = optional_int("ANNOUNCEMENT_MESSAGE_ID")
    moderator_role_id = optional_int("MODERATOR_ROLE_ID")
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
    trimmed_password = str(http_password).strip() if http_password else ""

    if any((trimmed_base, trimmed_token, trimmed_username, trimmed_password)):
        normalized_base = trimmed_base.rstrip("/")
        if not normalized_base:
            errors.append("CRCON_HTTP_BASE_URL is required when using the HTTP API integration")
        else:
            lowered_base = normalized_base.lower()
            if lowered_base.endswith("/api") or "/api/" in lowered_base:
                errors.append(
                    "CRCON_HTTP_BASE_URL should not include '/api'. Provide the host only (e.g. https://example.com:8010)."
                )
        if not trimmed_token and not (trimmed_username and trimmed_password):
            errors.append(
                "Provide either CRCON_HTTP_BEARER_TOKEN or both CRCON_HTTP_USERNAME and CRCON_HTTP_PASSWORD when enabling the HTTP API integration"
            )
        if trimmed_username and not trimmed_password:
            errors.append("CRCON_HTTP_PASSWORD is required when CRCON_HTTP_USERNAME is provided")
        if normalized_base and (trimmed_token or (trimmed_username and trimmed_password)):
            http_credentials = HttpCredentials(
                base_url=normalized_base,
                bearer_token=trimmed_token or None,
                username=trimmed_username or None,
                password=trimmed_password or None,
                verify=http_verify if http_verify is not None else True,
                timeout=http_timeout,
            )

    if http_credentials is None:
        errors.append("CRCON_HTTP_BASE_URL and authentication details are required to grant VIP via HTTP API")

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
        announcement_message_id=announcement_message_id,
        http_credentials=http_credentials,
        moderator_role_id=moderator_role_id,
        vip_temp_role_id=vip_temp_role_id,
        vip_claim_channel_id=vip_claim_channel_id,
    )


class VipHTTPError(Exception):
    """Raised when the HTTP API integration fails."""


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
            raise VipHTTPError(f"Login failed with status {response.status_code}: {response.text}")

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
            raise VipHTTPError(f"add_vip failed with status {response.status_code}: {response.text}")

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


class VipService:
    def __init__(self, config: AppConfig) -> None:
        if not config.http_credentials:
            raise RuntimeError("HTTP credentials are required for VIP service.")
        self._http_client = VipHttpClient(config.http_credentials)

    def grant_vip(
        self,
        player_id: str,
        comment: str,
        expiration_iso: Optional[str],
        *,
        player_name: Optional[str] = None,
    ) -> VipGrantResult:
        response = self._http_client.add_vip(
            player_id,
            comment,
            expiration_iso,
            player_name=player_name,
        )
        message: Any = response.get("result")
        if isinstance(message, dict):
            message = message.get("result") or message
        if message is None:
            message = "HTTP API add_vip succeeded."
        detail = str(message)
        return VipGrantResult(status_lines=[f"HTTP API: {detail}"], detail=detail)


class VipRequestModal(Modal):
    def __init__(self, parent_view: "CombinedView") -> None:
        super().__init__(title="Request VIP Access", custom_id="frontline-pass-vip-modal")
        self._parent_view = parent_view
        self.player_id = TextInput(
            label="T17 / Steam ID",
            placeholder=PLAYER_ID_PLACEHOLDER,
            custom_id="frontline-pass-vip-player-id-input",
            min_length=32,
            max_length=32,
            style=discord.TextStyle.short,
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
        vip_service: VipService,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.config = config
        self.vip_service = vip_service
        self._refresh_button_label()

    @discord.ui.button(label="Get VIP", style=ButtonStyle.green, custom_id="frontline-pass-get-vip")
    async def give_vip_button(self, interaction: discord.Interaction, _: Button) -> None:
        modal = VipRequestModal(self)
        try:
            await interaction.response.send_modal(modal)
        except discord.HTTPException:
            logging.exception("Failed to open VIP request modal for %s", interaction.user.id)
            error_message = "I couldn't open the VIP request form. Please try again shortly."
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

        steam_id = steam_id.lower()
        if len(steam_id) != 32:
            await interaction.response.send_message(
                "Player-ID must be a 32-character string copied from https://hllrecords.com.",
                ephemeral=True,
            )
            schedule_ephemeral_cleanup(interaction)
            return

        await interaction.response.defer(ephemeral=True)
        await self._grant_vip_for_player(interaction, steam_id)

    async def _grant_vip_for_player(self, interaction: discord.Interaction, steam_id: str) -> None:
        duration_hours = self.bot.vip_duration_hours
        local_time = datetime.now(self.config.timezone)
        expiration_time_local = local_time + timedelta(hours=duration_hours)
        expiration_time_utc = expiration_time_local.astimezone(pytz.utc)
        expiration_time_iso = expiration_time_utc.isoformat()
        comment = (
            f"Discord VIP for {interaction.user.display_name} until {expiration_time_utc:%Y-%m-%d %H:%M:%S} UTC"
        )

        try:
            result = await asyncio.to_thread(
                self.vip_service.grant_vip,
                steam_id,
                comment,
                expiration_time_iso,
            )
        except VipHTTPError as exc:
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
            expiration_time_utc.strftime("%Y-%m-%d %H:%M:%S"),
            "; ".join(result.status_lines),
        )
        self.bot.record_vip_grant(datetime.now(timezone.utc))
        await self.bot.refresh_announcement_message()

        header_lines = [
            f"You now have VIP for {self.config.vip_duration_label} hours!",
            f"Linked ID: {steam_id}",
            f"Expiration: {readable_expiration}",
        ]
        status_summary = "\n".join(f"- {line}" for line in result.status_lines)
        message_body = "\n".join(header_lines) + "\n\n**Status**:\n" + status_summary
        followup_message = await interaction.followup.send(
            message_body,
            ephemeral=True,
            wait=True,
        )
        schedule_ephemeral_cleanup(interaction, message=followup_message)
        await self._maybe_remove_temp_vip_role(interaction)

    async def _maybe_remove_temp_vip_role(self, interaction: discord.Interaction) -> None:
        role_id = getattr(self.config, "vip_temp_role_id", None)
        if not role_id:
            return
        guild = interaction.guild
        if guild is None:
            return
        user = interaction.user
        if not isinstance(user, discord.Member):
            try:
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


class FrontlinePassBot(commands.Bot):
    def __init__(self, config: AppConfig, vip_service: VipService) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.vip_service = vip_service
        self.announcement_manager = AnnouncementManager(config)
        self.persistent_view: Optional[CombinedView] = None
        self._vip_duration_hours = config.vip_duration_hours
        self._last_grant_utc: Optional[datetime] = None

    @property
    def vip_duration_hours(self) -> float:
        return self._vip_duration_hours

    @property
    def last_grant_time(self) -> Optional[datetime]:
        return self._last_grant_utc

    def record_vip_grant(self, when: datetime) -> None:
        self._last_grant_utc = when

    async def setup_hook(self) -> None:
        self.persistent_view = CombinedView(self, self.config, self.vip_service)
        self.add_view(self.persistent_view)
        await self._register_commands()
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
        try:
            cmd_names = ", ".join(sorted(cmd.name for cmd in self.tree.get_commands()))
            logging.info("Registered slash commands: %s", cmd_names)
        except Exception:
            logging.exception("Unable to list registered slash commands")

    async def on_ready(self) -> None:
        logging.info("Bot is ready: %s", self.user)
        http_base = self.config.http_credentials.base_url if self.config.http_credentials else "unset"
        logging.info("HTTP API base=%s; current VIP duration=%.2f hours", http_base, self.vip_duration_hours)
        await self.refresh_announcement_message()

    async def refresh_announcement_message(self) -> None:
        if not self.persistent_view:
            logging.error("Persistent view not initialised; cannot refresh announcement message.")
            return
        await self.announcement_manager.ensure(
            self,
            self.persistent_view,
            self.vip_duration_hours,
            self.last_grant_time,
        )

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

    async def set_vip_duration_hours(self, hours: float) -> None:
        self._vip_duration_hours = hours
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
                self.last_grant_time,
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
            description="Show bot health information.",
        )
        async def health(interaction: discord.Interaction) -> None:
            last_grant = self.last_grant_time
            if last_grant:
                local_dt = last_grant.astimezone(self.config.timezone)
                last_grant_text = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
            else:
                last_grant_text = "None yet"
            http_base = self.config.http_credentials.base_url if self.config.http_credentials else "unset"
            msg = (
                f"VIP duration: {self.vip_duration_hours:g} hours\n"
                f"Last VIP grant: {last_grant_text}\n"
                f"HTTP API base: {http_base}"
            )
            await interaction.response.send_message(msg, ephemeral=True)
            schedule_ephemeral_cleanup(interaction)

        @self.tree.command(
            name="assignvip",
            description="Assign a temporary VIP Discord role to a member so they can claim VIP.",
        )
        @app_commands.describe(member="Select the server member to grant temporary VIP role to")
        async def assignvip(interaction: discord.Interaction, member: discord.Member) -> None:
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
                await member.add_roles(role, reason="Frontline Pass: temporary VIP role for claim")
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

            claim_channel_id = self.config.vip_claim_channel_id or self.config.channel_id
            channel_mention = f"<#{claim_channel_id}>" if claim_channel_id else "the VIP channel"
            try:
                await interaction.response.send_message(
                    (
                        f"Assigned {role.mention} to {member.mention}.\n"
                        f"Please go to {channel_mention} and press Get VIP, then enter the Player-ID when prompted.\n"
                        "After you claim VIP, your temporary Discord role will be removed automatically."
                    ),
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
            except discord.InteractionResponded:
                with contextlib.suppress(discord.DiscordException):
                    await interaction.followup.send(
                        (
                            f"Assigned {role.mention} to {member.mention}. Go to {channel_mention}, press Get VIP, and enter the Player-ID to claim VIP."
                        ),
                        ephemeral=True,
                        wait=False,
                    )


def create_bot(config: AppConfig, vip_service: VipService) -> commands.Bot:
    return FrontlinePassBot(config, vip_service)


def main() -> None:
    config = load_config()
    vip_service = VipService(config)
    bot = create_bot(config, vip_service)
    bot.run(config.discord_token)


if __name__ == "__main__":
    main()
