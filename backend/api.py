"""HTTP API for the MoveInsight context engine."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .agent import AgentConfigurationError, AgentProviderError, OtaReasoningAgent
from .context import ContextEngine
from .insights import InsightEngine
from .metrics import Metrics


class ContextRequest(BaseModel):
    """KPI scope and time grain used to build one context response."""

    method: str = Field(min_length=1, description="Registered KPI method, for example ota")
    filters: Optional[dict[str, str]] = None
    period: Optional[str] = Field(
        default=None,
        description="Focus bucket: YYYY-MM, YYYY-Www, or YYYY-MM-DD",
    )
    grain: Literal["month", "week", "day"] = "month"


class InsightScanRequest(BaseModel):
    """Scope and options for a ranked C4 insight scan."""

    period: str = Field(min_length=1, description="Focus bucket: YYYY-MM, YYYY-Www, or YYYY-MM-DD")
    grain: Literal["month", "week", "day"] = "month"
    tenant_id: Optional[str] = None
    kpis: Optional[list[str]] = None
    dimensions: Optional[list[str]] = None
    include_global: bool = False


app = FastAPI(title="MoveInsight Context API", version="1.0.0")
_metrics = Metrics(str(Path(__file__).with_name("mobility.duckdb")))
_context_engine = ContextEngine(_metrics)
_insight_engine = InsightEngine(_context_engine)
_ota_reasoning_agent = OtaReasoningAgent()


@app.post("/context")
def get_context(request: ContextRequest) -> dict:
    """Return the full context object for a KPI and filter scope."""
    try:
        return _context_engine.context(
            request.method,
            request.filters,
            request.period,
            request.grain,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/insights/evaluate")
def evaluate_insight(request: ContextRequest):
    """Build and classify one C3 context object with C4 rules."""
    try:
        context = _context_engine.context(
            request.method,
            request.filters,
            request.period,
            request.grain,
        )
        return _insight_engine.evaluate_context(context)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/insights/scan")
def scan_insights(request: InsightScanRequest) -> list[dict]:
    """Return ranked C4 anomalies for a period and optional scope."""
    try:
        return _insight_engine.scan_period(
            period=request.period,
            grain=request.grain,
            tenant_id=request.tenant_id,
            kpis=request.kpis,
            dimensions=request.dimensions,
            include_global=request.include_global,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/agent/ota/reason")
def reason_about_ota(request: ContextRequest) -> dict:
    """Build C3/C4 OTA evidence and ask C5 to produce a concise interpretation."""
    try:
        if request.method != "ota":
            raise ValueError("The current reasoning endpoint only supports method='ota'.")
        context = _context_engine.context(
            request.method,
            request.filters,
            request.period,
            request.grain,
        )
        print("C3 OTA benchmark:")
        print(json.dumps(context, indent=2))
        insight = _insight_engine.evaluate_context(context)
        print("C4 OTA insights:")
        print(json.dumps(insight, indent=2))
        if not insight:
            return {
                "agent": "ota_reasoning",
                "status": "not_anomaly",
                "reasoning_summary": "No OTA SLA breach was detected for this scope.",
                "narrative": "C4 did not find any OTA group breaching SLA in this scope.",
                "recommended_next_step": "No escalation needed from this signal alone.",
                "evidence_used": [],
                "confidence_note": "Based on deterministic C3 grouped OTA benchmark and C4 rules.",
                "source": "deterministic_fallback",
            }
        if isinstance(insight, list):
            reasoned = _ota_reasoning_agent.reason_many(insight)
            reasoned["period"] = context.get("period")
            reasoned["grain"] = context.get("grain")
            reasoned["scope"] = context.get("scope")
            reasoned["c4_alerts"] = insight
            return reasoned
        return _ota_reasoning_agent.reason(insight)
    except AgentConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AgentProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
