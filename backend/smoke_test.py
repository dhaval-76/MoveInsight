"""End-to-end smoke test for the deterministic stack: C1 -> C2 -> C3.

Run from the project root:
    python -m backend.smoke_test

Verifies:
  C1  the canonical DuckDB exists and the 7 tables are populated & joinable.
  C2  every whitelisted KPI returns a sane value; the safe-query guard blocks
      illegal dimensions.
  C3  the context engine produces the grouped OTA benchmark contract and keeps
      the generic benchmark object healthy for the other KPIs.

Exits non-zero on the first hard failure so it can gate a demo.
"""
from __future__ import annotations
import sys

from .metrics import Metrics, ALLOWED_DIMS
from .context import ContextEngine
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
    # join integrity: trips -> employees on normalized trip_id
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

    # peer helper feeds C3
    grp = m.kpi_by_group("ota", "vendor", {"tenant_id": TENANT}, MONTH, min_n=2000)
    check("kpi_by_group returns >=2 vendors", len(grp) >= 2, f"{len(grp)} vendors")

    # SAFETY: illegal dimension must be rejected
    try:
        m.ota({"driver_name": "x"})
        check("safe-query guard blocks illegal dim", False, "no error raised!")
    except ValueError:
        check("safe-query guard blocks illegal dim", True, "ValueError raised")


# --------------------------------------------------------------------------- C3
REQUIRED_KEYS = {"kpi", "label", "value", "trend", "sla", "peer", "industry",
                 "drivers_of_change", "assessment", "headline"}
OTA_REQUIRED_KEYS = {"kpi", "tenant_id", "period", "grain", "overall", "groups"}


def test_c3(ce: ContextEngine):
    print("\nC3  context engine (benchmarking)")
    for kpi in C.METRIC_REGISTRY:
        ctx = ce.context(kpi, {"tenant_id": TENANT}, MONTH)
        required = OTA_REQUIRED_KEYS if kpi == "ota" else REQUIRED_KEYS
        missing = required - set(ctx)
        if kpi == "ota":
            good = not missing and isinstance(ctx["groups"], list) \
                and len(ctx["groups"]) >= 1 and "sla_gap_pts" in ctx["groups"][0]
        else:
            good = not missing and isinstance(ctx["trend"]["series"], list) \
                and len(ctx["trend"]["series"]) == len(C.DATA_MONTHS) \
                and isinstance(ctx["headline"], str) and ctx["headline"].endswith(".")
        check(f"context('{kpi}') well-formed", good,
              (f"missing {missing}" if missing else "ok"))

    # spotlight one full object
    print("\n  sample OTA benchmark (whole tenant):")
    ota = ce.context("ota", {"tenant_id": TENANT}, MONTH)
    print("   ", f"overall={ota['overall']['value']}%, groups={len(ota['groups'])}")

    check("OTA benchmark returns all eligible vendor groups", len(ota["groups"]) >= 10,
          f"{len(ota['groups'])} groups")

    # weekly grain gives a richer trend series derived from the data
    wk = ce.context("ota", {"tenant_id": TENANT}, period="2026-W29", grain="week")
    check("weekly grain returns grouped benchmark",
          wk["grain"] == "week" and len(wk["groups"]) >= 1,
          f"{len(wk['groups'])} weekly groups")


def main():
    print("=" * 64)
    print("MoveInSync deterministic stack smoke test  (C1 -> C2 -> C3)")
    print("=" * 64)
    try:
        m = Metrics(DB)
    except Exception as e:
        print(f"\nCould not open {DB}: {e}")
        print("Is the DuckDB CLI holding a write lock? Close it and retry, or")
        print("run:  python -m backend.ingest   to (re)build the database.")
        sys.exit(2)

    ce = ContextEngine(m)
    test_c1(m)
    test_c2(m)
    test_c3(ce)
    m.close()

    print("\n" + "=" * 64)
    print(f"RESULT: {ok} passed, {fail} failed")
    print("=" * 64)
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
