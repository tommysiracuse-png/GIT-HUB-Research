from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
import importlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from adapters.base import AdapterInfo  # noqa: E402
from adapters.registry import get_adapter, list_adapters, register_adapter  # noqa: E402
from signals.base import SignalInfo  # noqa: E402
from signals.registry import discover_signals, get_signal, list_signals, register_signal  # noqa: E402


class _Adapter:
    info = AdapterInfo(adapter_id="unit_test_adapter", venue="TEST", market_type="spot", source="fixture")

    def scan(self) -> list[dict]:
        return [{"venue": "TEST", "symbol": "BTC-USD"}]


class _Signal:
    info = SignalInfo(signal_id="unit_test_signal", family="fixture", version="v1")

    def generate(self, observations: list[dict]) -> list[dict]:
        return [{"signal_key": "fixture", "source_count": len(observations)}]


class PluginRegistryTests(unittest.TestCase):
    def test_adapter_registry_requires_adapter_id_and_returns_scan_output(self) -> None:
        adapter = register_adapter(_Adapter())

        self.assertIs(get_adapter("unit_test_adapter"), adapter)
        self.assertIn("unit_test_adapter", list_adapters())
        self.assertEqual(adapter.scan()[0]["venue"], "TEST")

    def test_signal_registry_requires_signal_id_and_returns_candidates(self) -> None:
        signal = register_signal(_Signal())

        self.assertIs(get_signal("unit_test_signal"), signal)
        self.assertIn("unit_test_signal", list_signals())
        self.assertEqual(signal.generate([{}])[0]["source_count"], 1)

    def test_registry_rejects_missing_identity(self) -> None:
        with self.assertRaises(ValueError):
            register_adapter(object())
        with self.assertRaises(ValueError):
            register_signal(object())

    def test_generated_signal_package_is_discovered_at_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            package = root / "fixture_generated_signals"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "alpha.py").write_text(
                "from signals.base import SignalInfo\n"
                "info = SignalInfo(signal_id='runtime_discovered_alpha', family='fixture', version='1')\n"
                "def generate(observations, context=None):\n"
                "    return []\n",
                encoding="utf-8",
            )
            sys.path.insert(0, tmpdir)
            try:
                importlib.invalidate_caches()
                report = discover_signals("fixture_generated_signals")
            finally:
                sys.path.remove(tmpdir)
                for name in list(sys.modules):
                    if name == "fixture_generated_signals" or name.startswith("fixture_generated_signals."):
                        sys.modules.pop(name, None)
        self.assertIn("runtime_discovered_alpha", report["discovered"])
        self.assertIsNotNone(get_signal("runtime_discovered_alpha"))


if __name__ == "__main__":
    unittest.main()
