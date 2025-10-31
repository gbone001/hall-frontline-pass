import gc
import importlib.util
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

import pytz

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
Database = frontline_pass.Database


class DummyResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = "response-text"

    def json(self) -> dict:
        return self._payload


class DummySession:
    def __init__(self, response: DummyResponse) -> None:
        self.response = response
        self.calls = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "json": json,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return self.response

    def post(self, url, json=None, headers=None, timeout=None):
        return self.request("POST", url, json=json, headers=headers, timeout=timeout)


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


class VipServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig(
            discord_token="token",
            vip_duration_hours=4,
            channel_id=1,
            timezone=pytz.UTC,
            timezone_name="UTC",
            rcon_host="localhost",
            rcon_port=1234,
            rcon_password="pwd",
            rcon_version=2,
            database_path=":memory:",
            database_table="vip_players",
            moderation_channel_id=None,
            moderator_role_id=None,
            announcement_message_id=None,
            http_credentials=HttpCredentials(
                base_url="https://example/api",
                bearer_token="abc123",
            ),
        )

    def test_prefers_http_before_rcon(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.add_vip.return_value = {"result": "ok"}
        service._http_client = fake_http_client  # type: ignore[attr-defined]
        service._grant_vip_via_rcon = mock.Mock()  # type: ignore[attr-defined]

        result = service.grant_vip("steam123", "comment", None)

        fake_http_client.add_vip.assert_called_once_with("steam123", "comment", None)
        service._grant_vip_via_rcon.assert_not_called()  # type: ignore[attr-defined]
        self.assertIn("HTTP API", result.status_lines[0])


class DatabaseTests(unittest.TestCase):
    def test_uses_legacy_sqlite_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = pathlib.Path(tmpdir) / "legacy.db"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE vip_players (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        discord_id TEXT UNIQUE NOT NULL,
                        steam_id TEXT UNIQUE NOT NULL
                    )
                    """
                )
                connection.execute(
                    'INSERT INTO vip_players (discord_id, steam_id) VALUES (?, ?)',
                    ("123456", "7654321"),
                )
                connection.execute(
                    """
                    CREATE TABLE metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                    """
                )
                connection.execute(
                    'INSERT INTO metadata (key, value) VALUES (?, ?)',
                    ("vip_duration_hours", "4"),
                )
                connection.commit()
            finally:
                connection.close()

            database = Database(str(db_path), "vip_players")

            self.assertEqual(database.fetch_player("123456"), "7654321")
            self.assertEqual(database.get_metadata("vip_duration_hours"), "4")
            self.assertEqual(database.count_players(), 1)

            database.upsert_player("654321", "999888777")
            database.set_metadata("announcement_message_id", "42")
            self.assertEqual(database.fetch_player("654321"), "999888777")
            self.assertEqual(database.get_metadata("announcement_message_id"), "42")
            self.assertEqual(database.count_players(), 2)

            # Re-open to ensure persistence
            database = None
            reopened = Database(str(db_path), "vip_players")
            self.assertEqual(reopened.fetch_player("654321"), "999888777")
            self.assertEqual(reopened.get_metadata("announcement_message_id"), "42")
            self.assertEqual(reopened.count_players(), 2)
            reopened = None
            gc.collect()

            with open(db_path, "rb") as handle:
                header = handle.read(16)
            self.assertTrue(header.startswith(b"SQLite format 3"))


if __name__ == "__main__":
    unittest.main()
