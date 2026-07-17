from __future__ import annotations

import base64
import http.client
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from ai_trade.broker.shadow import CANONICAL_COLUMNS
from ai_trade.config import load_config
from ai_trade.web.server import create_dashboard_server


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _csv(price: str = "10.01") -> str:
    values = (
        "fill-web-1",
        "order-web-1",
        "510300",
        "BUY",
        "100",
        price,
        "5",
        "0",
        "2026-07-15T09:31:00+08:00",
    )
    return ",".join(CANONICAL_COLUMNS) + "\r\n" + ",".join(values) + "\r\n"


class WebShadowAccountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        source = load_config(REPOSITORY_ROOT / "config" / "default.json")
        self.config = replace(source, project_root=root)
        self.server, self.token = create_dashboard_server(
            self.config, port=0, auth_enabled=False
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def _request(self, method: str, path: str, payload=None, token: str | None = None):
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["X-AI-Trade-Token"] = token
            headers["Origin"] = self.origin
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw) if raw else {}

    def test_import_api_is_local_idempotent_and_visible_in_trading_status(self):
        payload = {
            "source_label": "qmt-export",
            "account_alias": "模拟账户 A",
            "csv_base64": base64.b64encode(_csv().encode("utf-8")).decode("ascii"),
        }

        status, first = self._request(
            "POST", "/api/shadow-account/import", payload, self.token
        )
        repeated_status, repeated = self._request(
            "POST", "/api/shadow-account/import", payload, self.token
        )
        trading_status, trading = self._request("GET", "/api/trading")

        self.assertEqual(status, 201, first)
        self.assertEqual(repeated_status, 201, repeated)
        self.assertFalse(first["import_result"]["already_imported"])
        self.assertTrue(repeated["import_result"]["already_imported"])
        self.assertEqual(trading_status, 200, trading)
        shadow = trading["shadow_account"]
        self.assertEqual(shadow["fill_count"], 1)
        self.assertEqual(shadow["import_count"], 1)
        self.assertEqual(shadow["status"], "INSUFFICIENT_DATA")
        self.assertIsNone(shadow["review"]["trade_allocation_deviation"])
        self.assertEqual(
            shadow["review"]["review_reasons"],
            ["paper_comparison_unavailable"],
        )
        self.assertFalse(shadow["qualifying_evidence"])
        self.assertFalse(shadow["execution_enabled"])
        self.assertFalse(self.config.broker_orders_file.exists())
        self.assertFalse(self.config.broker_fills_file.exists())

    def test_write_security_validation_and_immutable_conflict(self):
        payload = {
            "source_label": "qmt-export",
            "account_alias": "模拟账户 A",
            "csv_base64": base64.b64encode(_csv().encode("utf-8")).decode("ascii"),
        }
        status, _ = self._request(
            "POST", "/api/shadow-account/import", payload, self.token
        )
        self.assertEqual(status, 201)

        missing_token, _ = self._request(
            "POST", "/api/shadow-account/import", payload
        )
        query_status, query_error = self._request(
            "POST", "/api/shadow-account/import?account=secret", payload, self.token
        )
        unknown_status, unknown_error = self._request(
            "POST",
            "/api/shadow-account/import",
            {**payload, "broker_password": "must-not-be-accepted"},
            self.token,
        )
        conflict_status, conflict = self._request(
            "POST",
            "/api/shadow-account/import",
            {
                **payload,
                "csv_base64": base64.b64encode(
                    _csv("10.50").encode("utf-8")
                ).decode("ascii"),
            },
            self.token,
        )
        invalid_status, invalid_error = self._request(
            "POST",
            "/api/shadow-account/import",
            {**payload, "csv_base64": "not base64!"},
            self.token,
        )

        self.assertEqual(missing_token, 403)
        self.assertEqual(query_status, 400)
        self.assertIn("query parameters", query_error["error"])
        self.assertEqual(unknown_status, 400)
        self.assertIn("broker_password", unknown_error["error"])
        self.assertEqual(conflict_status, 409)
        self.assertIn("immutable values", conflict["error"])
        self.assertEqual(invalid_status, 400)
        self.assertIn("valid Base64", invalid_error["error"])


if __name__ == "__main__":
    unittest.main()
