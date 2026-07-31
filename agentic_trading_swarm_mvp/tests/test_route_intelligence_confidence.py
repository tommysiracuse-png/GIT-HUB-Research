import unittest

from src.route_intelligence import UNKNOWN, _paper_route_confidence


class PaperRouteConfidenceTests(unittest.TestCase):
    def test_missing_route_evidence_downgrades_to_paper_conditional(self):
        checklist = {
            "venue_supports_margin_or_equivalent": "satisfied",
            "shortable_inventory_declared": "satisfied",
            "borrow_cost_model_present": "satisfied",
            "fees_modeled": "satisfied",
            "order_api_surface_mapped": UNKNOWN,
        }

        result = _paper_route_confidence(
            checklist,
            route_required=True,
            feasibility_state=UNKNOWN,
        )

        self.assertEqual(result["action"], "paper_conditional")
        self.assertEqual(result["gap_fields"], ["order_api_surface_mapped"])
        self.assertIn("order_api_surface_unconfirmed", result["reason_codes"])
        self.assertIn("route_feasibility_unconfirmed", result["reason_codes"])

    def test_complete_supported_route_remains_executable_paper(self):
        checklist = {
            "venue_supports_margin_or_equivalent": "satisfied",
            "shortable_inventory_declared": "satisfied",
            "borrow_cost_model_present": "not_applicable",
            "fees_modeled": "satisfied",
            "order_api_surface_mapped": "satisfied",
        }

        result = _paper_route_confidence(
            checklist,
            route_required=True,
            feasibility_state="supported",
        )

        self.assertEqual(result["action"], "executable_paper")
        self.assertEqual(result["gap_fields"], [])
        self.assertEqual(result["reason_codes"], [])

    def test_unsupported_route_rejects_even_without_extra_gap_reasons(self):
        checklist = {
            "venue_supports_margin_or_equivalent": "missing",
            "shortable_inventory_declared": "missing",
            "borrow_cost_model_present": "missing",
            "fees_modeled": "missing",
            "order_api_surface_mapped": "missing",
        }

        result = _paper_route_confidence(
            checklist,
            route_required=True,
            feasibility_state="unsupported",
        )

        self.assertEqual(result["action"], "reject")
