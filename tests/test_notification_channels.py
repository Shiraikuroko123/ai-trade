from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
import json

from ai_trade.monitoring import _fingerprint
from ai_trade.notification_channels import (
    _send_email,
    channel_delivery_status,
    deliver_channel_notifications,
    load_desktop_settings,
    load_email_settings,
    verify_channel_records,
)


def _notification() -> dict:
    value = {
        "schema_version": 1,
        "notification_id": "notification_" + "a" * 32,
        "created_at": "2026-07-24T08:00:00Z",
        "source_type": "alert",
        "source_id": "alert_" + "b" * 32,
        "source_fingerprint": "c" * 64,
        "evidence_fingerprint": "d" * 64,
        "severity": "warning",
        "title": "600000 close above",
        "message": "The completed close triggered the research rule.",
        "symbol": "600000",
        "data_date": "2026-07-24",
        "status": "unread",
    }
    value["fingerprint"] = _fingerprint(value)
    value["state_fingerprint"] = "e" * 64
    return value


class NotificationChannelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.profile = Path(self.temporary.name) / "p"
        self.profile_id = sha256(b"alice").hexdigest()
        self.notification = _notification()
        self.email = load_email_settings(
            {
                "AI_TRADE_EMAIL_SMTP_HOST": "smtp.example.com",
                "AI_TRADE_EMAIL_SMTP_PORT": "587",
                "AI_TRADE_EMAIL_SECURITY": "starttls",
                "AI_TRADE_EMAIL_USERNAME": "alice@example.com",
                "AI_TRADE_EMAIL_PASSWORD": "secret",
                "AI_TRADE_EMAIL_FROM": "alice@example.com",
                "AI_TRADE_EMAIL_TO": "owner@example.com",
                "AI_TRADE_EMAIL_MAX_ATTEMPTS": "3",
            }
        )
        self.desktop_off = load_desktop_settings({})

    def tearDown(self):
        self.temporary.cleanup()

    def test_email_delivery_is_idempotent_and_audited_without_secrets(self):
        with patch("ai_trade.notification_channels._send_email") as sender:
            first = deliver_channel_notifications(
                self.profile,
                self.profile_id,
                [self.notification],
                email=self.email,
                desktop=self.desktop_off,
            )
            second = deliver_channel_notifications(
                self.profile,
                self.profile_id,
                [self.notification],
                email=self.email,
                desktop=self.desktop_off,
            )

        self.assertEqual(first["email"]["status"], "succeeded")
        self.assertEqual(second["email"]["attempt_count"], 1)
        sender.assert_called_once()
        records = verify_channel_records(
            self.profile,
            self.profile_id,
            {self.notification["notification_id"]: self.notification},
        )
        self.assertEqual(len(records), 1)
        serialized = str(records)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("owner@example.com", serialized)

    def test_failed_email_can_retry_without_changing_notification_state(self):
        with patch(
            "ai_trade.notification_channels._send_email",
            side_effect=[OSError("SMTP unavailable"), None],
        ):
            failed = deliver_channel_notifications(
                self.profile,
                self.profile_id,
                [self.notification],
                email=self.email,
                desktop=self.desktop_off,
            )
            succeeded = deliver_channel_notifications(
                self.profile,
                self.profile_id,
                [self.notification],
                email=self.email,
                desktop=self.desktop_off,
            )

        self.assertEqual(failed["email"]["status"], "failed")
        self.assertEqual(succeeded["email"]["status"], "succeeded")
        self.assertEqual(succeeded["email"]["attempt_count"], 2)
        self.assertEqual(self.notification["status"], "unread")

    def test_desktop_settings_fail_closed_off_windows_and_command_is_encoded(self):
        unsupported = load_desktop_settings(
            {"AI_TRADE_DESKTOP_NOTIFICATIONS": "1"}, platform="linux"
        )
        self.assertFalse(unsupported.enabled)
        self.assertIn("Windows", unsupported.configuration_error)

        enabled = load_desktop_settings(
            {"AI_TRADE_DESKTOP_NOTIFICATIONS": "1"}, platform="win32"
        )
        completed = Mock(returncode=0)
        with patch("ai_trade.notification_channels.sys.platform", "win32"), patch(
            "ai_trade.notification_channels.subprocess.run", return_value=completed
        ) as run:
            result = deliver_channel_notifications(
                self.profile,
                self.profile_id,
                [self.notification],
                email=load_email_settings({}),
                desktop=enabled,
            )

        self.assertEqual(result["desktop"]["status"], "succeeded")
        command = run.call_args.args[0]
        self.assertIn("-EncodedCommand", command)
        self.assertNotIn(self.notification["message"], command)

    def test_invalid_partial_email_configuration_is_visible(self):
        invalid = load_email_settings({"AI_TRADE_EMAIL_SMTP_HOST": "smtp.example.com"})
        status = channel_delivery_status(
            self.profile,
            self.profile_id,
            [self.notification],
            email=invalid,
            desktop=self.desktop_off,
        )
        self.assertEqual(status["status"], "configuration_error")
        self.assertEqual(status["email"]["configuration_status"], "invalid")

    def test_attempt_record_tampering_is_rejected(self):
        with patch("ai_trade.notification_channels._send_email"):
            deliver_channel_notifications(
                self.profile,
                self.profile_id,
                [self.notification],
                email=self.email,
                desktop=self.desktop_off,
            )
        path = next((self.profile / "delivery_attempts").glob("*.json"))
        record = json.loads(path.read_text(encoding="utf-8"))
        record["status"] = "failed"
        path.write_text(json.dumps(record), encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "fingerprint"):
            verify_channel_records(
                self.profile,
                self.profile_id,
                {self.notification["notification_id"]: self.notification},
            )

    def test_starttls_email_uses_authenticated_tls_connection(self):
        smtp = MagicMock()
        client = smtp.return_value.__enter__.return_value
        with patch("ai_trade.notification_channels.smtplib.SMTP", smtp):
            _send_email(self.email, self.notification)

        smtp.assert_called_once_with(
            "smtp.example.com", 587, timeout=self.email.timeout_seconds
        )
        self.assertEqual(client.ehlo.call_count, 2)
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("alice@example.com", "secret")
        client.send_message.assert_called_once()

    def test_ssl_email_uses_smtp_ssl_without_starttls(self):
        email = load_email_settings(
            {
                "AI_TRADE_EMAIL_SMTP_HOST": "smtp.example.com",
                "AI_TRADE_EMAIL_SMTP_PORT": "465",
                "AI_TRADE_EMAIL_SECURITY": "ssl",
                "AI_TRADE_EMAIL_FROM": "alice@example.com",
                "AI_TRADE_EMAIL_TO": "owner@example.com",
            }
        )
        smtp = MagicMock()
        client = smtp.return_value.__enter__.return_value
        with patch("ai_trade.notification_channels.smtplib.SMTP_SSL", smtp):
            _send_email(email, self.notification)

        smtp.assert_called_once()
        client.starttls.assert_not_called()
        client.login.assert_not_called()
        client.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
