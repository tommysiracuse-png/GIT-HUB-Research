import unittest

from src.frontier_crypto_adapter import paper_only_build_governor_fields


class BuildGovernorFieldsTest(unittest.TestCase):
    def test_defaults_are_paper_only_and_non_trade_effecting(self):
        fields = paper_only_build_governor_fields()
        self.assertEqual(fields["category"], "paper_scoring_logic")
        self.assertEqual(fields["implementation_mode"], "paper_policy")
        self.assertTrue(fields["paper_only"])
        self.assertFalse(fields["trade_effecting"])

    def test_custom_fields_are_preserved(self):
        fields = paper_only_build_governor_fields(
            category="paper_risk_logic",
            implementation_mode="paper_report",
            paper_only=True,
            trade_effecting=False,
        )
        self.assertEqual(fields["category"], "paper_risk_logic")
        self.assertEqual(fields["implementation_mode"], "paper_report")


if __name__ == "__main__":
    unittest.main()
