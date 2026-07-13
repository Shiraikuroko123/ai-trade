import json
import tempfile
import unittest
from pathlib import Path

from ai_trade.cli import main


class CliTests(unittest.TestCase):
    def test_packaged_default_matches_repository_config(self):
        root = Path(__file__).resolve().parents[1]
        repository = json.loads((root / "config/default.json").read_text(encoding="utf-8"))
        packaged = json.loads(
            (root / "src/ai_trade/default_config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(packaged, repository)

    def test_init_creates_standalone_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "workspace"
            self.assertEqual(main(["init", "--directory", str(target)]), 0)
            self.assertTrue((target / "config/default.json").exists())
            self.assertTrue((target / "data/cache/.gitkeep").exists())
            self.assertTrue((target / "state/.gitkeep").exists())


if __name__ == "__main__":
    unittest.main()
