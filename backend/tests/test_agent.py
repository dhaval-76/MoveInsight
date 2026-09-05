import unittest

from backend.agent import AgentOrchestrator
from backend.context import ContextEngine
from backend.insights import InsightEngine
from backend.metrics import Metrics

DB_PATH = "backend/mobility.duckdb"


def sample_c4_anomaly():
    return {
        "insight_id": "ota|month|2026-07|office=San Jose Commons|tenant_id=pinnacle-Slc",
        "kpi": "ota",
        "anomaly_type": "ota_degradation",
        "is_anomaly": True,
        "priority_score": 72.6,
        "priority_band": "high",
        "signals": [
            {"name": "sample_confident", "detail": "n=324 meets confidence floor", "points": 10},
            {"name": "sla_breach", "detail": "53.27 pts below target", "points": 30.0, "bad": True},
            {"name": "adverse_trend", "detail": "moved 6.32 % in the bad direction", "points": 22.6, "bad": True},
            {"name": "industry_benchmark_miss", "detail": "57.27 % away from industry norm", "points": 10.0, "bad": True},
        ],
        "context": {
            "kpi": "ota",
            "label": "On-time arrival",
            "unit": "%",
            "value": 36.73,
            "n": 324,
            "filters": {"tenant_id": "pinnacle-Slc", "office": "San Jose Commons"},
            "period": "2026-07",
            "month": "2026-07",
            "good_direction": "up",
            "trend": {"delta": -6.32, "improving": False},
            "sla": {"target": 90.0, "gap_pts": -53.27, "breached": True},
            "peer": {"dim": "vendor", "percentile": 10},
            "industry": {"norm": 94.0, "delta": -57.27, "better_than_norm": False},
            "drivers_of_change": [
                {"label": "Pooja Mikhailov Travel", "contribution_pct": 67.3, "n": 200, "value": 32.0}
            ],
            "headline": "On-time arrival 36.73%; down from 43.05 last month; 53.27pts below SLA (90.0).",
        },
        "summary": "On-time arrival classified as anomaly for month 2026-07 with priority score 72.6.",
    }


class AgentOrchestratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.metrics = Metrics(DB_PATH)
            cls.context_engine = ContextEngine(cls.metrics)
            cls.insight_engine = InsightEngine(cls.context_engine)
            cls.agent = AgentOrchestrator(cls.context_engine, cls.insight_engine)
            cls.db_available = True
        except Exception:
            cls.db_available = False

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "db_available", False) and cls.metrics:
            cls.metrics.close()

    def test_sample_c4_payload_reasoning_enabled(self):
        if not self.db_available:
            self.skipTest("mobility.duckdb not available")

        res = self.agent.process_anomaly(sample_c4_anomaly(), enable_reasoning=True)

        self.assertTrue(res["reasoning_enabled"])
        self.assertEqual(res["priority_band"], "high")
        self.assertIn("transport_manager", res["personas"])
        self.assertIn("facilities_head", res["personas"])

        # verify reasoning trace steps
        trace_steps = [s["step"] for s in res["reasoning_trace"]]
        self.assertIn("1_SENSE_ANOMALY", trace_steps)
        self.assertIn("2_DRIVER_ATTRIBUTION", trace_steps)
        self.assertIn("3_OPERATIONAL_DRILLDOWN", trace_steps)
        self.assertIn("4_PERSONA_ROUTING", trace_steps)

        # verify action draft evidence
        action = res["action_draft"]
        self.assertEqual(action["type"], "vendor_escalation_email")
        self.assertEqual(action["recipient"], "Pooja Mikhailov Travel")
        self.assertIn("Pooja Mikhailov Travel", action["subject"])
        if action.get("groq_generated") or action.get("grok_generated"):
            self.assertTrue(len(action["body"]) > 0)
        else:
            self.assertIn("Pooja Mikhailov Travel' contributed 67.3%", action["body"])
        self.assertEqual(action["status"], "PROPOSED_WAITING_APPROVAL")

    def test_sample_c4_payload_reasoning_disabled(self):
        if not self.db_available:
            self.skipTest("mobility.duckdb not available")

        res = self.agent.process_anomaly(sample_c4_anomaly(), enable_reasoning=False)

        self.assertFalse(res["reasoning_enabled"])
        self.assertEqual(res["priority_band"], "high")

        # verify reasoning trace bypassed
        trace_steps = [s["step"] for s in res["reasoning_trace"]]
        self.assertIn("SENSE_PASS_THROUGH", trace_steps)
        self.assertNotIn("2_DRIVER_ATTRIBUTION", trace_steps)

        # verify action draft created directly (Sense + Act)
        action = res["action_draft"]
        self.assertEqual(action["type"], "vendor_escalation_email")
        self.assertEqual(action["status"], "PROPOSED_WAITING_APPROVAL")

    def test_weekly_grain_drilldown_matches_data(self):
        """Fix Finding 1: verify weekly period query parses grain correctly without UNKNOWN."""
        if not self.db_available:
            self.skipTest("mobility.duckdb not available")

        payload = sample_c4_anomaly()
        payload["context"]["period"] = "2026-W29"
        payload["context"]["grain"] = "week"

        res = self.agent.process_anomaly(payload, enable_reasoning=True)
        drilldown_step = [s for s in res["reasoning_trace"] if s["step"] == "3_OPERATIONAL_DRILLDOWN"][0]

        self.assertNotIn("UNKNOWN", drilldown_step["detail"])
        self.assertIn("delayed trips", drilldown_step["detail"])

    def test_healthy_payload_bypasses_escalation(self):
        """Fix Finding 3: verify payload with is_anomaly=False generates NO_ACTION_NEEDED."""
        payload = sample_c4_anomaly()
        payload["is_anomaly"] = False

        res = self.agent.process_anomaly(payload)

        self.assertEqual(res["status"], "NO_ACTION_NEEDED")
        self.assertEqual(res["action_draft"]["type"], "no_action_required")
        self.assertIn("[HEALTHY]", res["executive_summary"])

    def test_invalid_payload_raises_value_error(self):
        """Fix Finding 3: verify invalid payloads raise ValueError."""
        with self.assertRaises(ValueError):
            self.agent.process_anomaly({"invalid": "payload"})

    def test_process_query(self):
        if not self.db_available:
            self.skipTest("mobility.duckdb not available")

        res = self.agent.process_query("Why did OTA drop for pinnacle-Slc?", tenant_id="pinnacle-Slc")

        self.assertEqual(res["kpi"], "ota")
        self.assertIn("On-time arrival", res["answer"])
        self.assertIn("mobility.duckdb", res["citation"])
        self.assertEqual(len(res["reasoning_trace"]), 3)


if __name__ == "__main__":
    unittest.main()
