import unittest


class SelfImprovementLoopSmokeTests(unittest.TestCase):
    def test_module_imports(self) -> None:
        import tests.test_self_improvement_loop  # noqa: F401


if __name__ == "__main__":
    unittest.main()
