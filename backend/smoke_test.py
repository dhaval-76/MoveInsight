"""End-to-end smoke test for the full stack: C1 -> C2 -> C3 -> C4 -> C5.

Run from the project root:
    python -m backend.smoke_test

Verifies:
  C1  the canonical DuckDB exists and the 7 tables are populated & joinable.
  C2  every whitelisted KPI returns a sane value; safe-query guard works.
  C3  context engine produces well-formed context with reference points & drivers.
  C4  insight engine detects and ranks anomalies by business priority score.
  C5  agent orchestrator handles Sense+Reason+Act (when reasoning enabled) and
      Sense+Act (when reasoning disabled), generating evidence-backed drafts.
"""
from __future__ import annotations
import sys

from .metrics import Metrics, ALLOWED_DIMS
from .context import ContextEngine
from .insights import InsightEngine
from .agent import AgentOrchestrator
from . import config as C

DB = "backend/mobility.duckdb"
TENANT = "pinnacle-Slc"
MONTH = "2026-07"

CANON_TABLES = ["trips", "employees", "bills", "feedback", "alerts", "vendors", "data_quality"]

ok = 0
fail = 0


def check(name, cond, detail=""):
    global ok, fail
    mark = "PASS" if cond else "FAIL"
    if cond:
        ok += 1
    else:
        fail += 1
    print(f"  [{mark}] {name}{('  -> ' + detail) if detail else ''}")
    return cond


# --------------------------------------------------------------------------- C1
def test_c1(m: Metrics):
    print("\nC1  ingestion / canonical tables")
    for t in CANON_TABLES:
        n = m.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        check(f"table {t} populated", n > 0, f"{n:,} rows")
    j = m.con.execute(
        "SELECT count(*) FROM trips t JOIN employees e USING(trip_id)"
    ).fetchone()[0]
    tot = m.con.execute("SELECT count(*) FROM trips").fetchone()[0]
    check("trips<->employees join coverage >= 99%", j >= 0.99 * tot,
          f"{j:,}/{tot:,} = {100*j/tot:.1f}%")


# --------------------------------------------------------------------------- C2
def test_c2(m: Metrics):
    print("\nC2  metrics layer (deterministic KPIs)")
    ota = m.ota({"tenant_id": TENANT}, MONTH)
    check("ota value in 0..100", ota["value"] is not None and 0 <= ota["value"] <= 100,
          f"{ota['value']}% over n={ota['n']:,}")

    cost = m.cost_per_trip({"tenant_id": TENANT}, MONTH)
    check("cost_per_trip positive", cost["value"] and cost["value"] > 0,
          f"INR {cost['value']}")

    ns = m.noshow_rate({"tenant_id": TENANT}, MONTH)
    check("noshow_rate in 0..100", ns["value"] is not None and 0 <= ns["value"] <= 100,
          f"{ns['value']}%")

    grp = m.kpi_by_group("ota", "vendor", {"tenant_id": TENANT}, MONTH, min_n=2000)
    check("kpi_by_group returns >=2 vendors", len(grp) >= 2, f"{len(grp)} vendors")

    try:
        m.ota({"driver_name": "x"})
        check("safe-query guard blocks illegal dim", False, "no error raised!")
    except ValueError:
        check("safe-query guard blocks illegal dim", True, "ValueError raised")


# --------------------------------------------------------------------------- C3
REQUIRED_KEYS = {"kpi", "label", "value", "trend", "sla", "peer", "industry",
                 "drivers_of_change", "assessment", "headline"}


def test_c3(ce: ContextEngine):
    print("\nC3  context engine (benchmarking)")
    for kpi in C.METRIC_REGISTRY:
        ctx = ce.context(kpi, {"tenant_id": TENANT}, MONTH)
        missing = REQUIRED_KEYS - set(ctx)
        good = not missing and isinstance(ctx["trend"]["series"], list) \
            and len(ctx["trend"]["series"]) == len(C.DATA_MONTHS) \
            and isinstance(ctx["headline"], str) and ctx["headline"].endswith(".")
        check(f"context('{kpi}') well-formed", good,
              (f"missing {missing}" if missing else f"assess={ctx['assessment']}"))

    print("\n  sample headline (ota, whole tenant):")
    print("   ", ce.context("ota", {"tenant_id": TENANT}, MONTH)["headline"])

    d = ce.context("ota", {"tenant_id": TENANT}, MONTH)["drivers_of_change"]
    check("drivers_of_change populated at tenant scope", len(d) >= 1,
          f"{len(d)} drivers, top={d[0]['label'] if d else '-'}")

    one_vendor = d[0]["label"] if d else "Pooja Mikhailov Travel"
    d2 = ce.context("ota", {"tenant_id": TENANT, "vendor": one_vendor}, MONTH)["drivers_of_change"]
    check("drivers empty when vendor is pinned", d2 == [], f"{len(d2)} drivers")

    wk = ce.context("ota", {"tenant_id": TENANT}, period="2026-W29", grain="week")
    check("weekly grain yields > monthly points",
          len(wk["trend"]["series"]) > len(C.DATA_MONTHS),
          f"{len(wk['trend']['series'])} weekly buckets")
    check("weekly headline says 'last week'", "last week" in wk["headline"],
          wk["headline"][:60] + "...")


# --------------------------------------------------------------------------- C4
def test_c4(ie: InsightEngine):
    print("\nC4  insight & anomaly engine")
    anomalies = ie.scan_period(MONTH, tenant_id=TENANT, kpis=["ota", "noshow_rate", "cost_per_trip"], dimensions=["vendor"])
    check("scan_period detects anomalies", len(anomalies) > 0, f"found {len(anomalies)} anomalies")
    if anomalies:
        top = anomalies[0]
        check("top anomaly ranked with score >= 50", top["priority_score"] >= 50,
              f"score={top['priority_score']} band={top['priority_band']} kpi={top['kpi']}")


# --------------------------------------------------------------------------- C5
def test_c5(agent: AgentOrchestrator, ie: InsightEngine):
    print("\nC5  agent orchestrator (configurable reasoning mode)")
    anomalies = ie.scan_period(MONTH, tenant_id=TENANT, kpis=["ota", "noshow_rate", "cost_per_trip"], dimensions=["vendor"])
    if not anomalies:
        check("C5 anomaly processing", False, "no C4 anomalies to test")
        return

    top_anomaly = anomalies[0]

    # Mode 1: Sense + Reason + Act (enable_reasoning=True)
    res_reason = agent.process_anomaly(top_anomaly, enable_reasoning=True)
    check("C5 Sense+Reason+Act produces audit trace",
          res_reason["reasoning_enabled"] and len(res_reason["reasoning_trace"]) >= 4,
          f"{len(res_reason['reasoning_trace'])} reasoning steps")
    check("C5 generates evidence-backed escalation draft",
          res_reason["action_draft"]["type"] in ["vendor_escalation_email", "safety_incident_alert", "roster_compliance_notice"] and
          res_reason["action_draft"]["status"] == "PROPOSED_WAITING_APPROVAL",
          f"subject='{res_reason['action_draft']['subject']}'")

    # Mode 2: Sense + Act (enable_reasoning=False)
    res_no_reason = agent.process_anomaly(top_anomaly, enable_reasoning=False)
    check("C5 Sense+Act bypasses reasoning drilldown",
          not res_no_reason["reasoning_enabled"] and
          res_no_reason["reasoning_trace"][0]["step"] == "SENSE_PASS_THROUGH",
          "Pass-through verified")

    # Mode 3: NL Query resolution
    q_res = agent.process_query("Why did OTA drop?", tenant_id=TENANT, period=MONTH)
    check("C5 query processing resolves cited answer",
          "On-time arrival" in q_res["answer"] and "mobility.duckdb" in q_res["citation"],
          q_res["answer"][:60] + "...")


def main():
    print("=" * 64)
    print("MoveInSync full stack smoke test  (C1 -> C2 -> C3 -> C4 -> C5)")
    print("=" * 64)
    try:
        m = Metrics(DB)
    except Exception as e:
        print(f"\nCould not open {DB}: {e}")
        print("Is the DuckDB CLI holding a write lock? Close it and retry, or")
        print("run:  python -m backend.ingest   to (re)build the database.")
        sys.exit(2)

    ce = ContextEngine(m)
    ie = InsightEngine(ce)
    agent = AgentOrchestrator(ce, ie)

    test_c1(m)
    test_c2(m)
    test_c3(ce)
    test_c4(ie)
    test_c5(agent, ie)
    m.close()

    print("\n" + "=" * 64)
    print(f"RESULT: {ok} passed, {fail} failed")
    print("=" * 64)
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
