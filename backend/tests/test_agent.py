import os
import unittest

from backend.agent import AgentConfigurationError, GroqReasoningClient, OtaReasoningAgent
from backend import config as C


class FakeClient:
    model = "fake-model"

    def __init__(self):
        self.messages = None

    def complete_json(self, messages):
        self.messages = messages
        if "results array" in messages[1]["content"]:
            return {
                "results": [
                    {
                        "insight_id": "ota|month|2026-07|tenant_id=pinnacle-Slc",
                        "status": "anomaly",
                        "reasoning_summary": "First vendor is below SLA.",
                        "narrative": "The first OTA alert needs review.",
                        "recommended_next_step": "Check the first vendor dispatch sample.",
                        "evidence_used": ["sla_breach"],
                        "confidence_note": "Based only on deterministic C3/C4 evidence.",
                    },
                    {
                        "insight_id": "ota|month|2026-07|tenant_id=pinnacle-Slc|vendor=second",
                        "status": "anomaly",
                        "reasoning_summary": "Second vendor is below SLA.",
                        "narrative": "The second OTA alert needs review.",
                        "recommended_next_step": "Check the second vendor dispatch sample.",
                        "evidence_used": ["sla_breach"],
                        "confidence_note": "Based only on deterministic C3/C4 evidence.",
                    },
                ]
            }
        return {
            "insight_id": "from-model",
            "status": "anomaly",
            "reasoning_summary": "OTA is below SLA and worsening.",
            "narrative": "The OTA signal needs operational review.",
            "recommended_next_step": "Review vendor and shift dispatch performance.",
            "evidence_used": ["sla_breach", "adverse_trend"],
            "confidence_note": "Based only on deterministic C3/C4 evidence.",
        }


def ota_insight():
    return {
        "insight_id": "ota|month|2026-07|tenant_id=pinnacle-Slc",
        "kpi": "ota",
        "anomaly_type": "ota_degradation",
        "is_anomaly": True,
        "priority_score": 72.6,
        "priority_band": "high",
        "signals": [{"name": "sla_breach", "bad": True}],
        "context": {
            "filters": {"tenant_id": "pinnacle-Slc"},
            "grain": "month",
            "period": "2026-07",
            "headline": "On-time arrival 84%; 6pts below SLA.",
            "value": 84.0,
            "unit": "%",
            "n": 500,
            "trend": {"last_period": 91.0, "delta": -7.0, "direction": "down", "improving": False},
            "sla": {"target": 90.0, "gap_pts": -6.0, "breached": True},
            "peer": {"percentile": 10},
            "industry": {"better_than_norm": False},
            "drivers_of_change": [],
        },
    }


class AgentTests(unittest.TestCase):
    def test_reasoning_agent_returns_normalized_output(self):
        client = FakeClient()
        agent = OtaReasoningAgent(client)

        result = agent.reason(ota_insight())

        self.assertEqual(result["agent"], "ota_reasoning")
        self.assertEqual(result["model"], "fake-model")
        self.assertEqual(result["source"], "groq")
        self.assertEqual(result["evidence_used"], ["sla_breach", "adverse_trend"])
        self.assertIn("EVIDENCE", client.messages[1]["content"])

    def test_reasoning_agent_returns_one_result_per_insight(self):
        client = FakeClient()
        agent = OtaReasoningAgent(client)
        first = ota_insight()
        second = ota_insight()
        second["insight_id"] = "ota|month|2026-07|tenant_id=pinnacle-Slc|vendor=second"

        result = agent.reason_many([first, second])

        self.assertEqual(result["agent"], "ota_reasoning")
        self.assertEqual(result["total_alerts"], 2)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["c4_insight"]["insight_id"], first["insight_id"])
        self.assertEqual(result["results"][1]["c4_insight"]["insight_id"], second["insight_id"])
        self.assertIn("EVIDENCE", client.messages[1]["content"])

    def test_rejects_non_ota_insight_for_now(self):
        agent = OtaReasoningAgent(FakeClient())
        insight = ota_insight()
        insight["kpi"] = "safety_score"

        with self.assertRaises(ValueError):
            agent.reason(insight)

    def test_groq_client_requires_api_key(self):
        old_env = os.environ.pop(C.GROQ_API_KEY_ENV, None)
        old_config_key = C.GROQ_API_KEY
        C.GROQ_API_KEY = ""
        client = GroqReasoningClient(api_key="")

        try:
            with self.assertRaises(AgentConfigurationError):
                client.complete_json([])
        finally:
            C.GROQ_API_KEY = old_config_key
            if old_env is not None:
                os.environ[C.GROQ_API_KEY_ENV] = old_env


if __name__ == "__main__":
    unittest.main()
