import unittest


try:
    from radar_loop import strategy_lab_runtime as strategy_lab_runtime_module
except Exception:  # pragma: no cover
    strategy_lab_runtime_module = None


class StrategyLabRuntimeSelectionTests(unittest.TestCase):
    def setUp(self):
        if strategy_lab_runtime_module is None:
            self.skipTest("strategy lab runtime module unavailable")

    def is_candidate_filter(self, item):
        return strategy_lab_runtime_module.is_candidate_filter(item)

    def build_runtime_summary(self, candidates, **kwargs):
        return strategy_lab_runtime_module.build_runtime_summary(candidates, **kwargs)

    def test_candidate_filter_recognizes_supported_variants(self):
        self.assertTrue(self.is_candidate_filter({"strategy_lab_type": "candidate_filter"}))
        self.assertTrue(self.is_candidate_filter({"tags": ["research", "candidate-filter"]}))
        self.assertTrue(self.is_candidate_filter({"metadata": {"artifact_type": "filter"}}))
        self.assertFalse(self.is_candidate_filter({"strategy_lab_type": "variant_score"}))

    def test_runtime_selection_is_disabled_by_default(self):
        candidates = [
            {"strategy_lab_type": "candidate_filter", "enabled": True},
            {"strategy_lab_type": "variant_score", "enabled": True},
        ]

        summary = self.build_runtime_summary(candidates)

        self.assertEqual(summary["runtime_selection_mode"], "disabled")
        self.assertEqual(summary["accepted_count"], 0)
        self.assertEqual(summary["generated_count"], 0)

    def test_runtime_selection_can_emit_lab_generation_summary(self):
        candidates = [
            {"strategy_lab_type": "candidate_filter", "enabled": True},
        ]

        summary = self.build_runtime_summary(
            candidates,
            runtime_selection_mode="lab_generation",
            generated_count=1,
            accepted_count=1,
        )

        self.assertEqual(summary["runtime_selection_mode"], "lab_generation")
        self.assertEqual(summary["generated_count"], 1)
        self.assertEqual(summary["accepted_count"], 1)

    def test_runtime_summary_includes_selection_mode_when_explicitly_enabled(self):
        candidates = [
            {"strategy_lab_type": "candidate_filter", "enabled": True},
            {"strategy_lab_type": "candidate_filter", "enabled": False},
        ]

        summary = self.build_runtime_summary(
            candidates,
            runtime_selection_mode="lab_generation",
            generated_count=1,
            accepted_count=1,
        )

        self.assertEqual(
            summary,
            {
                "generated_count": 1,
                "accepted_count": 1,
                "selection_mode": "lab_generation",
                "runtime_selection_mode": "lab_generation",
            },
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
