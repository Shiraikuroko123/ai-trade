from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ai_trade.data.jqdata import (
    PASSWORD_ENV,
    USERNAME_ENV,
    JQDataCredentialError,
    JQDataCredentials,
    JQDataError,
    JQDataProbeStore,
    credentials_from_environment,
    probe_account,
    prompt_credentials,
)


USERNAME = "13812345678"
PASSWORD = "private-password"
CREATED_AT = datetime(2026, 7, 27, 9, 30, tzinfo=timezone.utc)


class _FakeSDK:
    __version__ = "1.9.7"

    def __init__(self) -> None:
        self.calls: list[object] = []
        self.auth_result: object = (True, "auth success")
        self.account_error: Exception | None = None

    def auth(self, username: str, password: str) -> object:
        self.calls.append(("auth", username, password))
        return self.auth_result

    def logout(self) -> None:
        self.calls.append("logout")

    def get_account_info(self) -> object:
        self.calls.append("get_account_info")
        if self.account_error is not None:
            raise self.account_error
        return {
            "license": 1,
            "date_range_start": "2015-01-01",
            "date_range_end": "2026-07-27",
            "query_count_limit": 500000,
            "expire_time": "2026-10-27 00:00:00",
            "mob": USERNAME,
            "ignored_private_field": "not returned",
        }

    def get_query_count(self) -> object:
        self.calls.append("get_query_count")
        return {"spare": 499999, "total": 500000}


class _FailingAuthSDK(_FakeSDK):
    def auth(self, username: str, password: str) -> object:
        raise RuntimeError(f"bad credentials {username} {password}")


class JQDataProbeTests(unittest.TestCase):
    def test_probe_is_read_only_masked_and_always_logs_out(self):
        sdk = _FakeSDK()
        credentials = JQDataCredentials(USERNAME, PASSWORD)

        result = probe_account(
            credentials,
            sdk=sdk,
            created_at=CREATED_AT,
        )

        serialized = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["account"]["mob"], "138******78")
        self.assertEqual(result["account"]["query_count_limit"], 500000)
        self.assertNotIn("ignored_private_field", result["account"])
        self.assertNotIn(USERNAME, serialized)
        self.assertNotIn(PASSWORD, serialized)
        self.assertFalse(result["data_requested"])
        self.assertFalse(result["credentials_persisted"])
        self.assertFalse(result["export_allowed"])
        self.assertEqual(
            sdk.calls,
            [
                ("auth", USERNAME, PASSWORD),
                "get_account_info",
                "get_query_count",
                "logout",
            ],
        )

    def test_authentication_and_query_errors_redact_credentials(self):
        credentials = JQDataCredentials(USERNAME, PASSWORD)
        with self.assertRaises(JQDataCredentialError) as captured:
            probe_account(
                credentials,
                sdk=_FailingAuthSDK(),
                created_at=CREATED_AT,
            )
        message = str(captured.exception)
        self.assertNotIn(USERNAME, message)
        self.assertNotIn(PASSWORD, message)
        self.assertIn("<redacted>", message)

        sdk = _FakeSDK()
        sdk.account_error = RuntimeError(
            f"account lookup failed for {USERNAME} using {PASSWORD}"
        )
        with self.assertRaises(JQDataError) as captured:
            probe_account(credentials, sdk=sdk, created_at=CREATED_AT)
        self.assertNotIn(USERNAME, str(captured.exception))
        self.assertNotIn(PASSWORD, str(captured.exception))
        self.assertEqual(sdk.calls[-1], "logout")

    def test_rejected_authentication_does_not_query_entitlement(self):
        sdk = _FakeSDK()
        sdk.auth_result = (False, "auth failed")

        with self.assertRaisesRegex(
            JQDataCredentialError, "authentication was rejected"
        ):
            probe_account(
                JQDataCredentials(USERNAME, PASSWORD),
                sdk=sdk,
                created_at=CREATED_AT,
            )

        self.assertEqual(sdk.calls, [("auth", USERNAME, PASSWORD)])

    def test_credentials_are_complete_and_repr_is_redacted(self):
        self.assertIsNone(credentials_from_environment({}))
        credentials = credentials_from_environment(
            {
                USERNAME_ENV: USERNAME,
                PASSWORD_ENV: PASSWORD,
            }
        )
        self.assertIsNotNone(credentials)
        self.assertNotIn(USERNAME, repr(credentials))
        self.assertNotIn(PASSWORD, repr(credentials))
        with self.assertRaisesRegex(JQDataCredentialError, "must be supplied together"):
            credentials_from_environment({USERNAME_ENV: USERNAME})
        prompted = prompt_credentials(
            account_reader=lambda _: USERNAME,
            password_reader=lambda _: PASSWORD,
        )
        self.assertEqual(prompted.username, USERNAME)

    def test_probe_store_is_immutable_and_detects_tampering(self):
        sdk = _FakeSDK()
        record = probe_account(
            JQDataCredentials(USERNAME, PASSWORD),
            sdk=sdk,
            created_at=CREATED_AT,
        )
        with TemporaryDirectory() as temporary:
            store = JQDataProbeStore(Path(temporary) / "jqdata")
            first = store.publish(record)
            second = store.publish(record)
            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])

            path = (
                store.probes_root
                / CREATED_AT.date().isoformat()
                / f"{record['probe_id']}.json"
            )
            stored_text = path.read_text(encoding="utf-8")
            self.assertNotIn(USERNAME, stored_text)
            self.assertNotIn(PASSWORD, stored_text)
            value = json.loads(stored_text)
            value["query_count"]["spare"] = 0
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "match content|fingerprint"):
                store.get(CREATED_AT, str(record["probe_id"]))

    def test_sdk_import_failure_has_install_instruction_without_secrets(self):
        from ai_trade.data.jqdata import load_jqdata_sdk

        with patch(
            "ai_trade.data.jqdata.importlib.import_module",
            side_effect=ModuleNotFoundError("missing"),
        ):
            with self.assertRaisesRegex(
                JQDataError, "install AI Trade with the jqdata extra"
            ):
                load_jqdata_sdk()


if __name__ == "__main__":
    unittest.main()
