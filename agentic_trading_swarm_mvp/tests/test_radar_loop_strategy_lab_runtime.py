import unittest


try:
    from radar_loop import strategy_lab_runtime as strategy_lab_runtime_module
except Exception:  # pragma: no cover
    strategy_lab_runtime_module = None


class StrategyLabRuntimeSelectionTests(unittest.TestCase):
    SURFACE_POLICY = {"eligible": True, "reason": "surface_compatible"}
    def setUp(self):
        if strategy_lab_runtime_module is None:
            self.skipTest("strategy lab runtime module unavailable")

    def is_candidate_filter(self, item):
        return strategy_lab_runtime_module.is_candidate_filter(item)

    def build_runtime_summary(self, candidates, **kwargs):
        return strategy_lab_runtime_module.build_runtime_summary(candidates, **kwargs)

    def reserve_review_candidates(self, candidates, settings, total_slots):
        return strategy_lab_runtime_module.reserve_review_candidates(candidates, settings, total_slots)

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

    def test_review_reserve_selects_distinct_lab_experiments(self):
        candidates = [
            {"strategy_lab_id": "lab_a", "strategy_lab_logic_type": "candidate_filter", "inst_id": "A", "direction": "long_proxy", "score": 99, "strategy_lab_surface_policy": self.SURFACE_POLICY},
            {"strategy_lab_id": "lab_a", "strategy_lab_logic_type": "candidate_filter", "inst_id": "A2", "direction": "long_proxy", "score": 98, "strategy_lab_surface_policy": self.SURFACE_POLICY},
            {"strategy_lab_id": "lab_b", "strategy_lab_logic_type": "candidate_filter", "inst_id": "B", "direction": "long_proxy", "score": 80, "strategy_lab_surface_policy": self.SURFACE_POLICY},
            {"strategy_lab_id": "lab_c", "strategy_lab_logic_type": "candidate_filter", "inst_id": "C", "direction": "long_proxy", "score": 70, "strategy_lab_surface_policy": self.SURFACE_POLICY},
        ]

        selected, summary = self.reserve_review_candidates(
            candidates,
            {"strategy_lab": {"runtime_review_reserved_slots": 2}},
            total_slots=25,
        )

        self.assertEqual(["lab_a", "lab_b"], [row["strategy_lab_id"] for row in selected])
        self.assertEqual(2, summary["reserved_count"])
        self.assertTrue(all(row["_hunter_allocation_reason"] == "strategy_lab_distinct_experiment_reserve" for row in selected))

    def test_review_reserve_excludes_watch_only_candidates(self):
        selected, summary = self.reserve_review_candidates(
            [{"strategy_lab_id": "lab_watch", "strategy_lab_logic_type": "candidate_filter", "direction": "watch_only", "score": 100, "strategy_lab_surface_policy": self.SURFACE_POLICY}],
            {"strategy_lab": {"runtime_review_reserved_slots": 5}},
            total_slots=25,
        )

        self.assertEqual([], selected)
        self.assertEqual(0, summary["reserved_count"])

    def test_review_reserve_prefers_standard_routes_and_distinct_sources(self):
        candidates = [
            {"strategy_lab_id": "lab_a", "strategy_lab_logic_type": "candidate_filter", "venue": "OKX", "inst_id": "LA-SWAP", "direction": "long_perp_short_spot", "trade_type": "basis", "route_status": "conditional", "score": 100, "strategy_lab_surface_policy": self.SURFACE_POLICY},
            {"strategy_lab_id": "lab_a", "strategy_lab_logic_type": "candidate_filter", "venue": "OKX_SPOT", "inst_id": "BTC-USDT", "direction": "long_frontier_spot", "trade_type": "spot", "route_status": "standard", "score": 80, "strategy_lab_surface_policy": self.SURFACE_POLICY},
            {"strategy_lab_id": "lab_b", "strategy_lab_logic_type": "candidate_filter", "venue": "OKX_SPOT", "inst_id": "BTC-USDT", "direction": "long_frontier_spot", "trade_type": "spot", "route_status": "standard", "score": 90, "strategy_lab_surface_policy": self.SURFACE_POLICY},
            {"strategy_lab_id": "lab_b", "strategy_lab_logic_type": "candidate_filter", "venue": "KRAKEN", "inst_id": "ETHUSD", "direction": "long_frontier_spot", "trade_type": "spot", "route_status": "standard", "score": 70, "strategy_lab_surface_policy": self.SURFACE_POLICY},
        ]

        selected, summary = self.reserve_review_candidates(
            candidates,
            {"strategy_lab": {"runtime_review_reserved_slots": 2}},
            total_slots=25,
        )

        self.assertEqual({"BTC-USDT", "ETHUSD"}, {row["inst_id"] for row in selected})
        self.assertEqual(2, summary["distinct_source_count"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
