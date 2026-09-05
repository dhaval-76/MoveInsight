import unittest

from backend.ota_agent import (
    AgentProviderError,
    GroqReasoningClient,
    OtaReasoningAgent,
)
from backend.tests.test_agent import sample_c4_anomaly


class FailingClient:
    model = "openai/gpt-oss-20b"

    def complete_json(self, messages):
        raise AgentProviderError("Groq API error 400: json_validate_failed")


class JsonClient:
    model = "openai/gpt-oss-20b"

    def complete_json(self, messages):
        return {
            "results": [
                {
                    "insight_id": sample_c4_anomaly()["insight_id"],
                    "status": "anomaly",
                    "reasoning_summary": "SLA miss is concentrated in one vendor.",
                    "narrative": "OTA is materially below SLA.",
                    "recommended_next_step": "Ask the vendor for a recovery plan.",
                    "evidence_used": ["53.27 pts below target"],
                    "confidence_note": "Uses supplied C4 evidence only.",
                }
            ]
        }


class RetryClient(GroqReasoningClient):
    def __init__(self):
        super().__init__(api_key="test-key", model="openai/gpt-oss-20b")
        self.modes = []

    def _request_completion(self, messages, response_format):
        self.modes.append((response_format or {}).get("type"))
        if response_format and response_format.get("type") == "json_schema":
            raise AgentProviderError("Groq API error 400: json_validate_failed")
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"results": [{"insight_id": "x", "status": "anomaly"}]}'
                    }
                }
            ]
        }


class OtaReasoningAgentTests(unittest.TestCase):
    def test_schema_validation_failure_retries_with_json_object_mode(self):
        client = RetryClient()
        result = client.complete_json([])

        self.assertEqual(client.modes, ["json_schema", "json_object"])
        self.assertEqual(result["results"][0]["insight_id"], "x")

    def test_strict_schema_matches_groq_requirements(self):
        client = GroqReasoningClient(api_key="test-key", model="openai/gpt-oss-20b")
        response_format = client._strict_response_format()
        schema = response_format["json_schema"]["schema"]
        result_schema = schema["properties"]["results"]["items"]

        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(result_schema["additionalProperties"])
        self.assertEqual(set(result_schema["required"]), set(result_schema["properties"].keys()))

    def test_provider_failure_returns_deterministic_fallback(self):
        agent = OtaReasoningAgent(llm_client=FailingClient())
        result = agent.reason_many([sample_c4_anomaly()])

        self.assertEqual(result["source"], "deterministic_fallback")
        self.assertEqual(result["status"], "anomaly")
        self.assertEqual(result["results"][0]["source"], "deterministic_fallback")
        self.assertIn("Groq reasoning was unavailable", result["results"][0]["confidence_note"])

    def test_provider_json_is_normalized(self):
        agent = OtaReasoningAgent(llm_client=JsonClient())
        result = agent.reason_many([sample_c4_anomaly()])

        self.assertEqual(result["source"], "groq")
        self.assertEqual(result["results"][0]["source"], "groq")
        self.assertEqual(result["results"][0]["recommended_next_step"], "Ask the vendor for a recovery plan.")


if __name__ == "__main__":
    unittest.main()
