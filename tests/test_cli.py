import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_trade.cli import build_parser, main
from ai_trade.config import _validate_auth, load_config
from ai_trade.web.auth import UserStore


class CliTests(unittest.TestCase):
    def test_serve_parser_defaults_to_loopback(self):
        args = build_parser().parse_args(["serve"])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8765)
        self.assertFalse(args.owner_local)
        owner = build_parser().parse_args(["serve", "--owner-local"])
        self.assertTrue(owner.owner_local)

    def test_packaged_default_matches_repository_config(self):
        root = Path(__file__).resolve().parents[1]
        repository = json.loads((root / "config/default.json").read_text(encoding="utf-8"))
        packaged = json.loads(
            (root / "src/ai_trade/default_config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(packaged, repository)
        repository_master = json.loads(
            (root / "config/security_master.json").read_text(encoding="utf-8")
        )
        packaged_master = json.loads(
            (root / "src/ai_trade/default_security_master.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(packaged_master, repository_master)

    def test_auth_configuration_validation(self):
        _validate_auth({})
        _validate_auth({"enabled": True, "session_hours": 8})
        for value, message in (
            ({"enabled": "yes"}, "enabled"),
            ({"users_file": ""}, "users_file"),
            ({"session_hours": 0}, "session_hours"),
            ({"max_failed_attempts": True}, "max_failed_attempts"),
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, message):
                _validate_auth(value)

    def test_init_creates_standalone_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "workspace"
            self.assertEqual(main(["init", "--directory", str(target)]), 0)
            self.assertTrue((target / "config/default.json").exists())
            self.assertTrue((target / "config/security_master.json").exists())
            self.assertTrue((target / "data/cache/.gitkeep").exists())
            self.assertTrue((target / "state/.gitkeep").exists())

    def test_beta_user_cli_and_portable_whitelist(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "ai_trade.cli._configure_logging"
        ):
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            self.assertEqual(main(["init", "--directory", str(first)]), 0)
            self.assertEqual(main(["init", "--directory", str(second)]), 0)
            first_config = first / "config/default.json"
            second_config = second / "config/default.json"
            password = "local-test-password"
            with patch("ai_trade.cli.getpass", side_effect=[password, password]):
                self.assertEqual(
                    main(
                        [
                            "--config",
                            str(first_config),
                            "beta-user-add",
                            "tester",
                        ]
                    ),
                    0,
                )
            first_store = UserStore(load_config(first_config).auth_users_file)
            self.assertTrue(first_store.verify("tester", password))
            self.assertNotIn(
                password,
                first_store.path.read_text(encoding="utf-8"),
            )

            bundle = root / "beta-users.json"
            self.assertEqual(
                main(
                    [
                        "--config",
                        str(first_config),
                        "beta-users-export",
                        str(bundle),
                    ]
                ),
                0,
            )
            self.assertNotIn(password, bundle.read_text(encoding="utf-8"))
            self.assertEqual(
                main(
                    [
                        "--config",
                        str(second_config),
                        "beta-users-import",
                        str(bundle),
                    ]
                ),
                0,
            )
            second_store = UserStore(load_config(second_config).auth_users_file)
            self.assertTrue(second_store.verify("tester", password))


if __name__ == "__main__":
    unittest.main()
