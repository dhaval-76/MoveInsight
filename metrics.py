"""C2 — Semantic / metrics layer.

Single source of truth for KPIs. Every KPI is a named, parameterized function
with typed dimension filters — NO free-form SQL from the model (HLD §5 C2,
§8.4). The agent and the dashboard both call these functions.

Each metric returns exact numbers with the sample size (n) so answers can be
cited ("based on N trips").
"""
from __future__ import annotations
from typing import Optional
import duckdb

from . import config as C

# Whitelisted dimensions an LLM/tool is allowed to filter on.
ALLOWED_DIMS = {"tenant_id", "vendor", "office", "mode", "shift_type", "direction"}


class Metrics:
    def __init__(self, db_path: str):
        self.con = duckdb.connect(db_path, read_only=True)

    # -- internal: build a safe WHERE clause from whitelisted dims -------------
    def _where(self, filters: Optional[dict], month: Optional[str] = None,
               ts_col: str = "actual_start"):
        clauses, params = [], []
        for k, v in (filters or {}).items():
            if k not in ALLOWED_DIMS:
                raise ValueError(f"Illegal filter dimension: {k}")
            clauses.append(f"{k} = ?")
            params.append(v)
        if month:  # 'YYYY-MM'
            clauses.append(f"strftime({ts_col}, '%Y-%m') = ?")
            params.append(month)
        sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return sql, params

    def _scalar(self, sql, params):
        return self.con.execute(sql, params).fetchone()

    # -- KPIs ------------------------------------------------------------------

    def ota(self, filters=None, month=None):
        """On-time arrival: share of trips with delay <= threshold."""
        w, p = self._where(filters, month)
        row = self._scalar(f"""
            SELECT round(100.0*avg(CASE WHEN delay_min <= {C.OTA_THRESHOLD_MIN} THEN 1 ELSE 0 END), 2) AS ota_pct,
                   count(*) AS n
            FROM trips{w}""", p)
        return {"kpi": "ota_pct", "value": row[0], "n": row[1],
                "unit": "%", "threshold_min": C.OTA_THRESHOLD_MIN}

    def sla_gap(self, filters=None, month=None):
        """Signed distance of OTA from the SLA target (percentage points)."""
        o = self.ota(filters, month)
        sla = C.DEFAULT_OTA_SLA * 100
        if filters and "tenant_id" in filters:
            sla = C.TENANT_OTA_SLA.get(filters["tenant_id"], C.DEFAULT_OTA_SLA) * 100
        gap = None if o["value"] is None else round(o["value"] - sla, 2)
        return {"kpi": "sla_gap_pts", "value": gap, "ota_pct": o["value"],
                "sla_target_pct": sla, "n": o["n"], "unit": "pts"}

    def noshow_rate(self, filters=None, month=None):
        w, p = self._where(filters, month)
        row = self._scalar(f"""
            SELECT round(100.0*sum(noshow_cnt)/nullif(sum(planned_emp_cnt),0), 2) AS pct,
                   sum(noshow_cnt) AS noshows, count(*) AS n
            FROM trips{w}""", p)
        return {"kpi": "noshow_rate_pct", "value": row[0], "noshows": row[1],
                "n": row[2], "unit": "%"}

    def cost_per_trip(self, filters=None, month=None):
        # bills has no timestamp -> join to trips for time/dim filtering
        w, p = self._where(filters, month)
        row = self._scalar(f"""
            SELECT round(avg(b.trip_cost), 1) AS avg_cost, count(b.trip_cost) AS n
            FROM trips t JOIN bills b USING (trip_id){w}""", p)
        return {"kpi": "cost_per_trip_inr", "value": row[0], "n": row[1], "unit": "INR"}

    def occupancy(self, filters=None, month=None):
        w, p = self._where(filters, month)
        row = self._scalar(f"""
            SELECT round(100.0*avg(actual_emp_cnt/nullif(cab_capacity,0)), 2) AS pct, count(*) AS n
            FROM trips{w} {'AND' if w else 'WHERE'} cab_capacity > 0""", p)
        return {"kpi": "occupancy_pct", "value": row[0], "n": row[1], "unit": "%"}

    def co2_per_trip(self, filters=None, month=None):
        w, p = self._where(filters, month)
        cases = " ".join(
            f"WHEN fuel_type = '{k}' THEN {v}" for k, v in C.EMISSION_KG_PER_KM.items()
        )
        row = self._scalar(f"""
            SELECT round(avg(traveled_km * (CASE {cases} ELSE {C.DEFAULT_EMISSION_KG_PER_KM} END)), 3) AS kg,
                   count(*) AS n
            FROM trips{w}""", p)
        return {"kpi": "co2_per_trip_kg", "value": row[0], "n": row[1], "unit": "kg"}

    def safety_score(self, filters=None, month=None):
        """Safety-alert rate: serious alerts per 1000 trips (lower is better)."""
        w, p = self._where(filters, month)
        n = self._scalar(f"SELECT count(*) FROM trips{w}", p)[0]
        # alerts filtered by the same dims via join to trips
        wa, pa = self._where(filters, month)
        serious = self._scalar(f"""
            SELECT count(*) FROM alerts a JOIN trips t USING (trip_id){wa}
            {'AND' if wa else 'WHERE'} a.event_type IN
              ('PANIC_DEVICE','PANIC_MOBILE','PANIC_FIXED_DEVICE','OVER_SPEEDING',
               'WOMAN_TRAVELLING_ALONE','EMPLOYEE_GEOFENCE_VIOLATION','VEHICLE_STOPPAGE')
        """, pa)[0]
        rate = None if not n else round(1000.0 * serious / n, 2)
        return {"kpi": "safety_alerts_per_1k_trips", "value": rate,
                "serious_alerts": serious, "n": n, "unit": "per 1k"}

    def escort_compliance(self, filters=None, month=None):
        """Share of night/late trips that had an escort."""
        w, p = self._where(filters, month)
        row = self._scalar(f"""
            SELECT round(100.0*avg(CASE WHEN actual_escort THEN 1 ELSE 0 END), 2) AS pct, count(*) AS n
            FROM trips{w}
            {'AND' if w else 'WHERE'} (hour(actual_start) >= 21 OR hour(actual_start) <= 5)
        """, p)
        return {"kpi": "night_escort_compliance_pct", "value": row[0], "n": row[1], "unit": "%"}

    def feedback_score(self, filters=None, month=None):
        w, p = self._where(filters, month)
        row = self._scalar(f"""
            SELECT round(avg(f.route_rating), 3) AS route, round(avg(f.driver_rating), 3) AS driver,
                   round(avg(f.safety_rating), 3) AS safety, count(f.route_rating) AS n
            FROM trips t JOIN feedback f USING (trip_id){w}""", p)
        return {"kpi": "feedback_avg", "value": row[0], "route": row[0],
                "driver": row[1], "safety": row[2], "n": row[3], "unit": "1-5"}

    # -- peer / trend helpers (feed C3 benchmarking) ---------------------------

    def ota_by_vendor(self, tenant_id=None, month=None, min_trips=None):
        """Volume-normalized vendor ranking for peer comparison (HLD §5 C3)."""
        min_trips = C.PEER_MIN_TRIPS if min_trips is None else min_trips
        filters = {"tenant_id": tenant_id} if tenant_id else None
        w, p = self._where(filters, month)
        return self.con.execute(f"""
            SELECT vendor, count(*) AS trips,
                   round(100.0*avg(CASE WHEN delay_min <= {C.OTA_THRESHOLD_MIN} THEN 1 ELSE 0 END), 2) AS ota_pct
            FROM trips{w}
            {'AND' if w else 'WHERE'} vendor IS NOT NULL
            GROUP BY vendor HAVING count(*) >= {min_trips}
            ORDER BY ota_pct ASC
        """, p).fetchall()

    def ota_trend(self, filters=None):
        """OTA per month for trend context."""
        w, p = self._where(filters)
        return self.con.execute(f"""
            SELECT strftime(actual_start, '%Y-%m') AS month, count(*) AS n,
                   round(100.0*avg(CASE WHEN delay_min <= {C.OTA_THRESHOLD_MIN} THEN 1 ELSE 0 END), 2) AS ota_pct
            FROM trips{w}
            GROUP BY 1 ORDER BY 1
        """, p).fetchall()

    def data_health(self, tenant_id=None):
        if tenant_id:
            return self.con.execute(
                "SELECT metric, value FROM data_quality WHERE tenant_id = ? ORDER BY metric",
                [tenant_id]).fetchall()
        return self.con.execute(
            "SELECT metric, tenant_id, value FROM data_quality ORDER BY metric, tenant_id").fetchall()

    def tenants(self):
        return [r[0] for r in self.con.execute(
            "SELECT DISTINCT tenant_id FROM trips ORDER BY 1").fetchall()]

    def close(self):
        self.con.close()
