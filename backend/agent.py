"""C5 — Agent Orchestration (Sense -> Reason -> Act Engine).

Consumes C4 anomaly insights and natural language queries, executes the
orchestration loop, routes to target personas, and generates pre-filled
action payloads and executive narratives.

Decoupled from C3: C5 processes C4 anomaly insights directly from the
self-contained C4 insight JSON payload (which embeds the full context,
SLA targets, trend, peer percentiles, and driver attributions).

Groq API Key is loaded automatically from server environment / .env file
(GROQ_API_KEY).

Supports a configurable reasoning mode:
- If enable_reasoning is True: Sense + Reason + Act (root cause investigation,
  driver breakdown, operational delay drilldown, Groq LLM reasoning, and audit trace).
- If enable_reasoning is False: Sense + Act (direct anomaly-to-action payload
  without detailed root cause drilldown or reasoning trace).
"""
from __future__ import annotations

import logging
from typing import Optional

from . import config as C
from .context import ContextEngine
from .insights import InsightEngine
from .metrics import Metrics

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    def __init__(self, context_engine: Optional[ContextEngine] = None,
                 insight_engine: Optional[InsightEngine] = None,
                 metrics: Optional[Metrics] = None,
                 enable_reasoning: Optional[bool] = None):
        self.context_engine = context_engine
        self.metrics = metrics or (context_engine.m if context_engine else None)
        self.insight_engine = insight_engine or (InsightEngine(context_engine) if context_engine else None)
        self.default_enable_reasoning = (
            enable_reasoning if enable_reasoning is not None else C.ENABLE_REASONING
        )
        self.groq_api_key = C.GROQ_API_KEY
        self.grok_api_key = self.groq_api_key

    def process_anomaly(self, insight: dict,
                        enable_reasoning: Optional[bool] = None) -> dict:
        """Process one self-contained C4 anomaly insight payload into an AgentResponse.

        Validates that input is a well-formed C4 payload and checks `is_anomaly`.
        If input is healthy (is_anomaly=False), action generation is safely bypassed.
        """
        if not isinstance(insight, dict) or "kpi" not in insight or "context" not in insight:
            raise ValueError("Invalid C4 insight payload: missing required 'kpi' or 'context' keys.")

        reasoning_on = (
            enable_reasoning if enable_reasoning is not None else self.default_enable_reasoning
        )
        active_groq_key = self.groq_api_key

        insight_id = insight.get("insight_id", "unknown_insight")
        kpi = insight.get("kpi", "ota")
        anomaly_type = insight.get("anomaly_type", "metric_anomaly")
        priority_score = insight.get("priority_score", 0.0)
        priority_band = insight.get("priority_band", "low")
        is_anomaly = insight.get("is_anomaly", False)
        ctx = insight.get("context", {})

        personas = C.PERSONA_ROUTING.get(priority_band, ["transport_manager"])

        # If payload is healthy (not an anomaly), bypass escalation creation
        if not is_anomaly:
            label = ctx.get("label", kpi)
            val = ctx.get("value")
            unit = ctx.get("unit", "")
            return {
                "insight_id": insight_id,
                "kpi": kpi,
                "anomaly_type": "healthy_metric",
                "priority_score": priority_score,
                "priority_band": "low",
                "reasoning_enabled": reasoning_on,
                "groq_active": bool(active_groq_key),
                "grok_active": bool(active_groq_key),
                "personas": personas,
                "reasoning_trace": [{
                    "step": "SENSE_ANOMALY",
                    "detail": f"Metric '{label}' evaluated as healthy/normal (is_anomaly=False). Action generation bypassed.",
                }],
                "executive_summary": f"[HEALTHY] {label} is operating within normal bounds ({val}{unit}). No escalation required.",
                "action_draft": {
                    "type": "no_action_required",
                    "summary": f"No escalation required. {label} is healthy.",
                    "status": "NO_ACTION_NEEDED",
                },
                "status": "NO_ACTION_NEEDED",
            }

        reasoning_trace = []
        root_cause_info = {}

        if reasoning_on:
            reasoning_trace, root_cause_info = self._reason_about_anomaly(
                insight, ctx, priority_score, priority_band, personas, active_groq_key
            )
        else:
            reasoning_trace = [{
                "step": "SENSE_PASS_THROUGH",
                "detail": f"Reasoning mode disabled. Directly routing {priority_band} anomaly ({kpi}) to action layer.",
            }]

        action_draft = self._generate_action_draft(
            insight, ctx, root_cause_info, personas, reasoning_on, active_groq_key
        )
        executive_summary = self._generate_executive_summary(
            insight, ctx, root_cause_info, reasoning_on, active_groq_key
        )

        return {
            "insight_id": insight_id,
            "kpi": kpi,
            "anomaly_type": anomaly_type,
            "priority_score": priority_score,
            "priority_band": priority_band,
            "reasoning_enabled": reasoning_on,
            "groq_active": bool(active_groq_key),
            "grok_active": bool(active_groq_key),
            "personas": personas,
            "reasoning_trace": reasoning_trace,
            "executive_summary": executive_summary,
            "action_draft": action_draft,
            "status": "PROPOSED_WAITING_APPROVAL",
        }

    def scan_and_process(self, period: str, grain: str = "month",
                         tenant_id: Optional[str] = None,
                         enable_reasoning: Optional[bool] = None) -> list[dict]:
        """Scan a period for anomalies via C4 and process each through C5."""
        if not self.insight_engine:
            raise ValueError("InsightEngine is required to perform period scan.")
        anomalies = self.insight_engine.scan_period(period, grain=grain, tenant_id=tenant_id)
        results = []
        for anomaly in anomalies:
            results.append(self.process_anomaly(
                anomaly, enable_reasoning=enable_reasoning
            ))
        return results

    def process_query(self, query: str, tenant_id: Optional[str] = None,
                      period: Optional[str] = None,
                      grain: str = "month") -> dict:
        """Resolve a natural language query over C2/C3 data with citations."""
        active_groq_key = self.groq_api_key
        q_lower = query.lower()
        filters = {"tenant_id": tenant_id} if tenant_id else {}
        focus_period = period or "2026-07"

        kpi_target = "ota"
        for candidate_kpi in C.METRIC_REGISTRY:
            if candidate_kpi in q_lower or C.METRIC_REGISTRY[candidate_kpi]["label"].lower() in q_lower:
                kpi_target = candidate_kpi
                break

        if self.context_engine:
            ctx = self.context_engine.context(kpi_target, filters=filters, period=focus_period, grain=grain)
            headline = ctx.get("headline", "")
            n = ctx.get("n", 0)
        else:
            ctx = {"kpi": kpi_target, "value": None, "n": 0}
            headline = f"KPI '{kpi_target}' query processed"
            n = 0

        trace = [
            {"step": "INTENT_PARSING", "detail": f"Mapped query to KPI '{kpi_target}'"},
            {"step": "FETCH_CONTEXT", "detail": f"Retrieved context for scope {filters}, period {focus_period} ({grain})"},
        ]

        groq_explanation = None
        if active_groq_key:
            prompt = (
                f"User Query: '{query}'\n"
                f"Exact Context Data: {headline}\n"
                f"Data Scope: {filters}, Period: {focus_period} ({grain}), Total Trips: {n:,}\n\n"
                f"Explain this mobility operations insight clearly and concisely for an executive manager."
            )
            groq_explanation = self._call_groq_api(
                prompt,
                system_prompt="You are MoveInsight Groq AI, an expert enterprise mobility intelligence assistant.",
                api_key=active_groq_key,
            )

        if groq_explanation:
            answer = f"{groq_explanation} (Context: {headline})"
            trace.append({"step": "GROQ_LLM_NARRATION", "detail": "Generated explanation using Groq model", "model": C.GROQ_MODEL})
        else:
            answer = f"{headline} (Data source: {n:,} trip records for {focus_period})."
            trace.append({"step": "DETERMINISTIC_NARRATION", "detail": f"Formulated cited answer based on n={n:,} records"})

        tenant_vendor_breakdown = self.get_tenant_vendor_breakdown(
            tenant_id=tenant_id, period=focus_period, grain=grain
        )

        return {
            "query": query,
            "kpi": kpi_target,
            "answer": answer,
            "context": ctx,
            "tenant_vendor_breakdown": tenant_vendor_breakdown,
            "reasoning_trace": trace,
            "groq_active": bool(active_groq_key and groq_explanation),
            "grok_active": bool(active_groq_key and groq_explanation),
            "citation": f"Based on {n:,} trips in canonical database mobility.duckdb",
        }

    def get_tenant_vendor_breakdown(self, tenant_id: Optional[str] = None,
                                   period: Optional[str] = None,
                                   grain: str = "month") -> dict[str, list[dict]]:
        """Return tenant-wise vendor OTA performance and trip volume breakdown."""
        if not self.metrics:
            return {}
        filters = {"tenant_id": tenant_id} if tenant_id else {}
        w, p = self.metrics._where(filters, period=period, grain=grain)
        sql = f"""
            SELECT tenant_id, vendor, count(*) AS trips,
                   round(100.0 * avg(CASE WHEN delay_min <= {C.OTA_THRESHOLD_MIN} THEN 1 ELSE 0 END), 2) AS ota_pct
            FROM trips{w}
            {'AND' if w else 'WHERE'} tenant_id IS NOT NULL AND vendor IS NOT NULL
            GROUP BY tenant_id, vendor
            ORDER BY tenant_id, trips DESC
        """
        try:
            rows = self.metrics.con.cursor().execute(sql, p).fetchall()
            result: dict[str, list[dict]] = {}
            for t_id, v_name, trips, ota_pct in rows:
                result.setdefault(t_id, []).append({
                    "vendor": v_name,
                    "trips": trips,
                    "ota_pct": ota_pct,
                })
            return result
        except Exception as exc:
            logger.warning("get_tenant_vendor_breakdown failed: %s", exc)
            return {}

    # -- Groq API Integration Helper ------------------------------------

    def _call_groq_api(self, prompt: str, system_prompt: str, api_key: str) -> Optional[str]:
        """Call Groq Cloud API using OpenAI SDK interface."""
        if not api_key:
            return None
        try:
            import openai
            client = openai.OpenAI(
                api_key=api_key,
                base_url=C.GROQ_BASE_URL,
                timeout=30.0,
            )
            if "prompt-guard" in C.GROQ_MODEL.lower():
                messages = [{"role": "user", "content": f"{system_prompt}\n{prompt}"}]
            else:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ]
            response = client.chat.completions.create(
                model=C.GROQ_MODEL,
                messages=messages,
                temperature=0.3,
                max_tokens=256,
            )
            return response.choices[0].message.content.strip()
        except Exception as exc:
            logger.warning("Groq API call failed or unauthenticated: %s", exc)
            return None

    # Alias for backward compatibility
    _call_grok_api = _call_groq_api

    # -- Internal Reasoning Helpers --------------------------------------------

    def _reason_about_anomaly(self, insight, ctx, priority_score, priority_band, personas, groq_key: str | None = None):
        trace = []
        root_cause = {}

        trace.append({
            "step": "1_SENSE_ANOMALY",
            "detail": f"Sensed {insight.get('kpi')} anomaly with priority score {priority_score} ({priority_band} band).",
            "signals": [s.get("name") for s in insight.get("signals", [])],
        })

        drivers = ctx.get("drivers_of_change", [])
        driver_dim = ctx.get("peer", {}).get("dim") or "vendor"
        root_cause["driver_dim"] = driver_dim

        if drivers:
            top_driver = drivers[0]
            root_cause["top_driver_label"] = top_driver.get("label")
            root_cause["top_driver_contrib"] = top_driver.get("contribution_pct")
            root_cause["top_driver_value"] = top_driver.get("value")
            trace.append({
                "step": "2_DRIVER_ATTRIBUTION",
                "detail": f"Primary driver ({driver_dim}) identified as '{top_driver.get('label')}' contributing {top_driver.get('contribution_pct')}% of the adverse gap.",
            })
        else:
            trace.append({
                "step": "2_DRIVER_ATTRIBUTION",
                "detail": "No single driver concentration detected; breach is distributed across scope.",
            })

        kpi = insight.get("kpi")
        filters = ctx.get("filters", {})
        period = ctx.get("period") or ctx.get("month")
        grain = ctx.get("grain", "month")

        if kpi == "ota":
            delay_reasons = self._query_delay_reasons(filters, period=period, grain=grain)
            root_cause["delay_reasons"] = delay_reasons
            if delay_reasons:
                top_reason = delay_reasons[0]
                trace.append({
                    "step": "3_OPERATIONAL_DRILLDOWN",
                    "detail": f"Top delay reason for scope is '{top_reason[0]}' accounting for {top_reason[1]:,} delayed trips.",
                })
            else:
                trace.append({
                    "step": "3_OPERATIONAL_DRILLDOWN",
                    "detail": "No specific operational delay reason recorded for scope.",
                })
        elif kpi == "safety_score":
            alert_types = self._query_safety_alert_types(filters, period=period, grain=grain)
            root_cause["alert_types"] = alert_types
            if alert_types:
                top_alert = alert_types[0]
                trace.append({
                    "step": "3_OPERATIONAL_DRILLDOWN",
                    "detail": f"Top safety alert category is '{top_alert[0]}' with {top_alert[1]:,} incidents.",
                })
            else:
                trace.append({
                    "step": "3_OPERATIONAL_DRILLDOWN",
                    "detail": "No specific safety alert category recorded for scope.",
                })
        else:
            trace.append({
                "step": "3_OPERATIONAL_DRILLDOWN",
                "detail": f"Evaluated metric movement: {ctx.get('label')} is {ctx.get('value')} {ctx.get('unit')}.",
            })

        trace.append({
            "step": "4_PERSONA_ROUTING",
            "detail": f"Selected target personas: {', '.join(personas)} based on {priority_band} severity.",
        })

        if groq_key:
            groq_prompt = (
                f"Analyze this operational anomaly in enterprise mobility:\n"
                f"- Anomaly KPI: {ctx.get('label')} ({kpi})\n"
                f"- Current Value: {ctx.get('value')}{ctx.get('unit')} (SLA Target: {ctx.get('sla', {}).get('target')})\n"
                f"- Priority Band: {priority_band} (Score: {priority_score})\n"
                f"- Driver ({driver_dim}): {root_cause.get('top_driver_label')} (Contribution: {root_cause.get('top_driver_contrib')}%)\n"
                f"- Operational Factor: {root_cause.get('delay_reasons') or root_cause.get('alert_types')}\n\n"
                f"Provide a 2-sentence expert operational reasoning analysis on why this occurred and what immediate corrective action is needed."
            )
            groq_reasoning = self._call_groq_api(
                groq_prompt,
                system_prompt="You are Groq AI, senior mobility operations intelligence reasoning agent.",
                api_key=groq_key,
            )
            if groq_reasoning:
                root_cause["groq_reasoning"] = groq_reasoning
                root_cause["grok_reasoning"] = groq_reasoning
                trace.append({
                    "step": "5_GROQ_LLM_REASONING",
                    "detail": groq_reasoning,
                    "model": C.GROQ_MODEL,
                })

        return trace, root_cause

    # Issue 2 Fix: Return empty list [] if query returns 0 rows or fails (no fake fallbacks)
    def _query_delay_reasons(self, filters: dict, period: str | None, grain: str = "month") -> list[tuple[str, int]]:
        if not self.metrics:
            return []
        try:
            w, p = self.metrics._where(filters, period=period, grain=grain)
            sql = f"""
                SELECT delay_reason, count(*) AS cnt
                FROM trips{w}
                {'AND' if w else 'WHERE'} delay_min > {C.OTA_THRESHOLD_MIN} AND delay_reason != 'NODELAY'
                GROUP BY 1 ORDER BY 2 DESC LIMIT 3
            """
            return self.metrics.con.cursor().execute(sql, p).fetchall()
        except Exception as exc:
            logger.warning("_query_delay_reasons failed: %s", exc)
            return []

    def _query_safety_alert_types(self, filters: dict, period: str | None, grain: str = "month") -> list[tuple[str, int]]:
        if not self.metrics:
            return []
        try:
            wa, pa = self.metrics._where(filters, period=period, grain=grain, alias="t")
            sql = f"""
                SELECT a.event_type, count(*) AS cnt
                FROM alerts a JOIN trips t USING (trip_id){wa}
                GROUP BY 1 ORDER BY 2 DESC LIMIT 3
            """
            return self.metrics.con.cursor().execute(sql, pa).fetchall()
        except Exception as exc:
            logger.warning("_query_safety_alert_types failed: %s", exc)
            return []

    # -- Internal Action & Narrative Generators -------------------------------

    def _generate_action_draft(self, insight: dict, ctx: dict,
                               root_cause: dict, personas: list[str],
                               reasoning_on: bool, groq_key: str | None) -> dict:
        kpi = insight.get("kpi", "ota")
        filters = ctx.get("filters", {})
        month = ctx.get("period") or ctx.get("month") or "recent period"
        value = ctx.get("value")
        unit = ctx.get("unit", "")
        n = ctx.get("n", 0)

        # Issue 1 & 4 Fix: Distinguish vendor attribution vs office attribution & dynamic action types
        driver_dim = root_cause.get("driver_dim") or ctx.get("peer", {}).get("dim") or "vendor"
        driver_label = root_cause.get("top_driver_label")

        action_type = "vendor_escalation_email"
        if kpi == "safety_score":
            action_type = "safety_incident_alert"
        elif kpi == "noshow_rate":
            action_type = "roster_compliance_notice"

        if driver_dim == "vendor" and driver_label:
            recipient = driver_label
        elif filters.get("vendor"):
            recipient = filters["vendor"]
        elif driver_dim == "office" and driver_label:
            recipient = f"{driver_label} Site Operations Team"
        else:
            recipient = "Assigned Fleet Management Team"

        if reasoning_on and driver_label:
            reason_detail = f"Primary cause: {driver_dim.title()} '{driver_label}' contributed {root_cause.get('top_driver_contrib')}% of the adverse gap."
            if "delay_reasons" in root_cause and root_cause["delay_reasons"]:
                top_r = root_cause["delay_reasons"][0]
                reason_detail += f" Key operational factor: {top_r[0]} delay ({top_r[1]:,} delayed trips)."
            elif "alert_types" in root_cause and root_cause["alert_types"]:
                top_a = root_cause["alert_types"][0]
                reason_detail += f" Key safety factor: {top_a[0]} incident ({top_a[1]:,} alerts)."
        else:
            reason_detail = f"Metric {ctx.get('label')} is currently {value}{unit} over {n:,} trips."

        groq_body = None
        if reasoning_on and groq_key:
            prompt = (
                f"Draft an urgent operational action notification ({action_type}) for recipient '{recipient}'.\n"
                f"Data: KPI {ctx.get('label')} is {value}{unit} against target SLA {ctx.get('sla', {}).get('target')}{unit}.\n"
                f"Evidence: {reason_detail}.\n"
                f"Keep it professional, formal, concise, demanding immediate corrective action within 24 hours."
            )
            groq_body = self._call_groq_api(
                prompt,
                system_prompt="You are MoveInsight Groq AI generating official enterprise operational alerts.",
                api_key=groq_key,
            )

        subject = f"URGENT: Mobility SLA & Incident Escalation - {recipient} [{month}]"
        body = groq_body or (
            f"Dear Team,\n\n"
            f"This is an automated operational escalation from the Mobility Operations System.\n\n"
            f"SCOPE & METRIC BREACH:\n"
            f"- KPI: {ctx.get('label')}\n"
            f"- Current Value: {value}{unit} (Target SLA: {ctx.get('sla', {}).get('target', 'N/A')}{unit})\n"
            f"- Total Trips Evaluated: {n:,}\n"
            f"- Scope: {filters if filters else 'All Fleet Trips'}\n\n"
            f"ANALYSIS & EVIDENCE:\n"
            f"{reason_detail}\n\n"
            f"REQUIRED ACTION:\n"
            f"Please submit a formal Corrective Action Plan (CAP) within 24 hours addressing operational compliance.\n\n"
            f"Regards,\n"
            f"Transport Operations Command Center"
        )

        return {
            "type": action_type,
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "groq_generated": bool(groq_body),
            "grok_generated": bool(groq_body),
            "evidence_attached": {
                "kpi": kpi,
                "current_value": value,
                "sla_target": ctx.get("sla", {}).get("target"),
                "sample_size": n,
                "root_cause_summary": reason_detail,
            },
            "status": "PROPOSED_WAITING_APPROVAL",
        }

    def _generate_executive_summary(self, insight: dict, ctx: dict,
                                    root_cause: dict, reasoning_on: bool,
                                    groq_key: str | None) -> str:
        label = ctx.get("label", "Metric")
        value = ctx.get("value")
        unit = ctx.get("unit", "")
        priority_band = insight.get("priority_band", "medium")

        if root_cause.get("groq_reasoning") or root_cause.get("grok_reasoning"):
            return f"[{priority_band.upper()} PRIORITY] {root_cause.get('groq_reasoning') or root_cause.get('grok_reasoning')}"

        if reasoning_on and root_cause.get("top_driver_label"):
            driver_dim = root_cause.get("driver_dim", "vendor")
            return (
                f"[{priority_band.upper()} PRIORITY] {label} degraded to {value}{unit}. "
                f"Root cause attribution pinpoints {driver_dim} '{root_cause['top_driver_label']}' "
                f"driving {root_cause['top_driver_contrib']}% of the gap. "
                f"Action payload drafted for operational review."
            )
        return (
            f"[{priority_band.upper()} PRIORITY] {label} classified as {insight.get('anomaly_type')} "
            f"with priority score {insight.get('priority_score')}. Action payload prepared."
        )
