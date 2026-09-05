"""HTTP API for the MoveInsight context, insights, and agent orchestration engine."""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .agent import AgentOrchestrator
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


class ProcessAnomalyRequest(BaseModel):
    """Payload for processing a C4 anomaly insight into an AgentResponse."""

    anomaly: dict = Field(..., description="C4 insight anomaly object")
    enable_reasoning: Optional[bool] = Field(
        default=None,
        description="Override reasoning mode (True: Sense+Reason+Act, False: Sense+Act)",
    )


class ScanAgentRequest(BaseModel):
    """Request to scan a period and process all anomalies via C5."""

    period: str = Field(..., description="Target period e.g. 2026-07")
    grain: Literal["month", "week", "day"] = "month"
    tenant_id: Optional[str] = None
    enable_reasoning: Optional[bool] = None


class AgentQueryRequest(BaseModel):
    """Natural language query request."""

    query: str = Field(min_length=1, description="User question e.g. Why did OTA drop?")
    tenant_id: Optional[str] = None
    period: Optional[str] = Field(default=None, description="Focus period e.g. 2026-07 or 2026-W29")
    month: str = Field(default="2026-07", description="Target month YYYY-MM")
    grain: Literal["month", "week", "day"] = "month"


app = FastAPI(title="MoveInsight Context, Insights & Agent API", version="1.0.0")

_db_path = str(Path(__file__).with_name("mobility.duckdb"))
_metrics = Metrics(_db_path)
_context_engine = ContextEngine(_metrics)
_insight_engine = InsightEngine(_context_engine)
_agent_orchestrator = AgentOrchestrator(_context_engine, _insight_engine)


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
def evaluate_insight(request: ContextRequest) -> dict:
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


@app.post("/agent/process-anomaly")
def process_anomaly(request: ProcessAnomalyRequest) -> dict:
    """Process a C4 anomaly insight into an action payload and reasoning trace.

    Groq API Key is loaded automatically from server environment / .env file.
    """
    try:
        return _agent_orchestrator.process_anomaly(
            request.anomaly,
            enable_reasoning=request.enable_reasoning,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/agent/scan")
def scan_and_process(request: ScanAgentRequest) -> list[dict]:
    """Scan a period for anomalies via C4 and return agent responses for each."""
    try:
        return _agent_orchestrator.scan_and_process(
            request.period,
            grain=request.grain,
            tenant_id=request.tenant_id,
            enable_reasoning=request.enable_reasoning,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/agent/query")
def process_query(request: AgentQueryRequest) -> dict:
    """Process a natural language user query and return cited answer with reasoning trace."""
    try:
        return _agent_orchestrator.process_query(
            request.query,
            tenant_id=request.tenant_id,
            period=request.period or request.month,
            grain=request.grain,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
