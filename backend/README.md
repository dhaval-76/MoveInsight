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
  context.py   C3 — OTA benchmark context with overall + grouped vendor/office facts
  insights.py  C4 — deterministic OTA SLA-breach detection over C3 groups
  agent.py     C5 — focused OTA reasoning using Groq over C4 evidence
```

## Setup

```bash
python3 -m venv ./backend/.venv
source ./backend/.venv/bin/activate
python -m pip install --upgrade pip
pip install -r backend/requirements.txt
```

### Environment Configuration (.env)

Create a `.env` file in the project root or `backend/` directory (or copy from `.env.example`):

```env
# Groq LLM Reasoning Configuration
GROQ_API_KEY=your_groq_api_key_here
GROQ_BASE_URL=https://api.groq.com/openai/v1
GROQ_MODEL=openai/gpt-oss-20b
ENABLE_REASONING=true
MOVEINSIGHT_API_URL=http://127.0.0.1:8000
```

The system automatically loads `GROQ_API_KEY` from `.env` using `python-dotenv`. If no key is set, MoveInsight operates cleanly with deterministic fallback narration.

## Run the alert pipeline manually

Start the API first:

```bash
cd /path/to/MoveInsight
backend/.venv/bin/python -m uvicorn backend.api:app --host 0.0.0.0 --port 8000
```

Then run one tenant-wide pipeline execution. The scheduler discovers the latest
completed period and calls the API; it does not open a second DuckDB connection:

```bash
backend/.venv/bin/python -m backend.scheduler --once
```

The command logs the selected period, API URL, run ID, alert count, tenant count,
and any failure. Reasoning follows `ENABLE_REASONING` from `.env`. To override it
for one run:

```bash
backend/.venv/bin/python -m backend.scheduler --once --disable-reasoning
backend/.venv/bin/python -m backend.scheduler --once --enable-reasoning
```

## Run the pipeline daily with cron

Keep the FastAPI service running, then edit the crontab with `crontab -e` and add
this daily 02:00 job. Replace `/path/to/MoveInsight` with the absolute repository
path:

```cron
0 2 * * * cd /path/to/MoveInsight && mkdir -p logs && backend/.venv/bin/python -m backend.scheduler --once >> logs/moveinsight-scheduler.log 2>&1
```

Inspect scheduler activity with:

```bash
tail -f logs/moveinsight-scheduler.log
```

Healthy runs include `Starting alert pipeline` followed by `Alert run ... completed`.
Failures include a traceback and `Scheduled alert pipeline failed` when using the
long-running mode. Cron failures are also captured in the redirected log file.

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
  "period": "2026-07",
  "grain": "month"
}
```

`grain` can be `month`, `week`, or `day`; use a matching period such as
`2026-07`, `2026-W29`, or `2026-07-15`.

Only `method` is required. `filters` and `period` are optional; when omitted,
the context engine uses the full data scope and its latest available period.
`grain` is optional and defaults to `month`.

For `method="ota"`, the response is the grouped OTA benchmark contract:

```json
{
  "kpi": "ota",
  "tenant_id": "pinnacle-Slc",
  "period": "2026-07",
  "grain": "month",
  "overall": {
    "value": 96.27,
    "n": 88574,
    "sla": 90.0
  },
  "groups": [
    {
      "dimension": "vendor",
      "name": "Pooja Sokolov Travel",
      "value": 31.45,
      "n": 248,
      "sla_gap_pts": -58.55,
      "breached": true
    }
  ]
}
```

For non-OTA KPIs, `ContextEngine` still returns the richer generic context
object with trend, SLA, peer, industry, attribution, assessment, and headline.

## Test C4 insights through the API

Evaluate OTA groups for one tenant/period:

```bash
curl -X POST http://127.0.0.1:8000/insights/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"method":"ota","filters":{"tenant_id":"pinnacle-Slc"},"period":"2026-07","grain":"month"}'
```

Scan ranked anomalies for a period:

```bash
curl -X POST http://127.0.0.1:8000/insights/scan \
  -H 'Content-Type: application/json' \
  -d '{"period":"2026-07","grain":"month","tenant_id":"pinnacle-Slc","kpis":["ota"],"dimensions":["vendor"]}'
```

For OTA, `/insights/evaluate` returns a list of breached groups. Both routes
return deterministic C4 output and are also available through the
interactive docs at `http://127.0.0.1:8000/docs`.

## Use the OTA reasoning layer (C5)

Set your Groq key in the environment before starting the API, or fill
`GROQ_API_KEY` in `backend/config.py` for a local demo run:

```bash
export GROQ_API_KEY="..."
export GROQ_REASONING_MODEL="openai/gpt-oss-20b"
```

Reason over all C4 OTA alerts for one context:

```bash
curl -X POST http://127.0.0.1:8000/agent/ota/reason \
  -H 'Content-Type: application/json' \
  -d '{"method":"ota","filters":{"tenant_id":"pinnacle-Slc"},"period":"2026-07","grain":"month"}'
```

The C5 route does not compute KPIs and does not decide anomalies. It builds the
C3 OTA benchmark, lets C4 classify group-level SLA breaches, then sends all C4
alerts to Groq for concise operational interpretations and
investigation-oriented next steps. The response includes a `results` array with
one reasoning object per C4 alert.

### API flow

1. Call `POST /context` when the consumer needs C3 context only.
2. Call `POST /insights/evaluate` when OTA groups should be classified into
   anomaly signals, priority score, and summary.
3. Call `POST /insights/scan` when ranked anomalies are needed across a tenant's
   vendors/offices for a period.

The C4 routes calculate their own C3 context; clients do not send calculated
values or insight scores:

```text
request -> Metrics -> ContextEngine (C3) -> InsightEngine (C4) -> response
```

The C5 OTA route extends that flow:

```text
request -> Metrics -> ContextEngine (C3) -> InsightEngine (C4) -> OtaReasoningAgent (C5) -> response
```

Invalid request fields such as an unsupported `grain` return `422`. Valid
request shapes with an unknown KPI or filter dimension return `400`.

### API state

The API is stateless. It does not persist sessions, request history, computed
contexts, or insight results. Every request recalculates its response from the
read-only DuckDB data. The API process keeps a database connection open while
running, but restarting Uvicorn does not lose application state.

## Use the metrics layer

```python
from backend.metrics import Metrics
m = Metrics("backend/mobility.duckdb")

m.ota()                                   # overall on-time arrival
m.ota({"vendor": "Pooja Mikhailov Travel"})
m.ota({"tenant_id": "pinnacle-Slc"}, period="2026-07", grain="month")
m.sla_gap({"tenant_id": "vanta-Sea"})     # signed pts vs SLA
m.ota_by_vendor(tenant_id="pinnacle-Slc") # volume-normalized peer ranking (feeds C3)
m.ota_trend()                             # month-over-month (feeds C3 trend context)
m.data_health("pinnacle-Slc")            # graceful-degradation panel
```

## Use the context engine (C3)

For OTA, C3 returns the grouped benchmark object that feeds C4.

```python
from backend.metrics import Metrics
from backend.context import ContextEngine

m = Metrics("backend/mobility.duckdb")
c = ContextEngine(m)

ctx = c.context("ota", {"tenant_id": "pinnacle-Slc"}, period="2026-07", grain="month")
ctx["overall"]       # tenant-level OTA value, sample size, and SLA
ctx["groups"]        # all grouped vendor rows with value, n, SLA gap, breach flag

# Configurable time grain — month (default) | week | day.
# Weekly gives ~13 trend points from the 3-month dataset vs 3 monthly.
c.context("ota", {"tenant_id": "pinnacle-Slc"}, period="2026-W29", grain="week")
```

Period buckets are derived from the data (`Metrics.periods(grain)`), not
hard-coded, so month/week/day contexts all use the same C3 contract. C3 does
not trim to top drivers for OTA; C4 receives every group in `groups`.

## Use the C3/C4 intelligence layers

```python
from backend.metrics import Metrics
from backend.context import ContextEngine
from backend.insights import InsightEngine

m = Metrics("backend/mobility.duckdb")
c3 = ContextEngine(m)
c4 = InsightEngine(c3)

ctx = c3.context("ota", {"tenant_id": "pinnacle-Slc"}, month="2026-07")
c4.evaluate_context(ctx)                  # returns ranked OTA SLA-breach insights
c4.scan_month("2026-07", tenant_id="pinnacle-Slc")
                                           # ranked anomalies for C5/C6/C7
c4.scan_period("2026-W29", grain="week", tenant_id="pinnacle-Slc")
                                           # same C4 rules over weekly C3 context
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

- **C3 (benchmarking)** is built — for OTA, `context.py` returns tenant-level overall OTA plus all grouped vendor/office rows with SLA gaps.
- **C4 (insights/anomalies)** consumes C3 OTA groups and raises deterministic SLA-breach anomalies from configured sample thresholds.
- **C5 (OTA reasoning)** consumes C4 OTA output and calls Groq for concise interpretation only; all numbers still come from C2/C3/C4.
- **C8 (dashboard/chat)** reads the same `Metrics`/`ContextEngine` functions — one source of truth.
