"""C5 — Focused OTA reasoning layer.

This is intentionally narrow for the current build. C2/C3/C4 produce all
numbers and anomaly decisions; this layer asks an LLM only to turn C4 OTA
insights into concise operational explanations and next-step recommendations.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional

from . import config as C


class AgentConfigurationError(RuntimeError):
    """Raised when the LLM client is not configured."""


class AgentProviderError(RuntimeError):
    """Raised when the LLM provider call fails or returns malformed data."""


class GroqReasoningClient:
    """Tiny OpenAI-compatible Groq chat-completions client."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or os.environ.get(C.GROQ_API_KEY_ENV) or C.GROQ_API_KEY
        self.model = model or C.GROQ_REASONING_MODEL
        self.base_url = C.GROQ_API_BASE_URL.rstrip("/")

    def complete_json(self, messages: list[dict]) -> dict:
        if not self.api_key:
            raise AgentConfigurationError(
                f"Set {C.GROQ_API_KEY_ENV} or backend.config.GROQ_API_KEY "
                "before calling the reasoning agent."
            )

        body = {
            "model": self.model,
            "messages": messages,
            "temperature": C.GROQ_TEMPERATURE,
            "max_tokens": C.GROQ_MAX_TOKENS,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
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
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AgentProviderError(f"Groq API error {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise AgentProviderError(f"Groq API request failed: {exc}") from exc

        try:
            content = payload["choices"][0]["message"]["content"]
            return json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise AgentProviderError("Groq response did not contain valid JSON content.") from exc


class OtaReasoningAgent:
    """Reason over C4 OTA output without recomputing metrics."""

    def __init__(self, llm_client: Optional[GroqReasoningClient] = None):
        self.llm_client = llm_client or GroqReasoningClient()

    def reason(self, insight: dict) -> dict:
        if insight.get("kpi") != "ota":
            raise ValueError("The current reasoning layer only supports OTA insights.")

        evidence = self._evidence(insight)
        messages = [
            {
                "role": "system",
                "content": (
                    "You are MoveInsight's OTA triage reasoning layer. "
                    "Use only the supplied deterministic C3/C4 evidence. "
                    "Do not invent numbers, causes, recipients, or actions. "
                    "Return concise JSON only."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Generate a concise operational interpretation for this OTA insight. "
                    "Return JSON with keys: insight_id, status, reasoning_summary, "
                    "narrative, recommended_next_step, evidence_used, confidence_note. "
                    "The recommended_next_step should be modest and investigation-oriented.\n\n"
                    f"EVIDENCE:\n{json.dumps(evidence, indent=2)}"
                ),
            },
        ]
        result = self.llm_client.complete_json(messages)
        return self._normalize(result, insight)

    def reason_many(self, insights: list[dict]) -> dict:
        """Return one reasoning result for each C4 OTA insight."""
        if not insights:
            return {
                "agent": "ota_reasoning",
                "model": getattr(self.llm_client, "model", None),
                "status": "not_anomaly",
                "total_alerts": 0,
                "results": [],
                "source": "deterministic_fallback",
            }
        for insight in insights:
            if insight.get("kpi") != "ota":
                raise ValueError("The current reasoning layer only supports OTA insights.")

        evidence = [self._evidence(insight) for insight in insights]
        messages = [
            {
                "role": "system",
                "content": (
                    "You are MoveInsight's OTA triage reasoning layer. "
                    "Use only the supplied deterministic C3/C4 evidence. "
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
                    "recommended_next_step, evidence_used, confidence_note. "
                    "Keep recommended_next_step modest and investigation-oriented.\n\n"
                    f"EVIDENCE:\n{json.dumps(evidence, indent=2)}"
                ),
            },
        ]
        result = self.llm_client.complete_json(messages)
        normalized = self._normalize_many(result, insights)
        return {
            "agent": "ota_reasoning",
            "model": getattr(self.llm_client, "model", None),
            "status": "anomaly" if normalized else "not_anomaly",
            "total_alerts": len(insights),
            "results": normalized,
            "source": "groq",
        }

    def _evidence(self, insight: dict) -> dict:
        ctx = insight.get("context") or {}
        group = insight.get("group") or {}
        overall = insight.get("overall") or {}
        return {
            "insight_id": insight.get("insight_id"),
            "is_anomaly": insight.get("is_anomaly"),
            "priority_score": insight.get("priority_score"),
            "priority_band": insight.get("priority_band"),
            "anomaly_type": insight.get("anomaly_type"),
            "signals": insight.get("signals", []),
            "tenant_id": insight.get("tenant_id") or ctx.get("tenant_id"),
            "grain": insight.get("grain") or ctx.get("grain"),
            "period": insight.get("period") or ctx.get("period") or ctx.get("month"),
            "overall": overall,
            "group": group,
            "value": group.get("value") or ctx.get("value"),
            "unit": "%",
            "sample_size": group.get("n") or ctx.get("n"),
            "trend": {
                "last_period": (ctx.get("trend") or {}).get("last_period"),
                "delta": (ctx.get("trend") or {}).get("delta"),
                "direction": (ctx.get("trend") or {}).get("direction"),
                "improving": (ctx.get("trend") or {}).get("improving"),
            },
            "sla": {
                "target": overall.get("sla"),
                "gap_pts": group.get("sla_gap_pts"),
                "breached": group.get("breached"),
            } if group else ctx.get("sla"),
            "peer": ctx.get("peer"),
            "industry": ctx.get("industry"),
            "drivers_of_change": ctx.get("drivers_of_change", []),
        }

    def _normalize(self, result: dict, insight: dict) -> dict:
        return {
            "insight_id": result.get("insight_id") or insight.get("insight_id"),
            "agent": "ota_reasoning",
            "model": getattr(self.llm_client, "model", None),
            "status": result.get("status") or ("anomaly" if insight.get("is_anomaly") else "not_anomaly"),
            "reasoning_summary": result.get("reasoning_summary", ""),
            "narrative": result.get("narrative", ""),
            "recommended_next_step": result.get("recommended_next_step", ""),
            "evidence_used": result.get("evidence_used", []),
            "confidence_note": result.get("confidence_note", ""),
            "source": "groq",
        }

    def _normalize_many(self, result: dict, insights: list[dict]) -> list[dict]:
        raw_results = result.get("results")
        if not isinstance(raw_results, list):
            raw_results = []

        by_id = {
            item.get("insight_id"): item
            for item in raw_results
            if isinstance(item, dict) and item.get("insight_id")
        }
        normalized = []
        for index, insight in enumerate(insights):
            model_result = by_id.get(insight.get("insight_id"))
            if model_result is None and index < len(raw_results) and isinstance(raw_results[index], dict):
                model_result = raw_results[index]
            normalized_item = self._normalize(model_result or {}, insight)
            normalized_item["c4_insight"] = insight
            normalized.append(normalized_item)
        return normalized
