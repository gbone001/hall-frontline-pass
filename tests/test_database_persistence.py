import importlib.util
import os
import pathlib
import tempfile
import unittest


MODULE_PATH = pathlib.Path(__file__).resolve().parent.parent / "frontline-pass.py"
SPEC = importlib.util.spec_from_file_location("frontline_pass_module", MODULE_PATH)
frontline_pass = importlib.util.module_from_spec(SPEC)
import sys

sys.modules[SPEC.name] = frontline_pass
SPEC.loader.exec_module(frontline_pass)  # type: ignore[union-attr]

Database = frontline_pass.Database


class DatabasePersistenceTests(unittest.TestCase):
    def test_registration_persists_across_reinstantiation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "vip-data.json")

            # First instance writes a registration
            db1 = Database(db_path, "vip_players")
            db1.upsert_player("discord_user_123", "steam_7656119")

            # Second instance loads existing data
            db2 = Database(db_path, "vip_players")
            self.assertEqual(db2.fetch_player("discord_user_123"), "steam_7656119")


if __name__ == "__main__":
    unittest.main()
