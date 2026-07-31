import unittest


class LLMStatePacketSchemaSmokeTests(unittest.TestCase):
    def test_module_imports(self) -> None:
        import tests.test_llm_state_packet_schema  # noqa: F401


if __name__ == "__main__":
    unittest.main()
