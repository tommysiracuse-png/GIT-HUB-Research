import unittest


class CodeEvolutionRepairSmokeTest(unittest.TestCase):
    def test_module_imports(self):
        __import__("tests.test_code_evolution_repair")


if __name__ == "__main__":
    unittest.main()
