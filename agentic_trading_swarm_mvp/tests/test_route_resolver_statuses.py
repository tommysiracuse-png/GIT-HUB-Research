import unittest

from src.route_resolver import ROUTE_STATUSES


class RouteResolverStatusesTests(unittest.TestCase):
    def test_route_resolver_accepts_paper_route_statuses(self):
        self.assertIn("paper_testable_via_proxy", ROUTE_STATUSES)
        self.assertIn("blocked_until_requirements_confirmed", ROUTE_STATUSES)
        self.assertIn("paper_observation_only", ROUTE_STATUSES)

    def test_route_resolver_preserves_existing_statuses(self):
        self.assertIn("standard", ROUTE_STATUSES)
        self.assertIn("conditional", ROUTE_STATUSES)
        self.assertIn("blocked", ROUTE_STATUSES)
        self.assertIn("unsupported_or_unknown", ROUTE_STATUSES)


if __name__ == "__main__":
    unittest.main()
