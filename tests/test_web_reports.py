import http.client
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from ai_trade.config import load_config
from ai_trade.web.server import create_dashboard_server


class WebReportTests(unittest.TestCase):
    def test_report_download_is_scoped_to_known_report_files(self):
        source = load_config(
            Path(__file__).resolve().parents[1] / "config/default.json"
        )
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(source, project_root=Path(temporary))
            config.reports_dir.mkdir(parents=True)
            report = config.reports_dir / "sample.json"
            report.write_text('{"status":"ok"}', encoding="utf-8")
            (config.reports_dir / "secret.exe").write_bytes(b"not-a-report")

            server, _ = create_dashboard_server(config, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, headers, body = _request(
                    server.server_port, "GET", "/reports/sample.json"
                )
                self.assertEqual(status, 200)
                self.assertEqual(body, report.read_bytes())
                self.assertIn("application/json", headers["content-type"])
                self.assertEqual(
                    headers["content-disposition"],
                    'attachment; filename="sample.json"',
                )

                status, headers, body = _request(
                    server.server_port, "HEAD", "/reports/sample.json"
                )
                self.assertEqual(status, 200)
                self.assertEqual(body, b"")
                self.assertEqual(int(headers["content-length"]), report.stat().st_size)

                for path in (
                    "/reports/%2e%2e%2fconfig%2fdefault.json",
                    "/reports/secret.exe",
                    "/reports/missing.csv",
                ):
                    with self.subTest(path=path):
                        status, _, _ = _request(server.server_port, "GET", path)
                        self.assertEqual(status, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


def _request(port, method, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path)
    response = connection.getresponse()
    value = (
        response.status,
        {name.lower(): item for name, item in response.getheaders()},
        response.read(),
    )
    connection.close()
    return value


if __name__ == "__main__":
    unittest.main()
