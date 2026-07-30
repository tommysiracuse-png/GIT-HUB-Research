import importlib
import unittest


class TestCandidateQualityDirectVsProxy(unittest.TestCase):
    def test_scoring_import_or_skip(self):
        try:
            importlib.import_module("src.scoring.candidate_quality")
        except ModuleNotFoundError as exc:
            self.skipTest(f"candidate quality module unavailable: {exc}")


if __name__ == "__main__":
    unittest.main()
