from __future__ import annotations

from hashlib import sha256
import hmac
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_trade.monitoring import _fingerprint
from ai_trade.notification_delivery import (
    deliver_webhook_notifications,
    load_webhook_settings,
    verify_webhook_records,
)


def _notification() -> dict:
    record = {
        "schema_version": 1,
        "notification_id": "notification_" + "a" * 32,
        "created_at": "2026-07-20T08:00:00Z",
        "source_type": "alert",
        "source_id": "alert_" + "b" * 32,
        "source_fingerprint": "c" * 64,
        "evidence_fingerprint": "d" * 64,
        "severity": "warning",
        "title": "600000 · close above",
        "message": "The completed close triggered the research rule.",
        "symbol": "600000",
        "data_date": "2026-07-17",
        "status": "unread",
    }
    record["fingerprint"] = _fingerprint(record)
    record["state_fingerprint"] = "e" * 64
    return record


def _environment(**changes: str) -> dict[str, str]:
    values = {
        "AI_TRADE_WEBHOOK_URL": "http://127.0.0.1:9876/hooks/monitoring",
        "AI_TRADE_WEBHOOK_SECRET": "0123456789abcdef0123456789abcdef",
        "AI_TRADE_WEBHOOK_TIMEOUT_SECONDS": "2",
        "AI_TRADE_WEBHOOK_MAX_ATTEMPTS": "3",
        "AI_TRADE_WEBHOOK_RETRY_BASE_SECONDS": "0",
        "AI_TRADE_WEBHOOK_BATCH_SIZE": "10",
    }
    values.update(changes)
    return values


class _Response:
    def __init__(self, status: int, body: bytes = b"{}"):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def getcode(self):
        return self.status

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]


class NotificationDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.profile_id = sha256(b"alice").hexdigest()
        self.profile = self.root / "users" / self.profile_id
        self.notification = _notification()
        self.settings = load_webhook_settings(_environment())
        self.addresses = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 9876),
            )
        ]

    def tearDown(self):
        self.temporary.cleanup()

    def test_hmac_delivery_is_idempotent_and_persists_immutable_evidence(self):
        requests = []

        def open_request(request, _timeout):
            requests.append(request)
            return _Response(204, b"")

        with patch(
            "ai_trade.notification_delivery.socket.getaddrinfo",
            return_value=self.addresses,
        ), patch(
            "ai_trade.notification_delivery._open_request",
            side_effect=open_request,
        ):
            first = deliver_webhook_notifications(
                self.profile,
                self.profile_id,
                [self.notification],
                settings=self.settings,
            )
            second = deliver_webhook_notifications(
                self.profile,
                self.profile_id,
                [self.notification],
                settings=self.settings,
            )

        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(second["status"], "succeeded")
        self.assertEqual(len(requests), 1)
        request = requests[0]
        body = request.data
        timestamp = request.get_header("X-ai-trade-timestamp")
        expected = hmac.new(
            self.settings.secret,
            timestamp.encode("ascii") + b"." + body,
            sha256,
        ).hexdigest()
        self.assertEqual(
            request.get_header("X-ai-trade-signature"), f"sha256={expected}"
        )
        self.assertEqual(
            request.get_header("Idempotency-key"),
            json.loads(body)["delivery_id"],
        )
        self.assertNotIn("state_fingerprint", json.loads(body)["notification"])
        self.assertEqual(
            len(list((self.profile / "webhook_outbox").glob("*.json"))), 1
        )
        self.assertEqual(
            len(list((self.profile / "webhook_attempts").glob("*.json"))), 1
        )

    def test_retry_failures_are_bounded_and_do_not_raise(self):
        open_request = Mock(return_value=_Response(503, b"unavailable"))
        with patch(
            "ai_trade.notification_delivery.socket.getaddrinfo",
            return_value=self.addresses,
        ), patch(
            "ai_trade.notification_delivery._open_request", open_request
        ), patch("ai_trade.notification_delivery.time.sleep"):
            result = deliver_webhook_notifications(
                self.profile,
                self.profile_id,
                [self.notification],
                settings=self.settings,
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["attempt_count"], 3)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(open_request.call_count, 3)
        self.assertEqual(
            len(list((self.profile / "webhook_attempts").glob("*.json"))), 3
        )

    def test_external_http_and_incomplete_secrets_fail_closed(self):
        external = load_webhook_settings(
            _environment(AI_TRADE_WEBHOOK_URL="http://example.com/hook")
        )
        incomplete = load_webhook_settings(
            {"AI_TRADE_WEBHOOK_URL": "https://example.com/hook"}
        )

        self.assertFalse(external.enabled)
        self.assertIn("HTTPS", external.configuration_error)
        self.assertFalse(incomplete.enabled)
        self.assertIn("set together", incomplete.configuration_error)

    def test_tampered_outbox_is_rejected(self):
        with patch(
            "ai_trade.notification_delivery.socket.getaddrinfo",
            return_value=self.addresses,
        ), patch(
            "ai_trade.notification_delivery._open_request",
            return_value=_Response(204, b""),
        ):
            deliver_webhook_notifications(
                self.profile,
                self.profile_id,
                [self.notification],
                settings=self.settings,
            )
        path = next((self.profile / "webhook_outbox").glob("*.json"))
        record = json.loads(path.read_text(encoding="utf-8"))
        record["payload"]["notification"]["title"] = "forged"
        path.write_text(json.dumps(record), encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "fingerprint"):
            verify_webhook_records(
                self.profile,
                self.profile_id,
                {self.notification["notification_id"]: self.notification},
            )


if __name__ == "__main__":
    unittest.main()
