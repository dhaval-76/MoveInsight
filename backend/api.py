"""HTTP API for the MoveInsight context, insights, and agent orchestration engine."""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .agent import AgentOrchestrator
from .alerts import AlertStore
from . import config as C
from .context import ContextEngine
from .insights import InsightEngine
from .metrics import Metrics
from .ota_agent import AgentConfigurationError, AgentProviderError, OtaReasoningAgent


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


class AlertRunRequest(BaseModel):
    """Tenant-wide daily alert pipeline options."""

    period: str = Field(min_length=1, description="Completed period, for example 2026-07")
    grain: Literal["month", "week", "day"] = "month"
    enable_reasoning: Optional[bool] = None


class AlertStatusRequest(BaseModel):
    """Allowed dashboard workflow status transition."""

    status: Literal["new", "acknowledged", "resolved", "dismissed"]


app = FastAPI(title="MoveInsight Context, Insights & Agent API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:4173",
        "http://localhost:4174",
        "http://localhost:4175",
        "http://127.0.0.1:4173",
        "http://127.0.0.1:4174",
        "http://127.0.0.1:4175",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

_db_path = str(Path(__file__).with_name("mobility.duckdb"))
_metrics = Metrics(_db_path)
_context_engine = ContextEngine(_metrics)
_insight_engine = InsightEngine(_context_engine)
_agent_orchestrator = AgentOrchestrator(_context_engine, _insight_engine)
_alert_store = AlertStore(_db_path)
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


@app.post("/agent/ota/reason")
def reason_about_ota(request: ContextRequest) -> dict:
    """Run grouped C3/C4 OTA analysis and process each result through C5."""
    if request.method != "ota":
        raise HTTPException(status_code=400, detail="This endpoint requires method='ota'.")
    try:
        context = _context_engine.context(
            request.method,
            request.filters,
            request.period,
            request.grain,
        )
        insights = _insight_engine.evaluate_context(context)
        if not isinstance(insights, list):
            insights = [insights] if insights.get("is_anomaly") else []
        reasoning = _ota_reasoning_agent.reason_many(insights)
        return {
            "agent": "ota_reasoning",
            "status": reasoning["status"],
            "period": context.get("period"),
            "grain": context.get("grain"),
            "scope": context.get("scope") or request.filters or {},
            "c4_alerts": insights,
            "total_alerts": reasoning["total_alerts"],
            "results": reasoning["results"],
            "source": reasoning["source"],
        }
    except (AgentConfigurationError, AgentProviderError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
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


@app.get("/alerts")
def list_alerts(tenant_id: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
    """Return persisted C5 alerts for the shared dashboard."""
    return _alert_store.list_alerts(tenant_id=tenant_id, status=status)


@app.get("/alerts/periods")
def list_alert_periods(grain: Literal["month", "week", "day"] = "month") -> dict:
    """Return available completed periods for the requested grain."""
    periods = _metrics.periods(grain=grain)
    return {
        "grain": grain,
        "periods": periods,
        "latest_period": periods[-1] if periods else None,
    }


@app.patch("/alerts/{alert_id}/status")
def update_alert_status(alert_id: str, request: AlertStatusRequest) -> dict:
    """Update the workflow status of one persisted alert."""
    try:
        return _alert_store.update_status(alert_id, request.status)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/alerts/run")
def run_alert_pipeline(request: AlertRunRequest) -> dict:
    """Run C3 -> C4 -> C5 for every tenant aggregate and persist anomalies."""
    run_id, _ = _alert_store.start_run(request.period, request.grain)
    try:
        reasoning_enabled = (
            C.ENABLE_REASONING
            if request.enable_reasoning is None
            else request.enable_reasoning
        )
        anomalies = _insight_engine.scan_period(
            request.period,
            grain=request.grain,
            tenant_id=None,
            dimensions=[],
        )
        ota_anomalies = [anomaly for anomaly in anomalies if anomaly.get("kpi") == "ota"]
        ota_reasoning = None
        if ota_anomalies and reasoning_enabled:
            try:
                ota_reasoning = _ota_reasoning_agent.reason_many(ota_anomalies)
            except (AgentConfigurationError, AgentProviderError) as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        saved = []
        for anomaly in anomalies:
            result = _agent_orchestrator.process_anomaly(
                anomaly,
                enable_reasoning=(
                    False if ota_reasoning and anomaly.get("kpi") == "ota"
                    else reasoning_enabled
                ),
            )
            if ota_reasoning:
                result["ota_reasoning"] = next(
                    (item for item in ota_reasoning["results"] if item["insight_id"] == anomaly.get("insight_id")),
                    None,
                )
            saved.append(_alert_store.save_alert(run_id, anomaly, result))

        summary = {"alerts_saved": len(saved), "tenants": sorted({item["tenant_id"] for item in saved})}
        _alert_store.finish_run(run_id, "completed", summary)
        return {"run_id": run_id, **summary, "alerts": saved}
    except HTTPException:
        _alert_store.finish_run(run_id, "failed", {"error": "C5 provider request failed"})
        raise
    except Exception as exc:
        _alert_store.finish_run(run_id, "failed", {"error": str(exc)})
        raise HTTPException(status_code=500, detail=str(exc)) from exc
