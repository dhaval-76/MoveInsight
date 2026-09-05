"""HTTP API for the MoveInsight context engine."""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .context import ContextEngine
from .metrics import Metrics


class ContextRequest(BaseModel):
    """Scope used to build one KPI context response."""

    method: str = Field(min_length=1, description="Registered KPI method, for example ota")
    filters: dict[str, str] = Field(default_factory=dict)
    month: str = Field(pattern=r"^\d{4}-\d{2}$")


app = FastAPI(title="MoveInsight Context API", version="1.0.0")
_metrics = Metrics(str(Path(__file__).with_name("mobility.duckdb")))
_context_engine = ContextEngine(_metrics)


@app.post("/context")
def get_context(request: ContextRequest) -> dict:
    """Return the full context object for a KPI and filter scope."""
    try:
        return _context_engine.context(
            request.method,
            request.filters,
            request.month,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc