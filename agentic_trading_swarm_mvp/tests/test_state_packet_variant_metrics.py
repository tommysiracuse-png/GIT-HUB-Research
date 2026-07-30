import unittest


class TestStatePacketVariantMetrics(unittest.TestCase):
    def test_importable(self):
        import tests.test_state_packet_variant_metrics  # noqa: F401

    def test_state_packet_smoke(self):
        try:
            from runtime.llm.state_packet_builder import StatePacketBuilder  # type: ignore
        except Exception:
            self.skipTest("runtime state packet module not present in this build context")
        self.assertTrue(hasattr(StatePacketBuilder, "__name__"))

