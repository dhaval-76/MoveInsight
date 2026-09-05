"""Final c5-agent grouped OTA reasoning contract."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

from . import config as C


class AgentConfigurationError(RuntimeError):
    """Raised when the reasoning provider is not configured."""


class AgentProviderError(RuntimeError):
    """Raised when the reasoning provider fails or returns invalid JSON."""


class GroqReasoningClient:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or C.GROQ_API_KEY
        self.model = model or C.GROQ_REASONING_MODEL
        self.base_url = C.GROQ_API_BASE_URL.rstrip("/")

    def complete_json(self, messages: list[dict]) -> dict:
        if not self.api_key:
            raise AgentConfigurationError("GROQ_API_KEY is required for OTA reasoning")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps({
                "model": self.model,
                "messages": messages,
                "temperature": C.GROQ_TEMPERATURE,
                "max_tokens": C.GROQ_MAX_TOKENS,
                "response_format": {"type": "json_object"},
            }).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "MoveInsight/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=C.GROQ_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise AgentProviderError(f"Groq API error {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise AgentProviderError(f"Groq API request failed: {exc}") from exc
        try:
            return json.loads(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise AgentProviderError("Groq response did not contain valid JSON") from exc


class OtaReasoningAgent:
    """Reason over deterministic grouped C4 OTA output without recomputing it."""

    def __init__(self, llm_client: Optional[GroqReasoningClient] = None):
        self.llm_client = llm_client or GroqReasoningClient()

    def reason_many(self, insights: list[dict]) -> dict:
        if not insights:
            return {
                "agent": "ota_reasoning",
                "model": self.llm_client.model,
                "status": "not_anomaly",
                "total_alerts": 0,
                "results": [],
                "source": "deterministic_fallback",
            }
        if any(insight.get("kpi") != "ota" for insight in insights):
            raise ValueError("The OTA reasoning agent only supports OTA insights")
        evidence = [self._evidence(insight) for insight in insights]
        messages = [
            {
                "role": "system",
                "content": (
                    "You are MoveInsight's OTA triage reasoning layer. "
                    "Use only supplied deterministic C3/C4 evidence. "
                    "Do not invent numbers, causes, recipients, or actions. "
                    "Return concise JSON only."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Generate one concise operational interpretation per OTA insight. "
                    "Return JSON with a results array. Each result must include: "
                    "insight_id, status, reasoning_summary, narrative, "
                    "recommended_next_step, evidence_used, confidence_note.\n\n"
                    f"EVIDENCE:\n{json.dumps(evidence, indent=2)}"
                ),
            },
        ]
        result = self.llm_client.complete_json(messages)
        normalized = self._normalize_many(result, insights)
        return {
            "agent": "ota_reasoning",
            "model": self.llm_client.model,
            "status": "anomaly" if normalized else "not_anomaly",
            "total_alerts": len(insights),
            "results": normalized,
            "source": "groq",
        }

    def _evidence(self, insight: dict) -> dict:
        context = insight.get("context") or {}
        group = insight.get("group") or {}
        return {
            "insight_id": insight.get("insight_id"),
            "is_anomaly": insight.get("is_anomaly"),
            "priority_score": insight.get("priority_score"),
            "priority_band": insight.get("priority_band"),
            "anomaly_type": insight.get("anomaly_type"),
            "signals": insight.get("signals", []),
            "tenant_id": insight.get("tenant_id") or (context.get("filters") or {}).get("tenant_id"),
            "grain": insight.get("grain") or context.get("grain"),
            "period": insight.get("period") or context.get("period"),
            "group": group,
            "value": group.get("value") or context.get("value"),
            "unit": "%",
            "sample_size": group.get("n") or context.get("n"),
            "sla": {
                "target": (context.get("sla") or {}).get("target"),
                "gap_pts": group.get("sla_gap_pts"),
                "breached": group.get("breached"),
            },
            "confidence": insight.get("confidence"),
        }

    def _normalize_many(self, result: dict, insights: list[dict]) -> list[dict]:
        raw_results = result.get("results") if isinstance(result, dict) else []
        raw_results = raw_results if isinstance(raw_results, list) else []
        by_id = {
            item.get("insight_id"): item for item in raw_results
            if isinstance(item, dict) and item.get("insight_id")
        }
        normalized = []
        for index, insight in enumerate(insights):
            item = by_id.get(insight.get("insight_id"))
            if item is None and index < len(raw_results) and isinstance(raw_results[index], dict):
                item = raw_results[index]
            item = item or {}
            normalized.append({
                "insight_id": item.get("insight_id") or insight.get("insight_id"),
                "agent": "ota_reasoning",
                "model": self.llm_client.model,
                "status": item.get("status") or "anomaly",
                "reasoning_summary": item.get("reasoning_summary", ""),
                "narrative": item.get("narrative", ""),
                "recommended_next_step": item.get("recommended_next_step", ""),
                "evidence_used": item.get("evidence_used", []),
                "confidence_note": item.get("confidence_note", ""),
                "source": "groq",
                "c4_insight": insight,
            })
        return normalized
