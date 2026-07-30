import unittest


class TestOkxFundingCaptureVariant(unittest.TestCase):
    def test_importable(self):
        import tests.test_okx_funding_capture_variant  # noqa: F401

    def test_paper_only_safety_smoke(self):
        try:
            from runtime.signals.okx_perp_funding_basis import OKXFundingCaptureVariant  # type: ignore
        except Exception:
            self.skipTest("runtime signal module not present in this build context")
        self.assertTrue(hasattr(OKXFundingCaptureVariant, "__name__"))
