from __future__ import annotations

import copy
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from llm_bridge import _compact_route_resolver  # noqa: E402
from route_resolver import enrich_candidates, summarize_route_intelligence  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402
from tests.test_proxy_route_activation import proxy_candidate  # noqa: E402


class StatePacketBuilderTests(unittest.TestCase):
    def test_compact_route_packet_preserves_scored_okx_paper_proxy(self) -> None:
        candidate = proxy_candidate()
        candidate.update({"score": 0.0, "funding_bps": 0.0, "basis_bps": 0.0})
        routed = enrich_candidates([candidate], copy.deepcopy(DEFAULT_SETTINGS))
        route_intelligence = summarize_route_intelligence(routed)

        packet = _compact_route_resolver(
            {
                "summary": {"paper_proxy_available_count": len(routed)},
                "route_intelligence": route_intelligence,
            }
        )

        row = packet["route_intelligence"]["paper_proxy_available"][0]
        alternative = row["alternative"]
        self.assertGreater(row["score"], 0.0)
        self.assertEqual("okx_derivatives_paper", alternative["route_id"])
        self.assertEqual(0.25, alternative["paper_allocation_multiplier"])
        self.assertEqual("proxy_not_live_equivalent", alternative["execution_semantics"])


if __name__ == "__main__":
    unittest.main()
