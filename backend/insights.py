"""C4 — Insight & anomaly detection.

Consumes C3 context objects and applies deterministic rules to detect KPI
breaches, adverse movement, weak peer position, benchmark misses, and driver
concentration. The output is a ranked insight object for C5/C6/C7 to reason
over and narrate.
"""
from __future__ import annotations

from typing import Optional

from . import config as C


class InsightEngine:
    def __init__(self, context_engine):
        self.context_engine = context_engine
        self.metrics = context_engine.m

    # -- public API ------------------------------------------------------------

    def evaluate_context(self, ctx: dict) -> dict | list[dict]:
        """Classify grouped OTA benchmarks or legacy KPI contexts."""
        if ctx.get("kpi") == "ota" and "groups" in ctx:
            return self.evaluate_ota_benchmark(ctx)

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
            return self._result(ctx, signals, 0.0, False)

        score += 10
        signals.append({
            "name": "sample_confident",
            "detail": f"n={n} meets confidence floor",
            "points": 10,
        })

        score += self._sla_signal(ctx, rules, good_up, signals)
        score += self._trend_signal(ctx, rules, good_up, signals)
        score += self._peer_signal(ctx, rules, signals)
        score += self._industry_signal(ctx, rules, signals)
        score += self._driver_signal(ctx, rules, signals)
        score += self._impact_signal(ctx, rules, signals)

        score = min(round(score, 1), 100.0)
        has_bad_signal = any(s.get("bad") for s in signals)
        is_anomaly = has_bad_signal and score >= C.C4_ANOMALY_SCORE_THRESHOLD
        return self._result(ctx, signals, score, is_anomaly)

    def evaluate_ota_benchmark(self, benchmark: dict) -> list[dict]:
        """Return ranked OTA anomalies for all groups in a C3 benchmark."""
        if benchmark.get("kpi") != "ota":
            raise ValueError("evaluate_ota_benchmark only supports kpi='ota'.")

        insights = [
            self._evaluate_ota_group(benchmark, group)
            for group in benchmark.get("groups", [])
        ]
        if not any(item["is_anomaly"] for item in insights):
            overall = self._overall_scope_group(benchmark)
            if overall:
                insights.append(self._evaluate_ota_group(benchmark, overall))
        return sorted(
            [item for item in insights if item["is_anomaly"]],
            key=lambda item: (item["priority_score"], item.get("context", {}).get("n", 0)),
            reverse=True,
        )

    def scan_ota_period(
        self,
        tenant_id: Optional[str],
        period: str,
        grain: str = "month",
        dimension: str = "vendor",
    ) -> list[dict]:
        """Scan grouped OTA benchmark data for one tenant and dimension."""
        filters = {"tenant_id": tenant_id} if tenant_id else {}
        return self.evaluate_ota_benchmark(
            self.context_engine.ota_benchmark(filters, period, grain, dimension)
        )

    def scan_period(self, period: str, grain: str = "month",
                    tenant_id: Optional[str] = None,
                    kpis: Optional[list[str]] = None,
                    dimensions: Optional[list[str]] = None,
                    include_global: bool = False) -> list[dict]:
        """Scan tenant and dimension scopes for a period; return ranked anomalies."""
        kpis = list(C.METRIC_REGISTRY) if kpis is None else kpis
        dimensions = ["vendor", "office"] if dimensions is None else dimensions
        scopes = self._period_scopes(period, grain, tenant_id, dimensions, include_global)

        insights = []
        for scope in scopes:
            for kpi in kpis:
                ctx = self.context_engine.context(
                    kpi, scope["filters"], period=period, grain=grain
                )
                evaluated = self.evaluate_context(ctx)
                evaluations = evaluated if isinstance(evaluated, list) else [evaluated]
                for item in evaluations:
                    if item["is_anomaly"]:
                        item["scope_type"] = scope["type"]
                        insights.append(item)

        insights.sort(
            key=lambda i: (i["priority_score"], self._insight_sample_size(i)),
            reverse=True,
        )
        return insights

    @staticmethod
    def _insight_sample_size(insight: dict) -> int:
        return (insight.get("context") or {}).get("n") or (insight.get("group") or {}).get("n") or 0

    def _overall_scope_group(self, benchmark: dict) -> dict | None:
        overall = benchmark.get("overall") or {}
        value, sla = overall.get("value"), overall.get("sla")
        if value is None or sla is None:
            return None
        gap = round(value - sla, 2)
        scope = benchmark.get("scope") or {}
        dimension = "tenant" if benchmark.get("tenant_id") else "global"
        name = benchmark.get("tenant_id") or "all"
        for candidate in ("vendor", "office", "shift_type", "direction", "mode"):
            if scope.get(candidate) is not None:
                dimension, name = candidate, scope[candidate]
                break
        return {
            "dimension": dimension,
            "name": name,
            "value": value,
            "n": overall.get("n"),
            "sla_gap_pts": gap,
            "breached": gap < 0,
            "scope_overall": True,
        }

    def _evaluate_ota_group(self, benchmark: dict, group: dict) -> dict:
        n = group.get("n") or 0
        minimum = C.C4_OTA_MIN_SAMPLE_BY_GRAIN.get(
            benchmark.get("grain"), C.C4_MIN_SAMPLE_SIZE
        )
        signals = [{
            "name": "sample_confident" if n >= minimum else "sample_below_confidence_floor",
            "detail": f"n={n} for {benchmark.get('grain')} confidence floor {minimum}",
            "points": 10.0 if n >= minimum else 0.0,
        }]
        if not group.get("breached"):
            return self._ota_result(benchmark, group, signals, 0.0, False)

        gap = abs(group.get("sla_gap_pts") or 0)
        signals.append({
            "name": "sla_breach",
            "detail": f"OTA is {gap:.2f} pts below SLA",
            "points": round(min(60.0, 30.0 + gap * 0.5), 1),
            "bad": True,
        })
        score = min(round(signals[0]["points"] + signals[1]["points"] + min(30.0, 10.0 + n / max(minimum, 1) * 2.0), 1), 100.0)
        signals.append({"name": "affected_trip_volume", "detail": f"{n} trips affected", "points": round(min(30.0, 10.0 + n / max(minimum, 1) * 2.0), 1)})
        return self._ota_result(benchmark, group, signals, score, True)

    def _ota_result(self, benchmark: dict, group: dict, signals: list[dict], score: float, is_anomaly: bool) -> dict:
        filters = benchmark.get("scope") or {}
        context = {
            "kpi": "ota", "label": "On-time arrival", "unit": "%",
            "value": group.get("value"), "n": group.get("n"),
            "filters": filters, "period": benchmark.get("period"),
            "grain": benchmark.get("grain"),
            "good_direction": "up",
            "sla": {"target": (benchmark.get("overall") or {}).get("sla"), "gap_pts": group.get("sla_gap_pts"), "breached": group.get("breached")},
            "groups": benchmark.get("groups", []),
        }
        return {
            "insight_id": "|".join(["ota", str(benchmark.get("grain")), str(benchmark.get("period")), f"tenant_id={benchmark.get('tenant_id')}", f"{group.get('dimension')}={group.get('name')}"]),
            "kpi": "ota", "anomaly_type": "ota_sla_breach", "is_anomaly": is_anomaly,
            "priority_score": score, "priority_band": self._priority_band(score),
            "confidence": "normal" if (group.get("n") or 0) >= C.C4_OTA_MIN_SAMPLE_BY_GRAIN.get(benchmark.get("grain"), C.C4_MIN_SAMPLE_SIZE) else "low",
            "tenant_id": benchmark.get("tenant_id"), "period": benchmark.get("period"),
            "grain": benchmark.get("grain"), "group": group, "signals": signals,
            "context": context,
            "summary": f"OTA for {group.get('dimension')} {group.get('name')} classified as {'anomaly' if is_anomaly else 'not_anomaly'} for {benchmark.get('grain')} {benchmark.get('period')} with priority score {score}.",
        }

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

    # -- signal rules ----------------------------------------------------------

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

    def _trend_signal(self, ctx, rules, good_up, signals):
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

    # -- scope generation ------------------------------------------------------

    def _period_scopes(self, period, grain, tenant_id, dimensions, include_global):
        scopes = []
        if include_global:
            scopes.append({"type": "global", "filters": {}})

        tenants = [tenant_id] if tenant_id else self.metrics.tenants()
        for tenant in tenants:
            tenant_filters = {"tenant_id": tenant}
            scopes.append({"type": "tenant", "filters": tenant_filters})
            for dim in dimensions:
                for value in self.metrics.distinct(dim, tenant_filters, period, grain):
                    filters = dict(tenant_filters)
                    filters[dim] = value
                    scopes.append({"type": dim, "filters": filters})
        return scopes

    # -- formatting ------------------------------------------------------------

    def _rules(self, kpi):
        rules = dict(C.C4_RULE_DEFAULTS)
        rules.update(C.C4_RULES.get(kpi, {}))
        return rules

    def _result(self, ctx, signals, score, is_anomaly):
        return {
            "insight_id": self._fingerprint(ctx),
            "kpi": ctx["kpi"],
            "anomaly_type": self._anomaly_type(ctx),
            "is_anomaly": is_anomaly,
            "priority_score": score,
            "priority_band": self._priority_band(score),
            "signals": signals,
            "context": ctx,
            "summary": self._summary(ctx, is_anomaly, score),
        }

    def _priority_band(self, score):
        for band, threshold in C.C4_PRIORITY_BANDS.items():
            if score >= threshold:
                return band
        return "low"

    def _anomaly_type(self, ctx):
        return {
            "ota": "ota_degradation",
            "noshow_rate": "noshow_spike",
            "cost_per_trip": "cost_increase",
            "occupancy": "occupancy_drop",
            "co2_per_trip": "emissions_increase",
            "safety_score": "safety_alert_spike",
            "escort_compliance": "escort_compliance_drop",
            "feedback_score": "experience_drop",
        }.get(ctx["kpi"], "metric_anomaly")

    def _summary(self, ctx, is_anomaly, score):
        status = "anomaly" if is_anomaly else "not_anomaly"
        period = ctx.get("period") or ctx.get("month") or "all data"
        grain = ctx.get("grain", "month")
        return (
            f"{ctx['label']} classified as {status} for {grain} {period} "
            f"with priority score {score}."
        )

    def _fingerprint(self, ctx):
        bits = [ctx.get("kpi"), ctx.get("grain"), ctx.get("period") or ctx.get("month")]
        filters = ctx.get("filters") or {}
        for key in sorted(filters):
            bits.append(f"{key}={filters[key]}")
        return "|".join(str(bit) for bit in bits if bit is not None)
