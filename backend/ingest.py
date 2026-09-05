"""C1 — Data ingestion & normalization.

Loads the 7 raw CSVs into a DuckDB file as clean canonical tables. All the
messy-data handling lives here (the "thin adapter" of the HLD): ID/date/cost
normalization, alert-severity cleaning, tenant tagging, and a data_quality
report.

Run:  python -m app.ingest        (from the app's parent dir)
   or python app/ingest.py
"""
from __future__ import annotations
import os
import time
import duckdb

from .. import config as C


# ---- SQL normalization helpers -------------------------------------------------

def _nid(col: str) -> str:
    """Normalize an ID column: strip quotes/commas/whitespace -> canonical digits."""
    return f"NULLIF(REPLACE(REPLACE(REPLACE(TRIM(CAST({col} AS VARCHAR)), ',', ''), '\"', ''), '''', ''), '')"


def _num(col: str) -> str:
    """Parse a possibly comma/quote-formatted number -> DOUBLE (NULL if junk)."""
    return f"TRY_CAST(REPLACE(REPLACE(TRIM(CAST({col} AS VARCHAR)), ',', ''), '\"', '') AS DOUBLE)"


def _epoch_to_ts(col: str) -> str:
    """Comma-formatted unix epoch (seconds, IST-localized) -> UTC TIMESTAMP.

    Epochs in the data are wall-clock IST; subtract 5h30m to get true UTC.
    """
    secs = _num(col)
    return f"to_timestamp(({secs}) - 19800)"


def _read(con, path: str, view: str):
    con.execute(
        f"CREATE OR REPLACE VIEW {view} AS "
        f"SELECT * FROM read_csv_auto('{path}', all_varchar=true, ignore_errors=true)"
    )


# ---- Canonical table builders --------------------------------------------------

def _build_trips(con):
    parts = []
    for f in C.TRIP_FILES:
        path = os.path.join(C.DATA_DIR, f)
        parts.append(f"""
            SELECT
                {_nid('trip_id')}                       AS trip_id,
                business_unit                           AS tenant_id,
                office                                  AS office,
                UPPER(TRIM(product_type))               AS mode,
                shift_type                              AS shift_type,
                trip_direction                          AS direction,
                LOWER(TRIM(CAST(actual_escort AS VARCHAR))) IN ('true','1','yes') AS actual_escort,
                TRIM(vendor_id)                         AS vendor,
                UPPER(TRIM(actual_cab_fuel_type))       AS fuel_type,
                {_num('actual_cab_capacity')}           AS cab_capacity,
                {_num('planned_km')}                    AS planned_km,
                {_num('traveled_km')}                   AS traveled_km,
                {_epoch_to_ts('planned_start_epoch')}   AS planned_start,
                {_epoch_to_ts('planned_end_epoch')}     AS planned_end,
                {_epoch_to_ts('actual_start_epoch')}    AS actual_start,
                {_epoch_to_ts('actual_end_epoch')}      AS actual_end,
                TRIM(delay_reason)                      AS delay_reason,
                {_num('delay_minutes')}                 AS delay_min,
                {_num('plannedemployee_cnt')}           AS planned_emp_cnt,
                {_num('actualemployee_cnt')}            AS actual_emp_cnt,
                {_num('noshow_cnt')}                    AS noshow_cnt
            FROM read_csv_auto('{path}', all_varchar=true, ignore_errors=true)
        """)
    union = "\nUNION ALL\n".join(parts)
    con.execute(f"CREATE OR REPLACE TABLE trips AS SELECT * FROM ({union})")
    # dedupe on trip_id (keep first) — trip files are monthly, ids are unique per trip
    con.execute("""
        CREATE OR REPLACE TABLE trips AS
        SELECT * FROM trips
        QUALIFY row_number() OVER (PARTITION BY trip_id ORDER BY actual_start) = 1
    """)


def _build_employees(con):
    path = os.path.join(C.DATA_DIR, C.EMP_FILE)
    con.execute(f"""
        CREATE OR REPLACE TABLE employees AS
        SELECT
            {_nid('trip_id')}      AS trip_id,
            {_nid('stwid')}        AS emp_id,
            business_unit          AS tenant_id,
            office                 AS office,
            UPPER(TRIM(product_type)) AS mode,
            shift_type             AS shift_type,
            UPPER(TRIM(gender))    AS gender,
            LOWER(TRIM(emp_role))  AS emp_role,
            TRIM(boarding_status)  AS boarding_status,
            TRIM(not_boarding_reason) AS not_boarding_reason,
            LOWER(TRIM(CAST(is_no_show AS VARCHAR))) IN ('true','1','yes') AS is_no_show,
            {_epoch_to_ts('planned_pickup_epoch')} AS planned_pickup,
            {_epoch_to_ts('actual_pickup_epoch')}  AS actual_pickup
        FROM read_csv_auto('{path}', all_varchar=true, ignore_errors=true)
    """)


def _build_bills(con):
    path = os.path.join(C.DATA_DIR, C.BILL_FILE)
    con.execute(f"""
        CREATE OR REPLACE TABLE bills AS
        SELECT
            {_nid('trip_id')}   AS trip_id,
            business_unit       AS tenant_id,
            office              AS office,
            TRIM(vendor)        AS vendor,
            TRIM(contract)      AS contract,
            TRIM(slab_name)     AS slab,
            {_num('total_trip_km')} AS total_km,
            {_num('trip_cost')}     AS trip_cost_raw,
            CASE WHEN {_num('trip_cost')} BETWEEN {C.COST_MIN_INR} AND {C.COST_MAX_INR}
                 THEN {_num('trip_cost')} ELSE NULL END AS trip_cost,
            ({_num('trip_cost')} < {C.COST_MIN_INR} OR {_num('trip_cost')} > {C.COST_MAX_INR}) AS cost_quarantined
        FROM read_csv_auto('{path}', all_varchar=true, ignore_errors=true)
    """)


def _build_feedback(con):
    path = os.path.join(C.DATA_DIR, C.FEEDBACK_FILE)

    def rating(col):
        v = _num(col)
        return f"CASE WHEN {v} BETWEEN 1 AND 5 THEN {v} ELSE NULL END"

    con.execute(f"""
        CREATE OR REPLACE TABLE feedback AS
        SELECT
            {_nid('trip_id')}  AS trip_id,
            {_nid('stwid')}    AS emp_id,
            business_unit      AS tenant_id,
            TRIM(trip_type)    AS trip_type,
            {rating('route_rating')}   AS route_rating,
            {rating('driver_rating')}  AS driver_rating,
            {rating('cab_rating')}     AS cab_rating,
            {rating('safety_rating')}  AS safety_rating,
            {rating('marshal_rating')} AS marshal_rating
        FROM read_csv_auto('{path}', all_varchar=true, ignore_errors=true)
    """)


def _build_alerts(con):
    path = os.path.join(C.DATA_DIR, C.ALERTS_FILE)
    valid = ", ".join(f"'{s}'" for s in C.VALID_SEVERITIES)
    con.execute(f"""
        CREATE OR REPLACE TABLE alerts AS
        SELECT
            {_nid('trip_id')}   AS trip_id,
            {_nid('stwid')}     AS emp_id,
            business_unit       AS tenant_id,
            TRIM(event_id)      AS event_id,
            UPPER(TRIM(event_type)) AS event_type,
            TRIM(state_text)    AS state,
            TRIM(source)        AS source,
            CASE WHEN TRIM(severity) IN ({valid}) THEN TRIM(severity) ELSE NULL END AS severity,
            (TRIM(severity) NOT IN ({valid}) OR severity IS NULL) AS severity_unknown
        FROM read_csv_auto('{path}', all_varchar=true, ignore_errors=true)
    """)


def _build_vendors(con):
    """Derive the vendor master from trips; attach config SLA target."""
    con.execute(f"""
        CREATE OR REPLACE TABLE vendors AS
        SELECT vendor, tenant_id, count(*) AS trips,
               {C.DEFAULT_OTA_SLA} AS ota_sla
        FROM trips
        WHERE vendor IS NOT NULL
        GROUP BY vendor, tenant_id
    """)


def _build_data_quality(con):
    """Data-health report surfaced by the dashboard (HLD §8.3)."""
    con.execute("CREATE OR REPLACE TABLE data_quality (metric VARCHAR, tenant_id VARCHAR, value DOUBLE)")

    def ins(metric, sql):
        con.execute(f"INSERT INTO data_quality SELECT '{metric}' AS metric, tenant_id, CAST(v AS DOUBLE) FROM ({sql}) t(tenant_id, v)")

    ins("trips_total", "SELECT tenant_id, count(*) FROM trips GROUP BY 1")
    ins("trips_billed_pct", """
        SELECT t.tenant_id, 100.0*count(DISTINCT b.trip_id)/count(DISTINCT t.trip_id)
        FROM trips t LEFT JOIN bills b USING (trip_id) GROUP BY 1""")
    ins("trips_rated_pct", """
        SELECT t.tenant_id, 100.0*count(DISTINCT f.trip_id)/count(DISTINCT t.trip_id)
        FROM trips t LEFT JOIN feedback f USING (trip_id) GROUP BY 1""")
    ins("trips_with_alerts_pct", """
        SELECT t.tenant_id, 100.0*count(DISTINCT a.trip_id)/count(DISTINCT t.trip_id)
        FROM trips t LEFT JOIN alerts a USING (trip_id) GROUP BY 1""")
    ins("cost_rows_quarantined", "SELECT tenant_id, sum(CASE WHEN cost_quarantined THEN 1 ELSE 0 END) FROM bills GROUP BY 1")
    ins("alerts_severity_unknown", "SELECT tenant_id, sum(CASE WHEN severity_unknown THEN 1 ELSE 0 END) FROM alerts GROUP BY 1")


def build(db_path: str | None = None, verbose: bool = True) -> str:
    """Build the full canonical DuckDB from raw CSVs. Returns the db path."""
    db_path = db_path or os.path.join(C.DATA_DIR, "app", C.DB_PATH)
    if os.path.exists(db_path):
        os.remove(db_path)
    con = duckdb.connect(db_path)
    steps = [
        ("trips", _build_trips), ("employees", _build_employees),
        ("bills", _build_bills), ("feedback", _build_feedback),
        ("alerts", _build_alerts), ("vendors", _build_vendors),
        ("data_quality", _build_data_quality),
    ]
    for name, fn in steps:
        t0 = time.time()
        fn(con)
        if verbose:
            try:
                n = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            except Exception:
                n = "-"
            print(f"  built {name:14} rows={n:>12,}  ({time.time()-t0:.1f}s)")
    con.close()
    return db_path


if __name__ == "__main__":
    print("Ingesting MoveInSync dataset -> DuckDB ...")
    p = build()
    print(f"Done. Canonical DB at: {p}")
