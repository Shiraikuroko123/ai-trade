from __future__ import annotations

import http.client
import unittest
import urllib.error

from ai_trade.data.eastmoney import (
    EastmoneyDownloadError,
    _is_transport_failure,
    _should_retry_eastmoney,
)


class EastmoneyTransportClassificationTests(unittest.TestCase):
    def test_non_retryable_client_errors_do_not_open_circuit(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                error = _http_error(status)
                wrapped = EastmoneyDownloadError("failed", [error])
                self.assertFalse(_should_retry_eastmoney(error))
                self.assertFalse(_is_transport_failure(wrapped))

    def test_rate_limit_server_error_and_disconnect_open_circuit(self):
        failures = [
            _http_error(429),
            _http_error(503),
            http.client.RemoteDisconnected("provider disconnected"),
        ]

        self.assertTrue(
            _is_transport_failure(EastmoneyDownloadError("failed", failures))
        )
        self.assertTrue(_should_retry_eastmoney(failures[0]))
        self.assertTrue(_should_retry_eastmoney(failures[1]))

    def test_all_attempts_must_be_provider_wide_failures(self):
        failures = [
            http.client.RemoteDisconnected("provider disconnected"),
            ValueError("invalid provider JSON"),
        ]

        self.assertFalse(
            _is_transport_failure(EastmoneyDownloadError("failed", failures))
        )

    def test_payload_and_local_validation_errors_are_not_retried(self):
        for error in (
            ValueError("invalid provider JSON"),
            RuntimeError("unexpected response schema"),
            PermissionError("cache is locked"),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertFalse(_should_retry_eastmoney(error))

    def test_nested_network_error_is_classified_but_local_os_error_is_not(self):
        try:
            raise http.client.RemoteDisconnected("provider disconnected")
        except http.client.RemoteDisconnected as cause:
            wrapped = RuntimeError("attempts exhausted")
            wrapped.__cause__ = cause

        self.assertTrue(_is_transport_failure(wrapped))
        self.assertFalse(_is_transport_failure(PermissionError("cache is locked")))


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://push2his.eastmoney.com/",
        status,
        "test status",
        hdrs=None,
        fp=None,
    )


if __name__ == "__main__":
    unittest.main()
