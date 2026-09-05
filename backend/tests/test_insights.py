import unittest

from backend.insights import InsightEngine


class DummyContextEngine:
    m = None


def ota_benchmark(**overrides):
    base = {
        "kpi": "ota",
        "tenant_id": "tenant-a",
        "scope": {"tenant_id": "tenant-a"},
        "period": "2026-07",
        "grain": "month",
        "overall": {
            "value": 96.0,
            "n": 1000,
            "sla": 90.0,
        },
        "groups": [
            {
                "dimension": "vendor",
                "name": "Healthy Travel",
                "value": 94.0,
                "n": 600,
                "sla_gap_pts": 4.0,
                "breached": False,
            },
            {
                "dimension": "vendor",
                "name": "Breached Travel",
                "value": 84.0,
                "n": 500,
                "sla_gap_pts": -6.0,
                "breached": True,
            },
        ],
    }
    base.update(overrides)
    return base


class InsightEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = InsightEngine(DummyContextEngine())

    def test_detects_one_insight_per_breached_ota_group(self):
        result = self.engine.evaluate_ota_benchmark(ota_benchmark())

        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["is_anomaly"])
        self.assertEqual(result[0]["group"]["name"], "Breached Travel")
        self.assertEqual(result[0]["anomaly_type"], "ota_sla_breach")
        self.assertIn("sla_breach", {s["name"] for s in result[0]["signals"]})

    def test_non_breached_groups_are_not_returned_as_anomalies(self):
        result = self.engine.evaluate_ota_benchmark(ota_benchmark(groups=[
            {
                "dimension": "vendor",
                "name": "Healthy Travel",
                "value": 94.0,
                "n": 600,
                "sla_gap_pts": 4.0,
                "breached": False,
            }
        ]))

        self.assertEqual(result, [])

    def test_low_sample_breach_is_returned_with_low_confidence(self):
        result = self.engine.evaluate_ota_benchmark(ota_benchmark(groups=[
            {
                "dimension": "vendor",
                "name": "Tiny Breach Travel",
                "value": 80.0,
                "n": 25,
                "sla_gap_pts": -10.0,
                "breached": True,
            }
        ]))

        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["is_anomaly"])
        self.assertEqual(result[0]["confidence"], "low")
        self.assertIn(
            "sample_below_confidence_floor",
            {s["name"] for s in result[0]["signals"]},
        )

    def test_daily_sample_threshold_is_lower_than_monthly(self):
        result = self.engine.evaluate_ota_benchmark(ota_benchmark(
            grain="day",
            period="2026-07-15",
            groups=[
                {
                    "dimension": "vendor",
                    "name": "Daily Breach Travel",
                    "value": 80.0,
                    "n": 25,
                    "sla_gap_pts": -10.0,
                    "breached": True,
                }
            ],
        ))

        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["is_anomaly"])

    def test_fingerprint_includes_grain_period_tenant_and_group(self):
        result = self.engine.evaluate_ota_benchmark(ota_benchmark(
            grain="week",
            period="2026-W29",
        ))

        self.assertTrue(
            result[0]["insight_id"].startswith(
                "ota|week|2026-W29|tenant_id=tenant-a|vendor=Breached Travel"
            )
        )

    def test_evaluate_context_routes_ota_benchmark_to_grouped_scanner(self):
        result = self.engine.evaluate_context(ota_benchmark())

        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["group"]["name"], "Breached Travel")

    def test_group_breaches_take_precedence_over_scoped_overall(self):
        result = self.engine.evaluate_ota_benchmark(ota_benchmark(
            scope={"tenant_id": "tenant-a", "office": "San Jose Commons"},
            overall={"value": 36.73, "n": 324, "sla": 90.0},
            groups=[
                {
                    "dimension": "vendor",
                    "name": "Small Breached Vendor",
                    "value": 27.33,
                    "n": 150,
                    "sla_gap_pts": -62.67,
                    "breached": True,
                }
            ],
        ))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["group"]["dimension"], "vendor")
        self.assertEqual(result[0]["group"]["name"], "Small Breached Vendor")
        self.assertEqual(result[0]["confidence"], "low")

    def test_scoped_overall_breach_is_fallback_when_no_groups_breach(self):
        result = self.engine.evaluate_ota_benchmark(ota_benchmark(
            scope={"tenant_id": "tenant-a", "office": "San Jose Commons"},
            overall={"value": 36.73, "n": 324, "sla": 90.0},
            groups=[
                {
                    "dimension": "vendor",
                    "name": "Healthy Vendor",
                    "value": 95.0,
                    "n": 250,
                    "sla_gap_pts": 5.0,
                    "breached": False,
                }
            ],
        ))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["group"]["dimension"], "office")
        self.assertEqual(result[0]["group"]["name"], "San Jose Commons")
        self.assertTrue(result[0]["group"]["scope_overall"])


if __name__ == "__main__":
    unittest.main()
