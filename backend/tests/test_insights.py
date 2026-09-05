import unittest

from backend.insights import InsightEngine


class DummyContextEngine:
    m = None


def ctx(**overrides):
    base = {
        "kpi": "ota",
        "label": "On-time arrival",
        "unit": "%",
        "value": 84.0,
        "n": 500,
        "filters": {"tenant_id": "tenant-a", "vendor": "vendor-a"},
        "grain": "month",
        "period": "2026-07",
        "month": "2026-07",
        "good_direction": "up",
        "trend": {"delta": -6.0, "improving": False},
        "sla": {"target": 90.0, "gap_pts": -6.0, "breached": True},
        "peer": {"dim": "vendor", "percentile": 10},
        "industry": {"norm": 94.0, "delta": -10.0, "better_than_norm": False},
        "drivers_of_change": [
            {"label": "vendor-a", "contribution_pct": 45.0, "n": 500, "value": 84.0}
        ],
    }
    base.update(overrides)
    return base


class InsightEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = InsightEngine(DummyContextEngine())

    def test_detects_high_priority_ota_anomaly(self):
        result = self.engine.evaluate_context(ctx())

        self.assertTrue(result["is_anomaly"])
        self.assertEqual(result["priority_band"], "critical")
        self.assertIn("sla_breach", {s["name"] for s in result["signals"]})
        self.assertIn("adverse_trend", {s["name"] for s in result["signals"]})
        self.assertIn("weak_peer_position", {s["name"] for s in result["signals"]})

    def test_healthy_context_is_not_anomaly(self):
        result = self.engine.evaluate_context(ctx(
            value=97.0,
            n=1500,
            trend={"delta": 2.0, "improving": True},
            sla={"target": 90.0, "gap_pts": 7.0, "breached": False},
            peer={"dim": "vendor", "percentile": 70},
            industry={"norm": 94.0, "delta": 3.0, "better_than_norm": True},
            drivers_of_change=[],
        ))

        self.assertFalse(result["is_anomaly"])
        self.assertEqual(result["priority_band"], "low")

    def test_low_sample_is_suppressed(self):
        result = self.engine.evaluate_context(ctx(n=50))

        self.assertFalse(result["is_anomaly"])
        self.assertEqual(result["priority_score"], 0.0)
        self.assertEqual(result["signals"][0]["name"], "sample_too_small")

    def test_down_is_good_metric_uses_positive_delta_as_adverse(self):
        result = self.engine.evaluate_context(ctx(
            kpi="cost_per_trip",
            label="Cost per trip",
            unit="INR",
            value=1500.0,
            good_direction="down",
            trend={"delta": 150.0, "improving": False},
            sla={"target": None, "gap_pts": None, "breached": None},
            peer={"dim": "vendor", "percentile": 20},
            industry={"norm": 1300.0, "delta": 200.0, "better_than_norm": False},
        ))

        self.assertTrue(result["is_anomaly"])
        self.assertIn("cost_increase", result["anomaly_type"])
        self.assertIn("adverse_trend", {s["name"] for s in result["signals"]})

    def test_fingerprint_includes_grain_and_period(self):
        result = self.engine.evaluate_context(ctx(grain="week", period="2026-W29", month=None))

        self.assertTrue(result["insight_id"].startswith("ota|week|2026-W29|"))


if __name__ == "__main__":
    unittest.main()
