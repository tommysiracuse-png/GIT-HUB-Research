import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import settings
import strategy_reliability


class SettingsLocalOverrideTest(unittest.TestCase):
    def test_default_paper_context_priors_match_strategy_reliability_policy(self) -> None:
        self.assertEqual(
            settings.DEFAULT_SETTINGS["paper_context_priors"],
            strategy_reliability.PAPER_CONTEXT_PRIOR_DEFAULTS,
        )

    def test_default_load_merges_ignored_local_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            default_path = config_dir / "settings.example.json"
            local_path = config_dir / "settings.local.json"
            default_path.write_text(
                json.dumps(
                    {
                        "allow_live_trading": False,
                        "account_capabilities": {
                            "prediction_markets": False,
                            "options": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            local_path.write_text(
                json.dumps(
                    {
                        "account_capabilities": {
                            "prediction_markets": True,
                            "options": True,
                        }
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(settings, "DEFAULT_SETTINGS_PATH", default_path), mock.patch.object(
                settings, "LOCAL_SETTINGS_PATH", local_path
            ):
                loaded = settings.load_settings()

        self.assertFalse(loaded["allow_live_trading"])
        self.assertTrue(loaded["account_capabilities"]["prediction_markets"])
        self.assertTrue(loaded["account_capabilities"]["options"])

    def test_explicit_config_path_does_not_merge_local_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            explicit_path = config_dir / "custom.json"
            local_path = config_dir / "settings.local.json"
            explicit_path.write_text(
                json.dumps({"account_capabilities": {"prediction_markets": False}}),
                encoding="utf-8",
            )
            local_path.write_text(
                json.dumps({"account_capabilities": {"prediction_markets": True}}),
                encoding="utf-8",
            )

            with mock.patch.object(settings, "LOCAL_SETTINGS_PATH", local_path):
                loaded = settings.load_settings(explicit_path)

        self.assertFalse(loaded["account_capabilities"]["prediction_markets"])


if __name__ == "__main__":
    unittest.main()
