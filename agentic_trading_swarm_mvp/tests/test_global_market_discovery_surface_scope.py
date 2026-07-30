import unittest

from global_market_discovery_scanner import _build_targets, _required_target


class GlobalMarketDiscoverySurfaceScopeTests(unittest.TestCase):
    def test_targets_include_surface_scope_metadata(self):
        targets = _build_targets(
            [{"venue_or_source": "B3", "priority": 90, "confidence": 0.9}],
            settings={"global_market_discovery_scanner": {"max_proxy_symbols_per_surface": 1}},
        )
        self.assertTrue(targets)
        discovery, proxy = targets[0]
        scope = proxy.get("paper_strategy_surface_scope")

        self.assertIsInstance(scope, dict)
        self.assertEqual(discovery.get("paper_strategy_surface_scope_key"), proxy.get("paper_strategy_surface_scope_key"))
        self.assertEqual(scope.get("scope_key"), proxy.get("paper_strategy_surface_scope_key"))
        self.assertEqual(scope.get("execution_surface"), proxy.get("surface"))
        self.assertTrue(scope.get("exact_match_required"))
        self.assertTrue(scope.get("paper_only"))
        self.assertEqual(scope.get("replay_policy"), "exact_declared_surface_only")
        self.assertEqual(scope.get("scope_mismatch_action"), "refuse_reuse")
        self.assertEqual(scope.get("validation_status"), "paper_invalid_until_fresh_target_surface_validation")
        self.assertTrue(scope.get("instrument_family"))
        self.assertTrue(scope.get("direction_family"))

    def test_required_targets_are_surface_scoped(self):
        target = _required_target("b3:EWZ")
        self.assertIsNotNone(target)
        discovery, proxy = target
        scope = proxy.get("paper_strategy_surface_scope")

        self.assertIsInstance(scope, dict)
        self.assertEqual(scope.get("execution_surface"), "required_open_trade")
        self.assertEqual(scope.get("scope_key"), discovery.get("paper_strategy_surface_scope_key"))
        self.assertTrue(scope.get("exact_match_required"))
        self.assertEqual(scope.get("scope_mismatch_action"), "refuse_reuse")

    def test_active_paper_cohort_scope_uses_cohort_surface(self):
        targets = _build_targets(
            [{"venue_or_source": "Cboe Global Markets", "priority": 95, "confidence": 0.95}],
            settings={
                "global_market_discovery_scanner": {
                    "max_proxy_symbols_per_surface": 1,
                    "active_paper_cohort_enabled": True,
                }
            },
        )
        _, proxy = next(item for item in targets if item[1].get("symbol") == "VIXY")
        scope = proxy.get("paper_strategy_surface_scope")

        self.assertEqual(proxy.get("cohort_surface"), "volatility_long")
        self.assertEqual(scope.get("execution_surface"), "volatility_long")
        self.assertEqual(scope.get("direction_family"), "long_only")


if __name__ == "__main__":
    unittest.main()
