import unittest

from self_inspection_core.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(3, result["records"])
        self.assertEqual("complete", result["day_one_status"])
        self.assertEqual("overdue", result["day_two_status"])
        self.assertEqual(1, result["day_two_disputed"])
        self.assertTrue(all(result["checks"].values()))


if __name__ == "__main__":
    unittest.main()
