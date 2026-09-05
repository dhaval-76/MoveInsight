"""C4 — Insight & anomaly detection.

For the current MVP, C4 consumes the C3 OTA benchmark contract:

    {kpi, tenant_id, period, grain, overall, groups[]}

It deterministically emits one anomaly insight per grouped entity whose OTA
breaches SLA. Sample size affects confidence/priority, but C4 should not hide
an SLA breach that C3 has already surfaced. C4 does not call the LLM and does
not derive KPI math itself.
"""
from __future__ import annotations

from typing import Optional

from . import config as C


class InsightEngine:
    def __init__(self, context_engine):
        self.context_engine = context_engine
        self.metrics = context_engine.m

    # -- public API ------------------------------------------------------------

    def evaluate_ota_benchmark(self, benchmark: dict) -> list[dict]:
        """Return ranked OTA SLA-breach anomalies from a C3 benchmark object."""
        if benchmark.get("kpi") != "ota":
            raise ValueError("evaluate_ota_benchmark only supports kpi='ota'.")

        insights = []
        for group in benchmark.get("groups", []):
            insight = self._evaluate_ota_group(benchmark, group)
            if insight["is_anomaly"]:
                insights.append(insight)

        if not insights:
            scoped_group = self._overall_scope_group(benchmark)
            if scoped_group is not None:
                scoped_insight = self._evaluate_ota_group(benchmark, scoped_group)
                if scoped_insight["is_anomaly"]:
                    insights.append(scoped_insight)

        insights.sort(
            key=lambda i: (i["priority_score"], i["group"].get("n") or 0),
            reverse=True,
        )
        return insights

    def evaluate_context(self, ctx: dict):
        """Compatibility entrypoint; OTA contexts now return a list of insights."""
        if ctx.get("kpi") == "ota" and "groups" in ctx:
            return self.evaluate_ota_benchmark(ctx)
        return self._evaluate_legacy_context(ctx)

    def scan_ota_period(self, tenant_id: Optional[str], period: str,
                        grain: str = "month", dimension: str = "vendor") -> list[dict]:
        """Build one C3 OTA benchmark and detect grouped SLA breaches."""
        benchmark = self.context_engine.ota_benchmark(
            {"tenant_id": tenant_id} if tenant_id else {},
            period=period,
            grain=grain,
            dimension=dimension,
        )
        return self.evaluate_ota_benchmark(benchmark)

    def scan_period(self, period: str, grain: str = "month",
                    tenant_id: Optional[str] = None,
                    kpis: Optional[list[str]] = None,
                    dimensions: Optional[list[str]] = None,
                    include_global: bool = False) -> list[dict]:
        """Scan period scopes. OTA uses the grouped C3 benchmark contract."""
        kpis = kpis or ["ota"]
        dimensions = dimensions or ["vendor"]
        insights = []

        if include_global:
            tenants = [None]
        elif tenant_id:
            tenants = [tenant_id]
        else:
            tenants = self.metrics.tenants()

        for kpi in kpis:
            if kpi != "ota":
                continue
            for tenant in tenants:
                for dimension in dimensions:
                    insights.extend(self.scan_ota_period(tenant, period, grain, dimension))

        insights.sort(
            key=lambda i: (i["priority_score"], i["group"].get("n") or 0),
            reverse=True,
        )
        return insights

    def scan_month(self, month: str, tenant_id: Optional[str] = None,
                   kpis: Optional[list[str]] = None,
                   dimensions: Optional[list[str]] = None,
                   include_global: bool = False) -> list[dict]:
        """Backward-compatible monthly scan."""
        return self.scan_period(
            month,
            grain="month",
            tenant_id=tenant_id,
            kpis=kpis,
            dimensions=dimensions,
            include_global=include_global,
        )

    # -- OTA scoring -----------------------------------------------------------

    def _overall_scope_group(self, benchmark):
        overall = benchmark.get("overall") or {}
        value = overall.get("value")
        sla = overall.get("sla")
        if value is None or sla is None:
            return None

        scope = benchmark.get("scope") or {}
        dim, name = self._scope_label(scope, benchmark.get("tenant_id"))
        gap = round(value - sla, 2)
        return {
            "dimension": dim,
            "name": name,
            "value": value,
            "n": overall.get("n"),
            "sla_gap_pts": gap,
            "breached": gap < 0,
            "scope_overall": True,
        }

    def _scope_label(self, scope, tenant_id):
        for dim in ("vendor", "office", "shift_type", "direction", "mode"):
            if scope.get(dim) is not None:
                return dim, scope[dim]
        if tenant_id is not None:
            return "tenant", tenant_id
        return "global", "all"

    def _evaluate_ota_group(self, benchmark, group):
        signals = []
        n = group.get("n") or 0
        min_n = C.C4_OTA_MIN_SAMPLE_BY_GRAIN.get(
            benchmark.get("grain"),
            C.C4_MIN_SAMPLE_SIZE,
        )

        sample_points = 10.0
        confidence = "normal"
        if n < min_n:
            sample_points = 0.0
            confidence = "low"
            signals.append({
                "name": "sample_below_confidence_floor",
                "detail": f"n={n} below {benchmark.get('grain')} confidence floor {min_n}",
                "points": 0,
                "confidence": confidence,
            })
        else:
            signals.append({
                "name": "sample_confident",
                "detail": f"n={n} meets {benchmark.get('grain')} confidence floor",
                "points": sample_points,
                "confidence": confidence,
            })

        if not group.get("breached"):
            return self._ota_result(benchmark, group, signals, sample_points, False, confidence)

        gap = abs(group.get("sla_gap_pts") or 0)
        breach_points = min(60.0, 30.0 + gap * 0.5)
        signals.append({
            "name": "sla_breach",
            "detail": f"OTA is {gap:.2f} pts below SLA",
            "points": round(breach_points, 1),
            "bad": True,
        })

        impact_points = self._ota_impact_points(n, min_n)
        signals.append({
            "name": "affected_trip_volume",
            "detail": f"{n} trips affected",
            "points": impact_points,
        })

        score = min(round(sample_points + breach_points + impact_points, 1), 100.0)
        return self._ota_result(benchmark, group, signals, score, True, confidence)

    def _ota_impact_points(self, n, min_n):
        if not n:
            return 0.0
        return round(min(30.0, 10.0 + (n / max(min_n, 1)) * 2.0), 1)

    def _ota_result(self, benchmark, group, signals, score, is_anomaly, confidence="normal"):
        return {
            "insight_id": self._ota_fingerprint(benchmark, group),
            "kpi": "ota",
            "anomaly_type": "ota_sla_breach",
            "is_anomaly": is_anomaly,
            "priority_score": score,
            "priority_band": self._priority_band(score),
            "confidence": confidence,
            "tenant_id": benchmark.get("tenant_id"),
            "period": benchmark.get("period"),
            "grain": benchmark.get("grain"),
            "overall": benchmark.get("overall"),
            "group": group,
            "signals": signals,
            "summary": self._ota_summary(benchmark, group, is_anomaly, score),
        }

    def _ota_summary(self, benchmark, group, is_anomaly, score):
        status = "anomaly" if is_anomaly else "not_anomaly"
        return (
            f"OTA for {group.get('dimension')} {group.get('name')} classified as "
            f"{status} for {benchmark.get('grain')} {benchmark.get('period')} "
            f"with priority score {score}."
        )

    def _ota_fingerprint(self, benchmark, group):
        return "|".join([
            "ota",
            str(benchmark.get("grain")),
            str(benchmark.get("period")),
            f"tenant_id={benchmark.get('tenant_id')}",
            f"{group.get('dimension')}={group.get('name')}",
        ])

    # -- legacy generic-context scoring ---------------------------------------

    def _evaluate_legacy_context(self, ctx: dict) -> dict:
        """Retain previous scorer for non-OTA contexts while OTA is rebuilt."""
        kpi = ctx["kpi"]
        rules = self._rules(kpi)
        good_up = ctx.get("good_direction") == "up"
        n = ctx.get("n") or 0

        signals = []
        score = 0.0

        if n < C.C4_MIN_SAMPLE_SIZE:
            signals.append({
                "name": "sample_too_small",
                "detail": f"n={n} below confidence floor {C.C4_MIN_SAMPLE_SIZE}",
                "points": 0,
            })
            return self._legacy_result(ctx, signals, 0.0, False)

        score += 10
        signals.append({
            "name": "sample_confident",
            "detail": f"n={n} meets confidence floor",
            "points": 10,
        })

        score += self._sla_signal(ctx, rules, good_up, signals)
        score += self._trend_signal(ctx, rules, signals)
        score += self._peer_signal(ctx, rules, signals)
        score += self._industry_signal(ctx, rules, signals)
        score += self._driver_signal(ctx, rules, signals)
        score += self._impact_signal(ctx, rules, signals)

        score = min(round(score, 1), 100.0)
        has_bad_signal = any(s.get("bad") for s in signals)
        is_anomaly = has_bad_signal and score >= C.C4_ANOMALY_SCORE_THRESHOLD
        return self._legacy_result(ctx, signals, score, is_anomaly)

    def _sla_signal(self, ctx, rules, good_up, signals):
        sla = ctx.get("sla") or {}
        gap = sla.get("gap_pts")
        if not sla.get("breached") or gap is None:
            return 0.0

        breach = abs(gap)
        threshold = rules["sla_gap_pts"]
        if breach < threshold:
            return 0.0

        points = min(30.0, 18.0 + (breach - threshold) * 3.0)
        direction = "below" if good_up else "above"
        signals.append({
            "name": "sla_breach",
            "detail": f"{breach:.2f} pts {direction} target",
            "points": round(points, 1),
            "bad": True,
        })
        return points

    def _trend_signal(self, ctx, rules, signals):
        trend = ctx.get("trend") or {}
        delta = trend.get("delta")
        if delta is None or trend.get("improving") is not False:
            return 0.0

        adverse_delta = abs(delta)
        threshold = rules["trend_delta"]
        if adverse_delta < threshold:
            return 0.0

        points = min(25.0, 14.0 + (adverse_delta - threshold) * 2.0)
        signals.append({
            "name": "adverse_trend",
            "detail": f"moved {adverse_delta:.2f} {ctx.get('unit')} in the bad direction",
            "points": round(points, 1),
            "bad": True,
        })
        return points

    def _peer_signal(self, ctx, rules, signals):
        peer = ctx.get("peer") or {}
        percentile = peer.get("percentile")
        if percentile is None or percentile > rules["peer_percentile"]:
            return 0.0

        points = min(15.0, 8.0 + (rules["peer_percentile"] - percentile) / 3.0)
        signals.append({
            "name": "weak_peer_position",
            "detail": f"{percentile}th percentile among {peer.get('dim')} peers",
            "points": round(points, 1),
            "bad": True,
        })
        return points

    def _industry_signal(self, ctx, rules, signals):
        industry = ctx.get("industry") or {}
        delta = industry.get("delta")
        if industry.get("better_than_norm") is not False or delta is None:
            return 0.0

        miss = abs(delta)
        threshold = rules["industry_delta"]
        if miss < threshold:
            return 0.0

        points = min(10.0, 5.0 + (miss - threshold))
        signals.append({
            "name": "industry_benchmark_miss",
            "detail": f"{miss:.2f} {ctx.get('unit')} away from industry norm",
            "points": round(points, 1),
            "bad": True,
        })
        return points

    def _driver_signal(self, ctx, rules, signals):
        drivers = ctx.get("drivers_of_change") or []
        if not drivers:
            return 0.0

        top = drivers[0]
        contribution = top.get("contribution_pct") or 0
        if contribution < rules["driver_contribution_pct"]:
            return 0.0

        points = min(10.0, 5.0 + (contribution - rules["driver_contribution_pct"]) / 10.0)
        signals.append({
            "name": "concentrated_driver",
            "detail": f"{top.get('label')} contributes {contribution}% of the adverse gap",
            "points": round(points, 1),
            "bad": True,
        })
        return points

    def _impact_signal(self, ctx, rules, signals):
        n = ctx.get("n") or 0
        threshold = rules["large_sample_size"]
        if n < threshold:
            return 0.0

        points = min(10.0, 5.0 + (n / threshold))
        signals.append({
            "name": "large_impact_scope",
            "detail": f"{n} records affected",
            "points": round(points, 1),
        })
        return points

    # -- formatting ------------------------------------------------------------

    def _rules(self, kpi):
        rules = dict(C.C4_RULE_DEFAULTS)
        rules.update(C.C4_RULES.get(kpi, {}))
        return rules

    def _priority_band(self, score):
        for band, threshold in C.C4_PRIORITY_BANDS.items():
            if score >= threshold:
                return band
        return "low"

    def _legacy_result(self, ctx, signals, score, is_anomaly):
        return {
            "insight_id": self._legacy_fingerprint(ctx),
            "kpi": ctx["kpi"],
            "anomaly_type": self._legacy_anomaly_type(ctx),
            "is_anomaly": is_anomaly,
            "priority_score": score,
            "priority_band": self._priority_band(score),
            "signals": signals,
            "context": ctx,
            "summary": self._legacy_summary(ctx, is_anomaly, score),
        }

    def _legacy_anomaly_type(self, ctx):
        return {
            "noshow_rate": "noshow_spike",
            "cost_per_trip": "cost_increase",
            "occupancy": "occupancy_drop",
            "co2_per_trip": "emissions_increase",
            "safety_score": "safety_alert_spike",
            "escort_compliance": "escort_compliance_drop",
            "feedback_score": "experience_drop",
        }.get(ctx["kpi"], "metric_anomaly")

    def _legacy_summary(self, ctx, is_anomaly, score):
        status = "anomaly" if is_anomaly else "not_anomaly"
        period = ctx.get("period") or ctx.get("month") or "all data"
        grain = ctx.get("grain", "month")
        return (
            f"{ctx['label']} classified as {status} for {grain} {period} "
            f"with priority score {score}."
        )

    def _legacy_fingerprint(self, ctx):
        bits = [ctx.get("kpi"), ctx.get("grain"), ctx.get("period") or ctx.get("month")]
        filters = ctx.get("filters") or {}
        for key in sorted(filters):
            bits.append(f"{key}={filters[key]}")
        return "|".join(str(bit) for bit in bits if bit is not None)
