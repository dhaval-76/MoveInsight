import unittest

from backend.context import ContextEngine


class FakeMetrics:
    def periods(self, grain="month", filters=None):
        return {
            "month": ["2026-06", "2026-07"],
            "week": ["2026-W28", "2026-W29"],
            "day": ["2026-07-14", "2026-07-15"],
        }[grain]

    def ota(self, filters=None, period=None, grain="month"):
        if filters and filters.get("vendor") == "Breached Travel":
            return {"value": 84.0, "n": 500}
        if filters and filters.get("vendor") == "Healthy Travel":
            return {"value": 94.0, "n": 600}
        return {"value": 96.0, "n": 1100}

    def kpi_by_group(self, method_name, dim, base_filters=None, period=None,
                     grain="month", min_n=1):
        return [
            {"group": "Healthy Travel", "value": 94.0, "n": 600},
            {"group": "Breached Travel", "value": 84.0, "n": 500},
        ]


class ContextEngineTests(unittest.TestCase):
    def test_ota_context_returns_grouped_benchmark_contract(self):
        c3 = ContextEngine(FakeMetrics())

        result = c3.context("ota", {"tenant_id": "tenant-a"}, period="2026-07")

        self.assertEqual(result["kpi"], "ota")
        self.assertEqual(result["tenant_id"], "tenant-a")
        self.assertEqual(result["overall"]["sla"], 90.0)
        self.assertEqual(len(result["groups"]), 2)
        self.assertNotIn("drivers_of_change", result)
        self.assertEqual(result["groups"][0]["name"], "Breached Travel")
        self.assertTrue(result["groups"][0]["breached"])

    def test_ota_context_uses_latest_period_when_missing(self):
        c3 = ContextEngine(FakeMetrics())

        result = c3.context("ota", {"tenant_id": "tenant-a"}, grain="day")

        self.assertEqual(result["grain"], "day")
        self.assertEqual(result["period"], "2026-07-15")


if __name__ == "__main__":
    unittest.main()
