from __future__ import annotations

import io
import json
import logging
import shutil
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from ai_trade.cli import main


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _reset_root_logging() -> None:
    """Close CLI log handlers so Windows can delete the temporary project.

    ``main()`` attaches a FileHandler inside the temporary directory; an open
    log file makes TemporaryDirectory cleanup fail with WinError 32 there.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


def _bars_csv(dates: list[str]) -> str:
    lines = ["date,open,close,high,low,volume,amount,amplitude"]
    for index, value in enumerate(dates):
        price = 1.0 + index * 0.01
        lines.append(
            f"{value},{price},{price},{price},{price},1000000,100000000,"
        )
    return "\n".join(lines) + "\n"


class UniverseVerifyTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # LIFO: runs before the directory cleanup above.
        self.addCleanup(_reset_root_logging)
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        shutil.copy(
            REPOSITORY_ROOT / "config" / "default.json",
            self.root / "config" / "default.json",
        )
        master = json.loads(
            (REPOSITORY_ROOT / "config" / "security_master.json").read_text(
                encoding="utf-8"
            )
        )
        keep = {"510300", "510500", "512720"}
        master["instruments"] = [
            item for item in master["instruments"] if item["symbol"] in keep
        ]
        master["universes"]["core_etf"] = [
            item
            for item in master["universes"]["core_etf"]
            if item["symbol"] in keep
        ]
        (self.root / "config" / "security_master.json").write_text(
            json.dumps(master, ensure_ascii=False), encoding="utf-8"
        )
        cache = self.root / "data" / "cache"
        cache.mkdir(parents=True)
        # 510300: history from the data-window start — healthy.
        (cache / "510300.csv").write_text(
            _bars_csv(["2013-01-04", "2013-01-07", "2026-07-24"]),
            encoding="utf-8",
        )
        # 512720: membership starts 2019-11-01 but cached history only
        # begins in 2020 — the dangerous direction.
        (cache / "512720.csv").write_text(
            _bars_csv(["2020-03-02", "2020-03-03", "2026-07-24"]),
            encoding="utf-8",
        )
        # 510500 has no cache file at all.

    def _run(self) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(
                [
                    "--config",
                    str(self.root / "config" / "default.json"),
                    "universe-verify",
                ]
            )
        raw = output.getvalue()
        return status, json.loads(raw[raw.find("{"):])

    def test_reports_dangerous_membership_missing_cache_and_disclosures(self):
        status, result = self._run()

        self.assertEqual(status, 1)
        summary = result["summary"]
        self.assertEqual(summary["instruments"], 3)
        self.assertEqual(summary["missing_cache"], 1)
        self.assertEqual(summary["dangerous_issues"], 1)
        rows = {item["symbol"]: item for item in result["members"]}
        self.assertEqual(rows["510500"]["status"], "missing_cache")
        self.assertEqual(
            rows["512720"]["status"], "membership_before_first_bar"
        )
        self.assertIn("推迟", " ".join(rows["512720"]["notes"]))
        self.assertEqual(rows["510300"]["status"], "ok")
        self.assertTrue(
            any("数据窗口起点" in note for note in rows["510300"]["notes"])
        )
        self.assertTrue(result["safety"]["read_only"])

    def test_healthy_membership_passes_after_dates_are_fixed(self):
        master_path = self.root / "config" / "security_master.json"
        master = json.loads(master_path.read_text(encoding="utf-8"))
        for member in master["universes"]["core_etf"]:
            if member["symbol"] == "512720":
                member["start"] = "2020-06-01"
        master_path.write_text(
            json.dumps(master, ensure_ascii=False), encoding="utf-8"
        )
        cache = self.root / "data" / "cache"
        (cache / "510500.csv").write_text(
            _bars_csv(["2013-03-15", "2026-07-24"]), encoding="utf-8"
        )

        status, result = self._run()

        self.assertEqual(status, 0)
        self.assertEqual(result["summary"]["dangerous_issues"], 0)
        self.assertEqual(result["summary"]["missing_cache"], 0)


if __name__ == "__main__":
    import unittest

    unittest.main()
