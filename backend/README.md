# C1 + C2 — Data Ingestion & Metrics Layer

Deterministic foundation for the Agentic Intelligence & Reporting Layer.
See `../HLD_v2.md` for the full design. **All math lives here; the LLM never
computes a KPI.**

## Layout
```
backend/
  config.py    SLA targets, emission factors, thresholds, file names (the tunable knobs)
  ingest.py    C1 — load 7 CSVs -> clean canonical DuckDB (ID/date/cost/severity normalization)
  metrics.py   C2 — whitelisted KPI functions (safe query interface for the agent + dashboard)
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
- **C3 (benchmarking)** consumes `ota_by_vendor`, `ota_trend`, and per-filter KPI values to build the context object.
- **C8 (dashboard/chat)** reads the same `Metrics` functions — one source of truth.
