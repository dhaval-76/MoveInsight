# High-Level Design (HLD) — v2 (data-validated)
## Agentic Intelligence & Reporting Layer for Enterprise Mobility

**Project:** MoveInSync Hackathon — Agentic AI for Enterprise Mobility / Operations Intelligence
**Document type:** High-Level Design
**Status:** Draft for hackathon build — **updated against the real provided dataset**
**Last updated:** 2026-09-04

> **What changed in v2 vs v1:** The design is now grounded in the actual dataset (7 CSVs, ~608K trips, May–Aug 2026, 5 tenants, 23 vendors). Major corrections:
> - **No GPS-trace file exists.** The assumed `gps_pings` entity is removed and replaced by a real **`alerts`** entity (safety/device events: panic, woman-travelling-alone, over-speeding, geofence, device-not-reachable).
> - **Multi-tenancy is real, not hypothetical** — `business_unit` is a natural tenant key with 5 tenants in the data.
> - **Vendor is a name string (23 vendors), no driver entity** — dimensions are vendor / office / mode / shift / direction.
> - KPIs, data model, and messy-data handling are rewritten to match observed columns and real data-quality issues.

---

## 1. Overview

### 1.1 Purpose
An **agentic intelligence and reporting layer** on top of enterprise mobility operations data. It **senses** what is happening across trips, vendors, employees, safety events and cost; **reasons** by contextualizing every metric against a reference point (historical trend, SLA/goal, peer, or industry norm); and **acts** by surfacing impact-ranked insights, generating leadership-ready narratives, and drafting communications — with minimal human prompting.

### 1.2 Problem being solved
Mobility ops generate rich structured data continuously, but it sits in static weekly/monthly reports. A metric without context is just a number: "OTA is 78%" matters far less than "it was 85% last month, SLA is 90%, and two vendors are responsible for the gap." Managers spend time **assembling** data instead of **acting** on it.

### 1.3 Design goals (mapped to evaluation criteria)
| Goal | Why it matters | Evaluation weight |
|---|---|---|
| Reduce manager effort; surface missed decisions; leadership-ready output | Business impact & experience | 35 |
| End-to-end working prototype on the provided dataset | Functionality | 25 |
| Genuine sense→reason→act loop; low inference cost & latency at scale | Agentic design & cost at scale | 20 |
| Clean, deployable, multi-tenant-ready architecture | Architecture & code quality | 20 |

### 1.4 Guiding principle
**The LLM does language and judgment; deterministic code does all the math.** No KPI is ever computed by the model. This drives accuracy (metrics are exact and testable), cost (metric lookups are near-free function calls, not reasoning tokens), and latency (sub-second numeric responses).

---

## 2. Scope

### 2.1 In scope
- Ingestion and normalization of the **actual** dataset (3 monthly trip files + alerts + bill + employee + feedback).
- Deterministic metrics/semantic layer with canonical KPI definitions.
- Benchmarking/context engine (trend, SLA, peer, industry norm).
- Insight & anomaly detection with business-impact ranking.
- Agentic orchestration loop (sense → reason → act) with scheduled proactive triggers.
- Narrative/report generation (leadership-ready briefing).
- Action layer: drafted communications (vendor escalation, ops alert) with human-in-the-loop approval.
- Presentation surfaces: decision-support dashboard, conversational agent, auto-briefing, proactive alerts.
- Serves at least one named persona (primary: Transport & Facilities Head; secondary: Transport Manager).

### 2.2 Out of scope (per problem statement)
- Production-grade authentication or security.
- A full historical data pipeline.
- Integration with real vendor systems (communications are drafted and mocked, not actually sent).
- Live system access (anonymized sample dataset only).

### 2.3 Dataset facts (validated 2026-09-04)
| File | Rows | Grain | Role |
|---|---|---|---|
| `Ride_data _trip-{may,june,july}_2026.csv` | 615,546 (608,793 distinct trips) | one row per trip | Core fact: delay, escort, km, employee counts, vendor, mode |
| `emp_Data.csv` | 1,637,906 | one row per employee-per-trip | Boarding/no-show, gender, role, pickup/drop epochs |
| `bill_data.csv` | 620,942 | one row per trip billing | Cost per trip, contract, slab, km |
| `trip_feedback.csv` | 512,873 | one row per rating | Route/driver/cab/safety/marshal ratings |
| `alerts_data.csv` | 51,699 | one row per safety/device event | **Replaces GPS**: panic, over-speeding, geofence, woman-alone, device-not-reachable |

**Coverage (join key = `trip_id`):** emp 100%, bill 99.9%, feedback 49% (only rated trips — expected), alerts 5.5% (rare safety events — by nature).
**Tenants (`business_unit`):** `pinnacle-Slc`, `vanta-Sea`, `catalyst-Sac`, `vanta-Aus`, `orbit-Slc` (5).
**Vendors:** 23 (names, e.g. "Sanjay Mikhailov Travel"). **Modes (`product_type`):** CAB 513K, BUS 100K, SPOT_2.0 2.3K.
**Date range:** 2026-05-01 → 2026-08-01 (epochs are IST-localized; standardized to UTC on ingest).
**Observed KPI baselines:** OTA (≤10 min) 96.4% overall; vendor OTA spread **93.2%–99.3%**; no-show 7.71%; avg feedback ~4.88/5; avg cost ~₹1,343/trip.

### 2.4 Assumptions
- SLA targets are **not** in the dataset → supplied via a small editable config per tenant/vendor (default 90% OTA), with stated assumptions.
- Industry-benchmark reference values are supplied via editable config with stated assumptions.
- Feedback skews high (~4.88 avg) → experience insights use **relative movement**, not absolute level.

---

## 3. Personas & primary use cases

| Persona | Role | Primary need | This system delivers |
|---|---|---|---|
| Transport & Facilities Head (primary) | Strategic — budget, SLA, vendor strategy, leadership reporting | A coherent cost/safety/experience story without assembling it | Auto-generated, forwardable executive briefing with contextualized metrics |
| Transport Manager (secondary) | Operational — vendor coordination, escalations, shift planning, delays | Fast, actionable signals, not reports | Proactive impact-ranked alerts + drafted vendor escalations |
| Team / Line Manager | Shift-based ops | Shift-level visibility: who made it, who was late, delay ripple | Shift-view drill-down (stretch goal) |

**Representative use cases**
- UC-1: Head opens Monday brief → sees top 3 movements, each contextualized, with root-cause attribution and recommended actions; forwards it to leadership with minimal edits.
- UC-2: System detects a vendor's OTA degradation crossing SLA → fires an alert to the Transport Manager and drafts an escalation email pre-filled with evidence; manager approves to send.
- UC-3: Manager asks in natural language "why did OTA drop last week?" → agent answers with contextualized numbers and citations back to the underlying trips.
- UC-4 (**new, data-driven**): A spike in `WOMAN_TRAVELLING_ALONE` / `PANIC` alerts for a vendor or office → safety insight ranked high, escalation drafted.

---

## 4. Architecture overview

### 4.1 Layered architecture
Data flows **upward** and is enriched at every tier; the agent loop drives the middle tiers and pushes outputs to the presentation surfaces.

```
+-----------------------------------------------------------------------+
|  PRESENTATION                                                         |
|  Dashboard (contextual KPIs) · Chat (NL Q&A, cited) ·                 |
|  Auto-briefing (leadership-ready) · Alerts (impact-ranked)           |
+----------------------------------^------------------------------------+
                                   |
+----------------------------------|------------------------------------+
|  AGENT ORCHESTRATION             |                                    |
|  sense -> reason -> act  ·  model routing for cost  ·  memory         |
+------------------^-------------------------^--------------------^------+
                   |                         |                    |
+------------------+------+   +--------------+-------+  +---------+-------------+
| Insight & anomaly       |   | Benchmarking engine  |  | Action layer          |
| detect & rank by impact |   | vs trend/SLA/peer     |  | draft comms, alerts   |
+------------------^------+   +--------------^-------+  +---------^-------------+
                   |                         |                    |
+------------------+-------------------------+--------------------+------+
|  SEMANTIC / METRICS LAYER (deterministic)                            |
|  canonical KPIs · safe query interface · entity model                |
+----------------------------------^------------------------------------+
                                   |
+----------------------------------+------------------------------------+
|  DATA INGESTION & NORMALIZATION                                       |
|  ID/date/cost normalization · alert-severity cleaning ·              |
|  entity matching on trip_id · tenant tagging · DuckDB                 |
+-----------------------------------------------------------------------+
```

Legend: deterministic/exact = ingestion + metrics layers; AI reasoning = orchestration; differentiators = benchmarking + action.

### 4.2 Component responsibilities (summary)
| # | Component | Responsibility | Deterministic / AI |
|---|---|---|---|
| C1 | Data ingestion & normalization | Load 7 CSVs, normalize IDs/dates/cost, clean alert severity, map to canonical schema, tag tenant | Deterministic |
| C2 | Semantic / metrics layer | Single source of truth for KPIs; safe query interface | Deterministic |
| C3 | Benchmarking / context engine | Wrap each metric with trend/SLA/peer/industry context | Deterministic |
| C4 | Insight & anomaly detection | Detect outliers/breaches, rank by business impact | Deterministic (+ optional AI summary) |
| C5 | Agent orchestration | Plan tool calls; run sense→reason→act; route models; memory | AI |
| C6 | Reporting / narrative generation | Turn ranked, contextualized insights into leadership prose | AI (template-guided) |
| C7 | Action / communication layer | Draft escalations/alerts; human-in-the-loop approval | AI draft + deterministic routing |
| C8 | Presentation | Dashboard, chat, briefing, alerts; persona views | UI |
| C9 | Proactive trigger / scheduler | Periodically run the sense loop; fire alerts on breach | Deterministic |

---

## 5. Component design (detail)

### C1 — Data ingestion & normalization
- **Input:** the 7 provided CSVs (3 monthly trip files + `emp_Data`, `bill_data`, `trip_feedback`, `alerts_data`).
- **Function:**
  - **ID normalization** — `trip_id`/`stwid` arrive with commas + quotes (`"1,516,906"`) in some files, plain (`1530200`) in others → strip to canonical integer string. This is what makes cross-file joins work.
  - **Date/epoch normalization** — 3 formats coexist (`"July 1, 2026"`, `2026-07-09`, `"May 1, 2026, 12:03 AM"`) plus comma-formatted epochs (`"1,782,864,900"`); epochs are IST-localized → normalize all to UTC timestamps.
  - **Cost cleaning** — `trip_cost` has negatives (min −2,233,333) and extreme highs (max 104,447) → clip/winsorize + quarantine with a counter (credits/adjustments flagged, not silently dropped).
  - **Alert-severity cleaning** — `severity` mixes `Sev-1/2/3` with junk (`NA` 16K, `False` 15K) → map to a valid enum + `severity_unknown` flag.
  - **Canonical-schema mapping** via a thin adapter (the only part expected to change if the schema shifts).
  - **Tenant tagging** — carry `business_unit` as `tenant_id` on every canonical row.
- **Store:** DuckDB — zero-setup, fast analytical queries over the full ~3M rows, no server.
- **Canonical entities:** `trips`, `vendors` (derived), `employees`, `feedback`, `alerts`, `bills`.
- **Messy-data handling (graded good-to-have):** unmatched records quarantined with counters; incomplete feedback/alerts handled via null-safe (LEFT) joins; a **data-health indicator** is surfaced (e.g., "100% of trips billed & rostered; 49% rated; 5.5% with safety alerts; N cost rows quarantined").
- **Output:** clean, queryable canonical tables + a data-quality report.

### C2 — Semantic / metrics layer
- **Function:** one canonical, tested definition per KPI, implemented as pure functions / SQL views. Both the dashboard and the agent read from here.
- **KPIs (mapped to real columns):**
  | KPI | Source |
  |---|---|
  | OTA / on-time arrival | `delay_minutes` (≤ threshold) or planned vs actual epochs |
  | SLA adherence | OTA vs configured SLA target |
  | No-show rate | `noshow_cnt` / `plannedemployee_cnt`; corroborated by `emp.is_no_show` |
  | Cost per trip / per employee | `bill.trip_cost` / employee counts |
  | Occupancy / utilization | `actualemployee_cnt` / `actual_cab_capacity` |
  | Safety-compliance score | `alerts` (panic, over-speeding, geofence, device) + `actual_escort` on night trips |
  | Night-escort compliance | `actual_escort` on late-shift trips + `WOMAN_TRAVELLING_ALONE` alerts |
  | Cancellation / fill | planned vs actual employee counts, `boarding_status` |
  | CO2 per trip | `traveled_km` × emission factor by `actual_cab_fuel_type` (Petrol/EV/HYD) |
  | Vendor performance index | composite of OTA + safety + cost + feedback per vendor |
  | Employee-experience score | `feedback` ratings (relative movement) |
- **Dimensions:** tenant (`business_unit`), vendor, office, mode (`product_type`), shift, direction (LOGIN/LOGOUT), time window.
- **Safe query interface:** **parameterized / whitelisted metric functions** (decision locked — no free-form text-to-SQL from the model). Each KPI is a named function with typed dimension filters.
- **Output:** exact metric values with dimensions.

### C3 — Benchmarking / context engine (headline differentiator)
- **Function:** for every metric, produce a **context object**:
  `{ value, delta_vs_last_period, moving_avg, vs_sla, percentile_among_peers, drivers_of_change }`.
- **Four reference points (brief requires at least one):**
  - Historical trend — May vs June vs July, and N-period moving average (3 months available).
  - SLA / goal — signed distance from configured target.
  - Peer comparison — rank the 23 vendors / offices / routes against each other. **Volume-normalized** (per-trip rates, min-sample threshold) so "worst vendor" is not just the biggest.
  - Industry norm — configurable defaults with stated assumptions.
- **Drivers of change:** attribution — e.g., "2 vendors cause 70% of the OTA drop."
- **Output:** e.g., "OTA 93.2%, down from 96%, under the 90% SLA on 3 routes; Pooja Mikhailov Travel and Rahul Orlov Travel own most of the gap."

### C4 — Insight & anomaly detection
- **Function:** statistical detection over the metrics layer — z-score / IQR outliers, month-over-month step changes, SLA-breach detection, vendor degradation trends, safety-alert spikes, route/office delay clustering.
- **Seasonality:** baseline by **day-of-week and shift** to avoid weekend/late-shift false positives.
- **Impact ranking:** findings ranked by business impact (cost delta × affected employees × severity), not raw statistical size — surfaces the ~3 things that matter, not 50 noisy blips.
- **Output:** ranked list of insights, each linked to its context object and supporting rows.

### C5 — Agent orchestration
- **Function:** planner/router that decides which tools to call and in what order (ReAct or plan-execute) for a given trigger or question. The **reason** step is where the agent visibly *decides*: which reference point is most telling, whether severity warrants escalation, which persona is the audience, and what action to draft — the trace is shown in the demo to prove genuine agency (not decoration).
- **Model routing (graded — cost at scale):** simple intent classification and templated narration → small cheap model; complex multi-step reasoning → larger model. Documented with a concrete cost-per-interaction figure in the deck.
- **Memory:** conversation memory + **per-persona alert state** (what has been flagged/acknowledged) to avoid duplicate alerts — persisted (see `agent_state` in §6).
- **Loop:** sense (scheduled scan) → reason (contextualize + assess severity + identify audience) → act (draft output + route to persona).

### C6 — Reporting / narrative generation
- **Function:** convert ranked, contextualized insights into a structured executive briefing: headline, 3 key movements with context, root-cause attribution, recommended actions, one-line "so what."
- **Template-guided generation:** LLM fills a fixed structure; every number is injected from C2/C3, never generated — consistent, cheap, hallucination-resistant.
- **Output formats:** HTML → print-to-PDF one-pager forwardable with minimal edits; optional scheduled "Monday Morning Mobility Brief."

### C7 — Action / communication layer
- **Function:** draft vendor escalation emails, ops alerts (Slack/WhatsApp-style), calendar-ready summaries — pre-filled with contextualized evidence (e.g., the offending vendor's OTA vs SLA vs peers + affected employee count).
- **Human-in-the-loop:** agent proposes, human approves/sends. Actual send is mocked (out of scope); the drafted, ready-to-send artifact is shown.
- **Safety:** irreversible/outbound actions always require explicit human approval.

### C8 — Presentation
- **Decision-support dashboard:** contextualized KPI tiles, each with a context badge (up/down vs SLA/trend), filterable by tenant/vendor/mode.
- **Conversational agent:** NL Q&A with drill-down and **citations** — every answer links back to underlying trips ("based on 1,204 trips across 3 vendors").
- **Persona toggle:** one backend, three lenses (strategic / operational / shift).
- **Tech:** Python (FastAPI) backend + React/Angular frontend; brief prefers Java+Angular+AWS but is not restrictive — functionality outweighs stack conformity.

### C9 — Proactive trigger / scheduler
- **Function:** run the sense loop on a timer; fire alerts when thresholds break. Enables proactive (not purely on-demand) behavior — a graded good-to-have.
- **Output:** unprompted alerts and scheduled briefings.

---

## 6. Data model (canonical)

| Entity | Key fields | Source / notes |
|---|---|---|
| `trips` | trip_id (PK), tenant_id (business_unit), office, mode (product_type), shift_type, trip_direction, vendor, actual_escort, planned/actual start/end (UTC), delay_min, planned_km, traveled_km, fuel_type, planned_emp_cnt, actual_emp_cnt, noshow_cnt, status | Union of 3 monthly trip files. Core fact table |
| `vendors` | vendor (PK), tenant_id, sla_target (config) | **Derived** from trips (23 names); SLA joined from config |
| `employees` | trip_id (FK), stwid (emp id), tenant_id, gender, emp_role, boarding_status, is_no_show, pickup/drop epochs (UTC) | From `emp_Data` (per employee-per-trip) |
| `alerts` | event_id (PK), trip_id (FK), tenant_id, event_type, severity (cleaned), severity_unknown (flag), start_time, ack_time, state, source | From `alerts_data`. **Replaces gps_pings** |
| `bills` | trip_id (FK), tenant_id, vendor, contract, slab, total_km, trip_cost (cleaned), cost_quarantined (flag), cycle_start/end | From `bill_data` |
| `feedback` | trip_id (FK), stwid, tenant_id, trip_type, route/driver/cab/safety/marshal ratings, creation_time | From `trip_feedback` (only ~49% of trips) |
| `data_quality` | metric, tenant_id, value | Unmatched/quarantined counters, coverage %, cost outliers flagged |
| `agent_state` | id (PK), tenant_id, persona, insight_fingerprint, status (flagged/acknowledged/sent), ts | **New** — dedupe + audit for the agent loop |

Derived views compute KPIs. No `drivers` entity (not in data); no `gps_pings` (not provided).

---

## 7. Key flows

### 7.1 Proactive alert (sense → reason → act)
1. Scheduler (C9) triggers a scan.
2. C4 detects a vendor's OTA crossing SLA (or a safety-alert spike); C3 attaches context (trend, SLA gap, peer rank, drivers).
3. C4 ranks it high by business impact (cost × affected employees × severity).
4. C5 reasons: assesses severity, picks audience (Transport Manager), checks `agent_state` to avoid a duplicate alert.
5. C7 drafts an escalation email with evidence.
6. C8 shows the alert + draft; human approves to send (mocked); `agent_state` updated.

### 7.2 Conversational query
1. User asks a question in C8 chat.
2. C5 parses intent; calls C2 (safe metric function) and C3 (context) via tools.
3. C6 narrates the answer with citations back to rows.
4. Response returned sub-second where no heavy reasoning is needed.

### 7.3 Executive briefing
1. Scheduler (or on-demand) triggers briefing generation.
2. C4 produces ranked insights; C3 contextualizes each.
3. C6 fills the briefing template with injected numbers.
4. C8 renders a forwardable HTML/PDF one-pager.

---

## 8. Non-functional considerations

### 8.1 Cost & latency at scale (graded 20%)
- Metrics computed in deterministic code, not by the LLM — near-zero marginal cost, sub-second.
- Model routing: cheap model for simple/templated work; large model only for complex reasoning.
- Caching of computed metrics, context objects, and generated narratives.
- **Concrete target for the deck:** a briefing ≈ one large-model call over ~a few hundred injected tokens; a numeric chat answer ≈ 0 model tokens (pure function call) or one small-model narration. Put a rupee/paise-per-interaction and p95-latency figure on the slide.

### 8.2 Deployability & multi-tenancy (bonus)
- **Real** tenant isolation via `tenant_id` (business_unit) partitioning on all queries — 5 tenants already in the data.
- Stateless services + a metrics store; horizontally scalable.
- Config-driven SLA targets and benchmarks per tenant/vendor.
- Clean separation of deterministic core vs AI layer eases embedding into an existing platform.

### 8.3 Reliability / graceful degradation
- Messy-data handling with quarantine + data-health surfacing (cost outliers, unknown severities, unmatched IDs).
- Null-safe (LEFT) joins for partial feedback/alerts coverage.
- If the AI layer is unavailable, the deterministic dashboard and metrics still function.

### 8.4 Security (lightweight, per scope)
- Production auth/security explicitly out of scope.
- Still: parameterized/whitelisted metric functions only; **no arbitrary SQL from the model**; human approval gates all outbound/irreversible actions.

---

## 9. Technology choices

| Concern | Choice | Rationale |
|---|---|---|
| Data store | DuckDB | Zero-setup, fast analytics over ~3M rows, no server |
| Backend | Python + FastAPI | Ship fastest; brief is not restrictive |
| Frontend | React (or Angular) | Same |
| AI models | Small model for routing/templating; larger for reasoning | Cost/latency at scale |
| Reporting | HTML → PDF one-pager | Forwardable with minimal edits |
| Cloud (optional) | AWS | Brief preference; not required for demo |

---

## 10. Build plan & risks

### 10.1 Build sequence
1. **Pre-hackathon (now):** dataset profiled ✓; schema locked ✓. Scaffold app, ingestion adapter (against the real columns above), DuckDB store, metrics-layer interface, benchmarking logic, orchestration skeleton, dashboard shell, narrative templates.
2. **Day 0 hour 1:** freeze API contracts between the 4 workstreams (data → intelligence → agent → UI).
3. **Then:** verify KPIs against baselines (OTA 96.4%, no-show 7.71%), tune anomaly thresholds, polish briefing + demo script.

### 10.2 Priority
Protect one flawless end-to-end slice — **data → detected anomaly → contextualized insight → generated briefing → drafted action** — over breadth. Functionality is 25% and literally "it runs."

### 10.3 Risks & mitigations
| Risk | Mitigation |
|---|---|
| Schema drift on join keys | ID normalization in the adapter (validated: 99.9–100% join coverage after normalization) |
| Over-investing in chatbot | Prioritize auto-briefing + proactive alerts (highest business-impact score) |
| LLM cost/latency blowup | Deterministic metrics + model routing + caching |
| Hallucinated numbers | Template-guided narration with injected values; citations |
| Messy/missing data breaks the demo | Quarantine + null-safe joins + data-health panel (cost outliers, junk severities) |
| "Agentic" looks like decoration | Show the reason-step decision trace; proactive unprompted alerts |

---

## 11. Deliverables (per problem statement)
- Source code repository (GitHub/GitLab)
- Architecture diagram
- README + setup instructions
- Sample inputs/outputs
- Demo video (if requested)
- Presentation deck
- Live demo

---

## 12. Differentiators (why this stands out)
1. Deterministic-metrics + LLM-judgment split, shown explicitly with a cost-per-interaction figure.
2. Benchmarking context object on every number — the brief's stated missing capability.
3. Impact-ranked proactive alerts (unprompted), not a chatbot.
4. Leadership-ready, one-click forwardable auto-briefing.
5. Data-health / graceful-degradation panel (real cost outliers + junk severities to show).
6. Citations + drill-down for trust.
7. **Real** multi-tenancy (5 tenants in the data) & deployability story in one slide.
8. Closed-loop action: agent drafts the vendor escalation with evidence attached, human approves.
9. **Safety intelligence from the alerts feed** (panic / woman-travelling-alone / over-speeding) — a dimension beyond OTA/cost that lands with leadership.
