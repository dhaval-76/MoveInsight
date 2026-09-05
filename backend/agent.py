"""C5 — Agent Orchestration (Sense -> Reason -> Act Engine).

Consumes C4 anomaly insights and natural language queries, executes the
orchestration loop, routes to target personas, and generates pre-filled
action payloads and executive narratives.

Decoupled from C3: C5 processes C4 anomaly insights directly from the
self-contained C4 insight JSON payload (which embeds the full context,
SLA targets, trend, peer percentiles, and driver attributions).

Supports Grok (xAI API) for LLM reasoning and executive narration, with
graceful deterministic fallback when offline or without an API key.

Supports a configurable reasoning mode:
- If enable_reasoning is True: Sense + Reason + Act (root cause investigation,
  driver breakdown, operational delay drilldown, Grok LLM reasoning, and audit trace).
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
                 enable_reasoning: Optional[bool] = None,
                 grok_api_key: Optional[str] = None):
        self.context_engine = context_engine
        self.metrics = metrics or (context_engine.m if context_engine else None)
        self.insight_engine = insight_engine or (InsightEngine(context_engine) if context_engine else None)
        self.default_enable_reasoning = (
            enable_reasoning if enable_reasoning is not None else C.ENABLE_REASONING
        )
        self.grok_api_key = grok_api_key or C.GROK_API_KEY

    def process_anomaly(self, insight: dict,
                        enable_reasoning: Optional[bool] = None,
                        grok_api_key: Optional[str] = None) -> dict:
        """Process one self-contained C4 anomaly insight payload into an AgentResponse.

        Does NOT depend on C3: all context, signals, priority scores, SLA targets,
        trend deltas, peer ranks, and driver attributions are read directly from
        the C4 insight payload.
        """
        reasoning_on = (
            enable_reasoning if enable_reasoning is not None else self.default_enable_reasoning
        )
        active_grok_key = grok_api_key or self.grok_api_key

        insight_id = insight.get("insight_id", "unknown_insight")
        kpi = insight.get("kpi", "ota")
        anomaly_type = insight.get("anomaly_type", "metric_anomaly")
        priority_score = insight.get("priority_score", 0.0)
        priority_band = insight.get("priority_band", "low")
        signals = insight.get("signals", [])
        ctx = insight.get("context", {})

        personas = C.PERSONA_ROUTING.get(priority_band, ["transport_manager"])

        reasoning_trace = []
        root_cause_info = {}

        if reasoning_on:
            reasoning_trace, root_cause_info = self._reason_about_anomaly(
                insight, ctx, priority_score, priority_band, personas, active_grok_key
            )
        else:
            reasoning_trace = [{
                "step": "SENSE_PASS_THROUGH",
                "detail": f"Reasoning mode disabled. Directly routing {priority_band} anomaly ({kpi}) to action layer.",
            }]

        action_draft = self._generate_action_draft(
            insight, ctx, root_cause_info, personas, reasoning_on, active_grok_key
        )
        executive_summary = self._generate_executive_summary(
            insight, ctx, root_cause_info, reasoning_on, active_grok_key
        )

        return {
            "insight_id": insight_id,
            "kpi": kpi,
            "anomaly_type": anomaly_type,
            "priority_score": priority_score,
            "priority_band": priority_band,
            "reasoning_enabled": reasoning_on,
            "grok_active": bool(active_grok_key),
            "personas": personas,
            "reasoning_trace": reasoning_trace,
            "executive_summary": executive_summary,
            "action_draft": action_draft,
            "status": "PROPOSED_WAITING_APPROVAL",
        }

    def scan_and_process(self, period: str, grain: str = "month",
                         tenant_id: Optional[str] = None,
                         enable_reasoning: Optional[bool] = None,
                         grok_api_key: Optional[str] = None) -> list[dict]:
        """Scan a period for anomalies via C4 and process each through C5."""
        if not self.insight_engine:
            raise ValueError("InsightEngine is required to perform period scan.")
        anomalies = self.insight_engine.scan_period(period, grain=grain, tenant_id=tenant_id)
        results = []
        for anomaly in anomalies:
            results.append(self.process_anomaly(
                anomaly, enable_reasoning=enable_reasoning, grok_api_key=grok_api_key
            ))
        return results

    def process_query(self, query: str, tenant_id: Optional[str] = None,
                      month: str = "2026-07",
                      grok_api_key: Optional[str] = None) -> dict:
        """Resolve a natural language query over C2/C3 data with citations."""
        active_grok_key = grok_api_key or self.grok_api_key
        q_lower = query.lower()
        filters = {"tenant_id": tenant_id} if tenant_id else {}

        kpi_target = "ota"
        for candidate_kpi in C.METRIC_REGISTRY:
            if candidate_kpi in q_lower or C.METRIC_REGISTRY[candidate_kpi]["label"].lower() in q_lower:
                kpi_target = candidate_kpi
                break

        if self.context_engine:
            ctx = self.context_engine.context(kpi_target, filters=filters, month=month)
            headline = ctx.get("headline", "")
            n = ctx.get("n", 0)
        else:
            ctx = {"kpi": kpi_target, "value": None, "n": 0}
            headline = f"KPI '{kpi_target}' query processed"
            n = 0

        trace = [
            {"step": "INTENT_PARSING", "detail": f"Mapped query to KPI '{kpi_target}'"},
            {"step": "FETCH_CONTEXT", "detail": f"Retrieved context for scope {filters}"},
        ]

        grok_explanation = None
        if active_grok_key:
            prompt = (
                f"User Query: '{query}'\n"
                f"Exact Context Data: {headline}\n"
                f"Data Scope: {filters}, Month: {month}, Total Trips: {n:,}\n\n"
                f"Explain this mobility operations insight clearly and concisely for an executive manager."
            )
            grok_explanation = self._call_grok_api(
                prompt,
                system_prompt="You are MoveInsight Grok AI, an expert enterprise mobility intelligence assistant.",
                api_key=active_grok_key,
            )

        if grok_explanation:
            answer = f"{grok_explanation} (Context: {headline})"
            trace.append({"step": "GROK_LLM_NARRATION", "detail": "Generated explanation using Grok (xAI) model", "model": C.GROK_MODEL})
        else:
            answer = f"{headline} (Data source: {n:,} trip records for {month})."
            trace.append({"step": "DETERMINISTIC_NARRATION", "detail": f"Formulated cited answer based on n={n:,} records"})

        return {
            "query": query,
            "kpi": kpi_target,
            "answer": answer,
            "context": ctx,
            "reasoning_trace": trace,
            "grok_active": bool(active_grok_key and grok_explanation),
            "citation": f"Based on {n:,} trips in canonical database mobility.duckdb",
        }

    # -- Grok (xAI API) Integration Helper ------------------------------------

    def _call_grok_api(self, prompt: str, system_prompt: str, api_key: str) -> Optional[str]:
        """Call Grok (xAI) API using OpenAI SDK interface."""
        if not api_key:
            return None
        try:
            import openai
            client = openai.OpenAI(
                api_key=api_key,
                base_url=C.GROK_BASE_URL,
            )
            response = client.chat.completions.create(
                model=C.GROK_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=600,
            )
            return response.choices[0].message.content.strip()
        except Exception as exc:
            logger.warning("Grok API call failed or unauthenticated: %s", exc)
            return None

    # -- Internal Reasoning Helpers --------------------------------------------

    def _reason_about_anomaly(self, insight, ctx, priority_score, priority_band, personas, grok_key):
        trace = []
        root_cause = {}

        trace.append({
            "step": "1_SENSE_ANOMALY",
            "detail": f"Sensed {insight.get('kpi')} anomaly with priority score {priority_score} ({priority_band} band).",
            "signals": [s.get("name") for s in insight.get("signals", [])],
        })

        # All driver attribution comes directly from C4 insight payload's embedded context
        drivers = ctx.get("drivers_of_change", [])
        if drivers:
            top_driver = drivers[0]
            root_cause["top_driver_label"] = top_driver.get("label")
            root_cause["top_driver_contrib"] = top_driver.get("contribution_pct")
            root_cause["top_driver_value"] = top_driver.get("value")
            trace.append({
                "step": "2_DRIVER_ATTRIBUTION",
                "detail": f"Primary driver identified as '{top_driver.get('label')}' contributing {top_driver.get('contribution_pct')}% of the adverse gap.",
            })
        else:
            trace.append({
                "step": "2_DRIVER_ATTRIBUTION",
                "detail": "No single driver concentration detected; breach is distributed across scope.",
            })

        kpi = insight.get("kpi")
        filters = ctx.get("filters", {})
        month = ctx.get("month") or ctx.get("period")

        if kpi == "ota":
            delay_reasons = self._query_delay_reasons(filters, month)
            root_cause["delay_reasons"] = delay_reasons
            top_reason = delay_reasons[0] if delay_reasons else ("UNKNOWN", 0)
            trace.append({
                "step": "3_OPERATIONAL_DRILLDOWN",
                "detail": f"Top delay reason for scope is '{top_reason[0]}' accounting for {top_reason[1]:,} delayed trips.",
            })
        elif kpi == "safety_score":
            alert_types = self._query_safety_alert_types(filters, month)
            root_cause["alert_types"] = alert_types
            top_alert = alert_types[0] if alert_types else ("UNKNOWN", 0)
            trace.append({
                "step": "3_OPERATIONAL_DRILLDOWN",
                "detail": f"Top safety alert category is '{top_alert[0]}' with {top_alert[1]:,} incidents.",
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

        # Grok LLM Reasoning Layer
        if grok_key:
            grok_prompt = (
                f"Analyze this operational anomaly in enterprise mobility:\n"
                f"- Anomaly KPI: {ctx.get('label')} ({kpi})\n"
                f"- Current Value: {ctx.get('value')}{ctx.get('unit')} (SLA Target: {ctx.get('sla', {}).get('target')})\n"
                f"- Priority Band: {priority_band} (Score: {priority_score})\n"
                f"- Top Driver Vendor: {root_cause.get('top_driver_label')} (Contribution: {root_cause.get('top_driver_contrib')}%)\n"
                f"- Operational Factor: {root_cause.get('delay_reasons') or root_cause.get('alert_types')}\n\n"
                f"Provide a 2-sentence expert operational reasoning analysis on why this occurred and what immediate corrective action is needed."
            )
            grok_reasoning = self._call_grok_api(
                grok_prompt,
                system_prompt="You are Grok AI, senior mobility operations intelligence reasoning agent.",
                api_key=grok_key,
            )
            if grok_reasoning:
                root_cause["grok_reasoning"] = grok_reasoning
                trace.append({
                    "step": "5_GROK_LLM_REASONING",
                    "detail": grok_reasoning,
                    "model": C.GROK_MODEL,
                })

        return trace, root_cause

    def _query_delay_reasons(self, filters: dict, month: str | None) -> list[tuple[str, int]]:
        if not self.metrics:
            return [("DRIVER", 0)]
        try:
            w, p = self.metrics._where(filters, month)
            sql = f"""
                SELECT delay_reason, count(*) AS cnt
                FROM trips{w}
                {'AND' if w else 'WHERE'} delay_min > {C.OTA_THRESHOLD_MIN} AND delay_reason != 'NODELAY'
                GROUP BY 1 ORDER BY 2 DESC LIMIT 3
            """
            return self.metrics.con.execute(sql, p).fetchall()
        except Exception:
            return [("DRIVER", 0)]

    def _query_safety_alert_types(self, filters: dict, month: str | None) -> list[tuple[str, int]]:
        if not self.metrics:
            return [("PANIC_DEVICE", 0)]
        try:
            wa, pa = self.metrics._where(filters, month, alias="t")
            sql = f"""
                SELECT a.event_type, count(*) AS cnt
                FROM alerts a JOIN trips t USING (trip_id){wa}
                GROUP BY 1 ORDER BY 2 DESC LIMIT 3
            """
            return self.metrics.con.execute(sql, pa).fetchall()
        except Exception:
            return [("PANIC_DEVICE", 0)]

    # -- Internal Action & Narrative Generators -------------------------------

    def _generate_action_draft(self, insight: dict, ctx: dict,
                               root_cause: dict, personas: list[str],
                               reasoning_on: bool, grok_key: str | None) -> dict:
        kpi = insight.get("kpi", "ota")
        filters = ctx.get("filters", {})
        month = ctx.get("month") or ctx.get("period") or "recent period"
        value = ctx.get("value")
        unit = ctx.get("unit", "")
        n = ctx.get("n", 0)

        target_vendor = root_cause.get("top_driver_label") or filters.get("vendor") or "Assigned Transport Vendor"

        if reasoning_on and root_cause.get("top_driver_label"):
            reason_detail = f"Primary cause: {root_cause.get('top_driver_label')} contributed {root_cause.get('top_driver_contrib')}% of the adverse gap."
            if "delay_reasons" in root_cause and root_cause["delay_reasons"]:
                top_r = root_cause["delay_reasons"][0]
                reason_detail += f" Key operational factor: {top_r[0]} delay ({top_r[1]:,} delayed trips)."
        else:
            reason_detail = f"Metric {ctx.get('label')} is currently {value}{unit} over {n:,} trips."

        grok_body = None
        if grok_key:
            prompt = (
                f"Draft an urgent vendor escalation email for transport vendor '{target_vendor}'.\n"
                f"Data: KPI {ctx.get('label')} is {value}{unit} against target SLA {ctx.get('sla', {}).get('target')}{unit}.\n"
                f"Evidence: {reason_detail}.\n"
                f"Keep it professional, formal, concise, demanding a Corrective Action Plan (CAP) within 24 hours."
            )
            grok_body = self._call_grok_api(
                prompt,
                system_prompt="You are MoveInsight Grok AI generating official enterprise vendor escalation emails.",
                api_key=grok_key,
            )

        subject = f"URGENT: SLA Breach & Performance Escalation - {target_vendor} [{month}]"
        body = grok_body or (
            f"Dear Service Delivery Team,\n\n"
            f"This is an automated operational escalation from the Mobility Management System.\n\n"
            f"SCOPE & METRIC BREACH:\n"
            f"- KPI: {ctx.get('label')}\n"
            f"- Current Value: {value}{unit} (Target SLA: {ctx.get('sla', {}).get('target', 'N/A')}{unit})\n"
            f"- Total Trips Evaluated: {n:,}\n"
            f"- Scope: {filters if filters else 'All Fleet Trips'}\n\n"
            f"ANALYSIS & EVIDENCE:\n"
            f"{reason_detail}\n\n"
            f"REQUIRED ACTION:\n"
            f"Please submit a formal Corrective Action Plan (CAP) within 24 hours addressing driver allocation and route adherence.\n\n"
            f"Regards,\n"
            f"Transport Operations Command Center"
        )

        return {
            "type": "vendor_escalation_email",
            "recipient": target_vendor,
            "subject": subject,
            "body": body,
            "grok_generated": bool(grok_body),
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
                                    grok_key: str | None) -> str:
        label = ctx.get("label", "Metric")
        value = ctx.get("value")
        unit = ctx.get("unit", "")
        priority_band = insight.get("priority_band", "medium")

        if root_cause.get("grok_reasoning"):
            return f"[{priority_band.upper()} PRIORITY] {root_cause['grok_reasoning']}"

        if reasoning_on and root_cause.get("top_driver_label"):
            return (
                f"[{priority_band.upper()} PRIORITY] {label} degraded to {value}{unit}. "
                f"Root cause attribution pinpoints vendor '{root_cause['top_driver_label']}' "
                f"driving {root_cause['top_driver_contrib']}% of the gap. "
                f"Action payload drafted for Transport Manager review."
            )
        return (
            f"[{priority_band.upper()} PRIORITY] {label} classified as {insight.get('anomaly_type')} "
            f"with priority score {insight.get('priority_score')}. Action payload prepared."
        )
