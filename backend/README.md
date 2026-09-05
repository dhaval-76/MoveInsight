# C1 + C2 + C3 — Ingestion, Metrics & Context Layer

Deterministic foundation for the Agentic Intelligence & Reporting Layer.
See `../HLD_v2.md` for the full design. **All math lives here; the LLM never
computes a KPI.**

## Layout

```
backend/
  config.py    SLA targets, emission factors, thresholds, file names + METRIC_REGISTRY (the tunable knobs)
  ingest.py    C1 — load 7 CSVs -> clean canonical DuckDB (ID/date/cost/severity normalization)
  metrics.py   C2 — whitelisted KPI functions (safe query interface for the agent + dashboard)
  context.py   C3 — benchmarking/context engine (trend + SLA + peer + industry + drivers)
```

## Setup

```bash
python3 -m venv ./backend/.venv
source ./backend/.venv/bin/activate
python -m pip install --upgrade pip
pip install -r backend/requirements.txt
```

## Build the database (from the repo root, where the CSVs live)

```bash
python -m backend.ingest
# -> backend/mobility.duckdb  (built in ~4s over ~3M rows)
```

## Run the context API (FastAPI)

```bash
source backend/.venv/bin/activate
uvicorn backend.api:app --reload
```

Interactive docs at `http://127.0.0.1:8000/docs`.

> **DuckDB write-lock:** the API can read the database only when no other
> application holds a write lock. Close the `mobility.duckdb` connection in
> DBeaver / the DuckDB CLI before starting Uvicorn. If the lock error mentions a
> stale client, fully quit that client and start Uvicorn again.

Primary endpoint — `POST /context`:

```json
{
  "method": "ota",
  "filters": {"tenant_id": "pinnacle-Slc"},
  "month": "2026-07"
}
```

The response is the full context object produced by `ContextEngine` — value,
sample size, trend, SLA, peer comparison, industry norm, drivers of change,
assessment, and headline.

## Use the metrics layer

```python
from backend.metrics import Metrics
m = Metrics("backend/mobility.duckdb")

m.ota()                                   # overall on-time arrival
m.ota({"vendor": "Pooja Mikhailov Travel"})
m.ota({"tenant_id": "pinnacle-Slc"}, month="2026-07")
m.sla_gap({"tenant_id": "vanta-Sea"})     # signed pts vs SLA
m.ota_by_vendor(tenant_id="pinnacle-Slc") # volume-normalized peer ranking (feeds C3)
m.ota_trend()                             # month-over-month (feeds C3 trend context)
m.data_health("pinnacle-Slc")            # graceful-degradation panel
```

## Use the context engine (C3)

Wraps any KPI in benchmarking context — the "so what" layer that feeds C4/C6.

```python
from backend.context import ContextEngine
c = ContextEngine("backend/mobility.duckdb")

ctx = c.context("ota", {"tenant_id": "pinnacle-Slc"}, month="2026-07")
ctx["headline"]      # pre-composed, numbers-only sentence (zero hallucination risk)
ctx["assessment"]    # "|"-joined flags: sla_breached|declining|bottom_quartile_peer|below_industry_norm, or "healthy"

# Configurable time grain — month (default) | week | day.
# Weekly gives ~13 trend points from the 3-month dataset vs 3 monthly.
c.context("ota", {"tenant_id": "pinnacle-Slc"}, period="2026-W29", grain="week")
```

Trend buckets are derived from the data (`Metrics.periods(grain)`), not hard-coded,
so the axis adapts to the dataset and the chosen grain.
Each context object bundles **4 reference points** around the raw value:

1. `trend` — month-over-month series, moving avg, polarity-aware `improving` flag
2. `sla` — signed `gap_pts` vs target + `breached`
3. `peer` — volume-normalized rank/percentile + best/median/worst spread
4. `industry`— delta vs configured industry norm

Plus `drivers_of_change[]` — weighted attribution of which vendors/offices move the
number, ranked by `contribution_pct`. See `./backend/sample_context.json` for full examples
(this is the locked contract for `GET /api/context/{kpi}`).

## Canonical tables

`trips`, `employees`, `bills`, `feedback`, `alerts`, `vendors`, `data_quality`.
Join key across all: normalized `trip_id`.

## KPIs available

`ota`, `sla_gap`, `noshow_rate`, `cost_per_trip`, `occupancy`, `co2_per_trip`,
`safety_score`, `escort_compliance`, `feedback_score`.

## Messy-data handling (validated against the real dataset)

- **IDs** with commas/quotes (`"1,516,906"`) normalized to canonical digits — restores 99.9–100% join coverage.
- **Epochs** are IST wall-clock -> converted to UTC.
- **Cost** negatives (to -2.2M) and extremes (to 104k) quarantined + flagged (`cost_quarantined`), counted in `data_quality`.
- **Alert severity** junk (`NA`, `False`) mapped to NULL + `severity_unknown` flag.
- **Partial coverage** (feedback ~49%, alerts ~5.5%) handled with LEFT joins.

## Safety

The metrics layer only exposes **named, parameterized functions** with a
whitelisted set of filter dimensions (`ALLOWED_DIMS`). Illegal dimensions raise
`ValueError`. No free-form SQL is ever accepted from the model.

## Notes for the other workstreams

- **C3 (benchmarking)** is built — `context.py` consumes the `Metrics` helpers (`distinct`, `kpi_by_group`, trends) to produce the context object per KPI.
- **C4 (detection/ranking)** consumes C3 context objects; ranks by business impact.
- **C8 (dashboard/chat)** reads the same `Metrics`/`ContextEngine` functions — one source of truth.
