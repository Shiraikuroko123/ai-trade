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
    load_dingtalk_settings,
    load_email_settings,
    load_pushplus_settings,
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


class MobilePushChannelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.profile = Path(self.temporary.name) / "p"
        self.profile_id = sha256(b"alice").hexdigest()
        self.notification = _notification()
        self.email_off = load_email_settings({})
        self.desktop_off = load_desktop_settings({})
        self.pushplus = load_pushplus_settings(
            {"AI_TRADE_PUSHPLUS_TOKEN": "abc12345TOKEN"}
        )
        self.dingtalk = load_dingtalk_settings(
            {
                "AI_TRADE_DINGTALK_WEBHOOK": (
                    "https://oapi.dingtalk.com/robot/send?access_token=" + "f" * 32
                ),
                "AI_TRADE_DINGTALK_SECRET": "SECtestsecret",
            }
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_unset_environments_stay_disabled_without_error(self):
        pushplus = load_pushplus_settings({})
        dingtalk = load_dingtalk_settings({})
        self.assertFalse(pushplus.enabled)
        self.assertFalse(dingtalk.enabled)
        self.assertIsNone(pushplus.configuration_error)
        self.assertIsNone(dingtalk.configuration_error)

    def test_invalid_configuration_fails_closed_with_message(self):
        bad_token = load_pushplus_settings({"AI_TRADE_PUSHPLUS_TOKEN": "no spaces!"})
        self.assertFalse(bad_token.enabled)
        self.assertIn("AI_TRADE_PUSHPLUS_TOKEN", bad_token.configuration_error)
        http_url = load_dingtalk_settings(
            {"AI_TRADE_DINGTALK_WEBHOOK": "http://oapi.dingtalk.com/robot/send?access_token=x"}
        )
        self.assertFalse(http_url.enabled)
        self.assertIn("HTTPS", http_url.configuration_error)
        wrong_host = load_dingtalk_settings(
            {"AI_TRADE_DINGTALK_WEBHOOK": "https://evil.example.com/robot/send?access_token=x"}
        )
        self.assertFalse(wrong_host.enabled)

    def test_pushplus_delivery_is_idempotent_and_audits_without_token(self):
        with patch(
            "ai_trade.notification_channels._post_json",
            return_value={"code": 200},
        ) as poster:
            first = deliver_channel_notifications(
                self.profile,
                self.profile_id,
                [self.notification],
                email=self.email_off,
                desktop=self.desktop_off,
                pushplus=self.pushplus,
                dingtalk=load_dingtalk_settings({}),
            )
            second = deliver_channel_notifications(
                self.profile,
                self.profile_id,
                [self.notification],
                email=self.email_off,
                desktop=self.desktop_off,
                pushplus=self.pushplus,
                dingtalk=load_dingtalk_settings({}),
            )
        self.assertEqual(first["pushplus"]["status"], "succeeded")
        self.assertEqual(second["pushplus"]["attempt_count"], 1)
        self.assertEqual(poster.call_count, 1)
        url, payload, _timeout = poster.call_args[0]
        self.assertEqual(url, "https://www.pushplus.plus/send")
        self.assertEqual(payload["token"], "abc12345TOKEN")
        attempts = list((self.profile / "delivery_attempts").iterdir())
        self.assertEqual(len(attempts), 1)
        raw = attempts[0].read_text(encoding="utf-8")
        self.assertNotIn("abc12345TOKEN", raw)
        self.assertIn("pushplus", raw)

    def test_pushplus_provider_error_is_recorded_and_retryable(self):
        with patch(
            "ai_trade.notification_channels._post_json",
            side_effect=[{"code": 500, "msg": "limit"}, {"code": 200}],
        ):
            failed = deliver_channel_notifications(
                self.profile,
                self.profile_id,
                [self.notification],
                email=self.email_off,
                desktop=self.desktop_off,
                pushplus=self.pushplus,
                dingtalk=load_dingtalk_settings({}),
            )
            succeeded = deliver_channel_notifications(
                self.profile,
                self.profile_id,
                [self.notification],
                email=self.email_off,
                desktop=self.desktop_off,
                pushplus=self.pushplus,
                dingtalk=load_dingtalk_settings({}),
            )
        self.assertEqual(failed["pushplus"]["status"], "failed")
        self.assertIn("code=500", failed["pushplus"]["last_error"])
        self.assertEqual(succeeded["pushplus"]["status"], "succeeded")
        self.assertEqual(succeeded["pushplus"]["attempt_count"], 2)

    def test_dingtalk_signs_the_webhook_and_checks_errcode(self):
        captured = {}

        def poster(url, payload, timeout):
            captured["url"] = url
            captured["payload"] = payload
            return {"errcode": 0}

        with patch(
            "ai_trade.notification_channels._post_json", side_effect=poster
        ), patch("ai_trade.notification_channels.time.time", return_value=1_753_500_000.0):
            result = deliver_channel_notifications(
                self.profile,
                self.profile_id,
                [self.notification],
                email=self.email_off,
                desktop=self.desktop_off,
                pushplus=load_pushplus_settings({}),
                dingtalk=self.dingtalk,
            )

        self.assertEqual(result["dingtalk"]["status"], "succeeded")
        self.assertIn("&timestamp=1753500000000&sign=", captured["url"])
        import base64 as _b64
        import hmac as _hmac
        from hashlib import sha256 as _sha256
        from urllib.parse import quote_plus as _quote

        expected = _quote(
            _b64.b64encode(
                _hmac.new(
                    b"SECtestsecret",
                    b"1753500000000\nSECtestsecret",
                    _sha256,
                ).digest()
            ).decode("ascii")
        )
        self.assertTrue(captured["url"].endswith(f"&sign={expected}"))
        self.assertEqual(captured["payload"]["msgtype"], "text")
        self.assertIn("[AI Trade]", captured["payload"]["text"]["content"])
        self.assertIn("research_only", captured["payload"]["text"]["content"])

    def test_combined_records_verify_across_all_channels(self):
        with patch(
            "ai_trade.notification_channels._post_json",
            return_value={"code": 200, "errcode": 0},
        ):
            deliver_channel_notifications(
                self.profile,
                self.profile_id,
                [self.notification],
                email=self.email_off,
                desktop=self.desktop_off,
                pushplus=self.pushplus,
                dingtalk=self.dingtalk,
            )
        records = verify_channel_records(
            self.profile,
            self.profile_id,
            {self.notification["notification_id"]: self.notification},
        )
        self.assertEqual(
            sorted(item["channel"] for item in records), ["dingtalk", "pushplus"]
        )
        status = channel_delivery_status(
            self.profile,
            self.profile_id,
            [self.notification],
            email=self.email_off,
            desktop=self.desktop_off,
            pushplus=self.pushplus,
            dingtalk=self.dingtalk,
        )
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(status["pushplus"]["succeeded_count"], 1)
        self.assertEqual(status["dingtalk"]["succeeded_count"], 1)
