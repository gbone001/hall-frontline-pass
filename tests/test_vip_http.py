import importlib.util
import pathlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytz
from requests.cookies import RequestsCookieJar

MODULE_PATH = pathlib.Path(__file__).resolve().parent.parent / "frontline-pass.py"
SPEC = importlib.util.spec_from_file_location("frontline_pass_module", MODULE_PATH)
frontline_pass = importlib.util.module_from_spec(SPEC)
import sys

sys.modules[SPEC.name] = frontline_pass
SPEC.loader.exec_module(frontline_pass)  # type: ignore[union-attr]

AppConfig = frontline_pass.AppConfig
HttpCredentials = frontline_pass.HttpCredentials
VipHttpClient = frontline_pass.VipHttpClient
VipHTTPError = frontline_pass.VipHTTPError
VipService = frontline_pass.VipService


class DummyResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict,
        *,
        text: str | None = None,
        cookies: RequestsCookieJar | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else "response-text"
        self.cookies = cookies or RequestsCookieJar()

    def json(self) -> dict:
        return self._payload


class DummySession:
    def __init__(self, responses) -> None:
        if isinstance(responses, DummyResponse):
            responses = [responses]
        self.responses = list(responses)
        self.calls = []
        self.verify = True
        self.cookies = RequestsCookieJar()

    def _next_response(self) -> DummyResponse:
        if self.responses:
            response = self.responses.pop(0)
        else:
            raise AssertionError("Unexpected HTTP request in DummySession")
        if response.cookies:
            for key, value in response.cookies.items():
                self.cookies.set(key, value)
        return response

    def request(self, method, url, json=None, headers=None, timeout=None, params=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "json": json,
                "headers": headers,
                "timeout": timeout,
                "params": params,
            }
        )
        return self._next_response()

    def post(self, url, json=None, headers=None, timeout=None, params=None):
        return self.request("POST", url, json=json, headers=headers, timeout=timeout, params=params)


class VipHttpClientTests(unittest.TestCase):
    def test_add_vip_uses_bearer_token(self) -> None:
        session = DummySession(DummyResponse(200, {"result": "ok"}))
        client = VipHttpClient(
            HttpCredentials(base_url="https://example/api", bearer_token="abc123"),
            timeout=5.0,
            session=session,
        )

        result = client.add_vip("player-id", "desc", "2025-10-31T12:00:00Z")

        self.assertEqual(result["result"], "ok")
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["url"], "https://example/api/add_vip")
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["timeout"], 5.0)
        self.assertEqual(
            call["json"],
            {
                "player_id": "player-id",
                "description": "desc",
                "expiration": "2025-10-31T12:00:00Z",
            },
        )
        self.assertIn("Authorization", call["headers"])
        self.assertEqual(call["headers"]["Authorization"], "Bearer abc123")

    def test_add_vip_includes_player_name(self) -> None:
        session = DummySession(DummyResponse(200, {"result": "ok"}))
        client = VipHttpClient(
            HttpCredentials(base_url="https://example/api", bearer_token="abc123"),
            session=session,
        )

        client.add_vip("player-id", "desc", None, player_name="GBONE001")

        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(
            call["json"],
            {
                "player_id": "player-id",
                "description": "desc",
                "player_name": "GBONE001",
            },
        )

    def test_add_vip_rejects_non_200(self) -> None:
        session = DummySession(DummyResponse(401, {"error": "unauthorized"}))
        client = VipHttpClient(
            HttpCredentials(base_url="https://example/api", bearer_token="abc123"),
            session=session,
        )

        with self.assertRaises(VipHTTPError):
            client.add_vip("player-id", "desc", None)

    def test_bearer_token_preferred_over_login(self) -> None:
        session = DummySession(DummyResponse(200, {"result": "ok"}))
        credentials = HttpCredentials(
            base_url="https://example/api",
            bearer_token="abc123",
            username="user",
            password="pass",
        )
        client = VipHttpClient(credentials, session=session)

        client.add_vip("player-id", "desc", None)

        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("login", session.calls[0]["url"])

    def test_logs_in_and_uses_session_cookie(self) -> None:
        cookie_jar = RequestsCookieJar()
        cookie_jar.set("sessionid", "session-cookie")
        cookie_jar.set("csrftoken", "csrf-token")
        login_response = DummyResponse(
            200,
            {"result": True, "failed": False},
            text="login-success",
            cookies=cookie_jar,
        )
        api_response = DummyResponse(200, {"result": "ok"})
        session = DummySession([login_response, api_response])
        credentials = HttpCredentials(
            base_url="https://example",
            username="user",
            password="pass",
        )
        client = VipHttpClient(credentials, session=session)

        result = client.add_vip("player-id", "desc", None)

        self.assertEqual(result["result"], "ok")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0]["url"], "https://example/api/login")
        self.assertEqual(session.calls[1]["url"], "https://example/api/add_vip")
        self.assertEqual(session.calls[1]["headers"]["Referer"], "https://example")

    def test_get_player_profile_fetches_profile(self) -> None:
        session = DummySession(DummyResponse(200, {"result": {"player_id": "player-id"}}))
        client = VipHttpClient(
            HttpCredentials(base_url="https://example/api", bearer_token="abc123"),
            session=session,
        )

        profile = client.get_player_profile("player-id")

        self.assertEqual(profile["player_id"], "player-id")
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "https://example/api/get_player_profile")
        self.assertEqual(
            call["params"],
            {
                "player_id": "player-id",
                "num_sessions": 10,
            },
        )

    def test_get_player_profile_raises_on_failure(self) -> None:
        session = DummySession(DummyResponse(403, {"result": None, "error": "nope"}))
        client = VipHttpClient(
            HttpCredentials(base_url="https://example/api", bearer_token="abc123"),
            session=session,
        )

        with self.assertRaises(VipHTTPError):
            client.get_player_profile("player-id")


class VipServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig(
            discord_token="token",
            vip_duration_hours=4,
            channel_id=1,
            timezone=pytz.UTC,
            timezone_name="UTC",
            http_credentials=HttpCredentials(
                base_url="https://example",
                bearer_token="abc123",
            ),
        )

    def test_grant_vip_uses_http_client(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.get_player_profile.return_value = {}
        fake_http_client.add_vip.return_value = {"result": "ok"}
        service._http_client = fake_http_client  # type: ignore[attr-defined]
        fixed_now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        service._now_utc = mock.Mock(return_value=fixed_now)  # type: ignore[attr-defined]

        result = service.grant_vip(
            "steam123",
            duration_hours=4,
            local_timezone=pytz.UTC,
            requester_display_name="GBONE",
        )

        fake_http_client.add_vip.assert_called_once()
        args, kwargs = fake_http_client.add_vip.call_args
        fake_http_client.get_player_profile.assert_called_once_with("steam123", num_sessions=10)
        expected_expiration = fixed_now + timedelta(hours=4)
        self.assertEqual(args[0], "steam123")
        self.assertIn("Discord VIP for GBONE", args[1])
        self.assertEqual(args[2], expected_expiration.isoformat())
        self.assertIsNone(kwargs.get("player_name"))
        self.assertIn("HTTP API", result.status_lines[0])
        self.assertEqual(result.expiration_utc, expected_expiration)
        self.assertEqual(result.expiration_local, expected_expiration)

    def test_grant_vip_forwards_player_name(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.get_player_profile.return_value = {}
        fake_http_client.add_vip.return_value = {"result": "ok"}
        service._http_client = fake_http_client  # type: ignore[attr-defined]
        service._now_utc = mock.Mock(return_value=datetime(2030, 1, 1, tzinfo=timezone.utc))  # type: ignore[attr-defined]

        service.grant_vip(
            "steam123",
            duration_hours=1,
            local_timezone=pytz.UTC,
            requester_display_name="GBONE",
            player_name="GBONE001",
        )

        fake_http_client.add_vip.assert_called_once()
        _, kwargs = fake_http_client.add_vip.call_args
        self.assertEqual(kwargs.get("player_name"), "GBONE001")

    def test_grant_vip_extends_existing_expiration(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.get_player_profile.return_value = {
            "vips": [
                {"expiration": "2031-01-01T00:00:00+00:00"},
                {"expiration": "2030-12-01T00:00:00+00:00"},
            ]
        }
        fake_http_client.add_vip.return_value = {"result": "ok"}
        service._http_client = fake_http_client  # type: ignore[attr-defined]
        service._now_utc = mock.Mock(return_value=datetime(2030, 6, 1, tzinfo=timezone.utc))  # type: ignore[attr-defined]
        local_tz = pytz.timezone("Australia/Sydney")

        result = service.grant_vip(
            "steam123",
            duration_hours=2,
            local_timezone=local_tz,
            requester_display_name="GBONE",
        )

        base = datetime.fromisoformat("2031-01-01T00:00:00+00:00")
        expected_expiration = base + timedelta(hours=2)
        self.assertEqual(result.expiration_utc, expected_expiration)
        self.assertEqual(result.expiration_local, expected_expiration.astimezone(local_tz))
        self.assertEqual(
            fake_http_client.add_vip.call_args[0][2],
            expected_expiration.isoformat(),
        )

    def test_get_player_vip_status_returns_expiration(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.get_player_profile.return_value = {
            "vips": [
                {"expiration": "2032-05-01T10:00:00+00:00"},
            ]
        }
        service._http_client = fake_http_client  # type: ignore[attr-defined]

        status = service.get_player_vip_status("steam123")

        self.assertEqual(status.player_id, "steam123")
        self.assertEqual(status.expiration_utc, datetime.fromisoformat("2032-05-01T10:00:00+00:00"))
        fake_http_client.get_player_profile.assert_called_once_with("steam123", num_sessions=10)

    def test_get_player_vip_status_handles_missing_entries(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.get_player_profile.return_value = {}
        service._http_client = fake_http_client  # type: ignore[attr-defined]

        status = service.get_player_vip_status("steam123")

        self.assertEqual(status.player_id, "steam123")
        self.assertIsNone(status.expiration_utc)


if __name__ == "__main__":
    unittest.main()
