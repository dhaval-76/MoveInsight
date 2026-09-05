"""C3 — Benchmarking / context engine.

Wraps any bare KPI value from C2 with the four reference points the brief names
as the missing capability: historical trend, SLA/goal, peer comparison, and
industry norm — plus drivers-of-change attribution and a good/bad verdict.

Pure deterministic math over DuckDB. No LLM. Returns one "context object" that
every downstream consumer (dashboard badges, briefing prose, escalation
evidence, impact ranking) reads from.

Usage:
    from backend.metrics import Metrics
    from backend.context import ContextEngine
    ce = ContextEngine(Metrics("backend/mobility.duckdb"))
    ce.context("ota", {"vendor": "Pooja Mikhailov Travel"}, month="2026-07")
"""
from __future__ import annotations
from typing import Optional
from statistics import median

from . import config as C


class ContextEngine:
    def __init__(self, metrics):
        self.m = metrics

    # -- public API ------------------------------------------------------------

    def context(self, kpi: str, filters: Optional[dict] = None,
                period: Optional[str] = None, grain: str = "month",
                month: Optional[str] = None) -> dict:
        """Build the full context object for a KPI under a filter scope.

        `grain` buckets time as month | week | day; `period` is the focus bucket
        (e.g. '2026-07', '2026-W29', '2026-07-15'). `month` is accepted as a
        legacy alias for a monthly `period`.
        """
        if kpi not in C.METRIC_REGISTRY:
            raise ValueError(f"Unknown KPI: {kpi}")
        if period is None and month is not None:  # backward-compat
            period, grain = month, "month"
        reg = C.METRIC_REGISTRY[kpi]
        method = reg["method"]
        good_up = reg["good"] == "up"

        current = getattr(self.m, method)(filters, period, grain)
        value = current.get("value")

        ctx = {
            "kpi": kpi,
            "label": reg["label"],
            "unit": reg["unit"],
            "value": value,
            "n": current.get("n"),
            "filters": filters or {},
            "grain": grain,
            "period": period,
            "month": period if grain == "month" else None,  # legacy mirror
            "good_direction": reg["good"],
            "trend": self._trend(method, filters, period, grain, value, good_up),
            "sla": self._sla(reg, value, good_up),
            "peer": self._peer(method, reg, filters, period, grain, value, good_up),
            "industry": self._industry(reg, value, good_up),
            "drivers_of_change": self._drivers(method, reg, filters, period, grain, good_up),
        }
        ctx["assessment"] = self._assess(ctx, good_up)
        ctx["headline"] = self._headline(ctx)
        return ctx

    # -- 1. historical trend ---------------------------------------------------

    def _trend(self, method, filters, period, grain, value, good_up):
        fn = getattr(self.m, method)
        buckets = self.m.periods(grain, filters)      # derived from data, not hard-coded
        series = []
        for b in buckets:
            r = fn(filters, b, grain)
            series.append({"period": b, "value": r.get("value"), "n": r.get("n")})
        vals = [s["value"] for s in series if s["value"] is not None]
        moving_avg = round(sum(vals) / len(vals), 2) if vals else None

        # "last period" = the bucket before `period` (or the last full bucket)
        idx = buckets.index(period) if period in buckets else len(buckets) - 1
        last = series[idx - 1]["value"] if idx - 1 >= 0 else None
        cur = value if period else (series[-1]["value"] if series else None)
        delta = None if (cur is None or last is None) else round(cur - last, 2)
        direction = None
        if delta is not None:
            direction = "flat" if delta == 0 else ("up" if delta > 0 else "down")
        return {
            "grain": grain,
            "series": series, "moving_avg": moving_avg,
            "last_period": last, "delta": delta, "direction": direction,
            "improving": None if delta is None else ((delta > 0) == good_up or delta == 0),
        }

    # -- 2. SLA / goal ---------------------------------------------------------

    def _sla(self, reg, value, good_up):
        target = reg.get("sla")
        if target is None or value is None:
            return {"target": target, "gap_pts": None, "breached": None}
        gap = round(value - target, 2)                 # signed
        breached = gap < 0 if good_up else gap > 0     # polarity-aware
        return {"target": target, "gap_pts": gap, "breached": breached}

    # -- 3. peer comparison ----------------------------------------------------

    def _peer(self, method, reg, filters, period, grain, value, good_up):
        dim = reg["attribution_dim"]
        min_n = C.PEER_MIN_TRIPS if dim in ("vendor",) else 200
        groups = self.m.kpi_by_group(method, dim, filters, period, grain, min_n=min_n)
        if len(groups) < 2:
            return {"dim": dim, "rank": None, "total": len(groups),
                    "percentile": None, "best_in_class": None, "median": None}
        # sort best -> worst per polarity
        groups_sorted = sorted(groups, key=lambda g: g["value"], reverse=good_up)
        vals = [g["value"] for g in groups_sorted]
        best = vals[0]
        med = round(median(vals), 2)

        subject = (filters or {}).get(dim)
        rank = percentile = None
        if subject is not None:
            for i, g in enumerate(groups_sorted):
                if g["group"] == subject:
                    rank = i + 1
                    percentile = round(100 * (len(groups_sorted) - i) / len(groups_sorted))
                    break
        return {"dim": dim, "rank": rank, "total": len(groups_sorted),
                "percentile": percentile, "best_in_class": best, "median": med,
                "worst": vals[-1]}

    # -- 4. industry norm ------------------------------------------------------

    def _industry(self, reg, value, good_up):
        norm = reg.get("industry_norm")
        if norm is None or value is None:
            return {"norm": norm, "delta": None, "source": "config assumption"}
        delta = round(value - norm, 2)
        better = (delta > 0) == good_up
        return {"norm": norm, "delta": delta, "better_than_norm": better,
                "source": "config assumption"}

    # -- drivers of change (attribution) --------------------------------------

    def _drivers(self, method, reg, filters, period, grain, good_up, top=3):
        """Decompose the metric into the groups pulling it in the BAD direction.

        Each group's weighted deviation from the overall value = (n_g/N)*(v_g - V).
        These sum to ~0. Groups deviating in the bad direction are the drivers.
        Works for any weighted-average rate KPI regardless of polarity.
        """
        dim = reg["attribution_dim"]
        # don't attribute across the same dim we've already filtered to a single value
        if (filters or {}).get(dim) is not None:
            return []
        min_n = C.PEER_MIN_TRIPS if dim in ("vendor",) else 200
        groups = self.m.kpi_by_group(method, dim, filters, period, grain, min_n=min_n)
        overall = getattr(self.m, method)(filters, period, grain)
        V, N = overall.get("value"), sum(g["n"] for g in groups)
        if V is None or not N:
            return []
        contribs = []
        for g in groups:
            wdev = (g["n"] / N) * (g["value"] - V)
            # bad contribution: below-overall when up-is-good, above-overall when down-is-good
            bad = -wdev if good_up else wdev
            if bad > 0:
                contribs.append({"dimension": dim, "label": g["group"],
                                 "value": g["value"], "n": g["n"], "_bad": bad})
        total_bad = sum(c["_bad"] for c in contribs) or 1
        contribs.sort(key=lambda c: c["_bad"], reverse=True)
        out = []
        for c in contribs[:top]:
            out.append({"dimension": c["dimension"], "label": c["label"],
                        "value": c["value"], "n": c["n"],
                        "contribution_pct": round(100 * c["_bad"] / total_bad, 1)})
        return out

    # -- verdict + headline ----------------------------------------------------

    def _assess(self, ctx, good_up):
        flags = []
        if ctx["sla"].get("breached"):
            flags.append("sla_breached")
        t = ctx["trend"]
        if t.get("improving") is False and t.get("delta"):
            flags.append("declining")
        p = ctx["peer"]
        if p.get("percentile") is not None and p["percentile"] <= 25:
            flags.append("bottom_quartile_peer")
        ind = ctx["industry"]
        if ind.get("better_than_norm") is False:
            flags.append("below_industry_norm")
        if not flags:
            return "healthy"
        return "|".join(flags)

    def _headline(self, ctx):
        """One-line human summary — numbers only, safe for the LLM to reuse verbatim."""
        v, u = ctx["value"], ctx["unit"]
        if v is None:
            return f"{ctx['label']}: no data for this scope."
        parts = [f"{ctx['label']} {v}{'' if u in ('INR','1-5','per 1k') else u}"]
        t = ctx["trend"]
        if t.get("delta") is not None and t.get("last_period") is not None:
            arrow = "up" if t["delta"] > 0 else ("down" if t["delta"] < 0 else "flat")
            unit = t.get("grain", "month")
            parts.append(f"{arrow} from {t['last_period']} last {unit}")
        s = ctx["sla"]
        if s.get("gap_pts") is not None:
            rel = "above" if s["gap_pts"] >= 0 else "below"
            parts.append(f"{abs(s['gap_pts'])}pts {rel} SLA ({s['target']})")
        p = ctx["peer"]
        if p.get("rank") is not None:
            parts.append(f"ranked {p['rank']} of {p['total']} {p['dim']}s")
        d = ctx["drivers_of_change"]
        if d:
            top = d[0]
            parts.append(f"{top['label']} drives {top['contribution_pct']}% of the gap")
        return "; ".join(parts) + "."
