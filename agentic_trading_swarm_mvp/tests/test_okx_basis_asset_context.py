import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from self_improvement import _infer_code_category


class OkxBasisAssetContextTests(unittest.TestCase):
    def test_runtime_pipeline_integration_category(self) -> None:
        payload = {
            "action": "propose_code_change",
            "title": "Wire OKX basis asset context end-to-end",
            "code_change": {
                "implementation_mode": "runtime_active",
                "expected_files": [
                    "src/paper_runtime/candidate_builder.py",
                    "src/paper_runtime/paper_trade_recorder.py",
                    "src/paper_runtime/position_serializer.py",
                ],
            },
            "proposed_change": "Preserve base_asset and quote_asset through candidate builder, paper trade recorder, and position serializer for paper persistence.",
        }
        self.assertEqual(_infer_code_category(payload), "runtime_pipeline_integration")

