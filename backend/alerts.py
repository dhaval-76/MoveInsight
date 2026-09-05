"""Persistence for C5 alert runs and dashboard alert records."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import duckdb


class AlertStore:
    """Write and read the alert records consumed by the dashboard."""

    def __init__(self, db_path: str):
        self.con = duckdb.connect(db_path)
        self._create_tables()

    def _create_tables(self) -> None:
        self.con.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_runs (
                id VARCHAR PRIMARY KEY,
                period VARCHAR NOT NULL,
                grain VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                started_at TIMESTAMP NOT NULL,
                finished_at TIMESTAMP,
                summary_json VARCHAR
            )
            """
        )
        self.con.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_alerts (
                id VARCHAR PRIMARY KEY,
                run_id VARCHAR NOT NULL,
                tenant_id VARCHAR NOT NULL,
                kpi VARCHAR NOT NULL,
                period VARCHAR NOT NULL,
                grain VARCHAR NOT NULL,
                severity VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                priority_score DOUBLE,
                priority_band VARCHAR,
                title VARCHAR NOT NULL,
                summary VARCHAR NOT NULL,
                root_cause_json VARCHAR,
                recommended_actions_json VARCHAR,
                context_json VARCHAR,
                generated_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                c4_confidence VARCHAR,
                group_dimension VARCHAR,
                group_name VARCHAR,
                group_value DOUBLE,
                group_sample_size BIGINT,
                group_sla_gap_pts DOUBLE,
                group_breached BOOLEAN,
                c5_agent VARCHAR,
                c5_model VARCHAR,
                c5_status VARCHAR,
                c5_reasoning_summary VARCHAR,
                c5_narrative VARCHAR,
                c5_recommended_next_step VARCHAR,
                c5_evidence_used_json VARCHAR,
                c5_confidence_note VARCHAR,
                c5_source VARCHAR,
                c5_insight_json VARCHAR
            )
            """
        )

    def start_run(self, period: str, grain: str) -> tuple[str, datetime]:
        started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        run_id = f"run_{started_at.strftime('%Y%m%d%H%M%S%f')}"
        self.con.execute(
            "INSERT INTO alert_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            [run_id, period, grain, "running", started_at, None, None],
        )
        return run_id, started_at

    def finish_run(self, run_id: str, status: str, summary: dict) -> None:
        finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.con.execute(
            """
            UPDATE alert_runs
            SET status = ?, finished_at = ?, summary_json = ?
            WHERE id = ?
            """,
            [status, finished_at, json.dumps(summary), run_id],
        )

    def save_alert(self, run_id: str, anomaly: dict, agent_result: dict) -> dict:
        context = anomaly.get("context") or {}
        filters = context.get("filters") or {}
        tenant_id = filters.get("tenant_id")
        if not tenant_id:
            raise ValueError("Only tenant-scoped anomalies can be saved as dashboard alerts")

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        alert_id = anomaly.get("insight_id")
        if not alert_id:
            raise ValueError("Anomaly is missing insight_id")

        kpi = anomaly.get("kpi", context.get("kpi", "unknown"))
        period = context.get("period") or context.get("month") or "unknown"
        grain = context.get("grain", "month")
        priority_band = anomaly.get("priority_band", "low")
        group = anomaly.get("group") or {}
        group_label = group.get("name")
        title = f"{context.get('label', kpi)} alert for {group_label or tenant_id}"
        if group_label and group.get("dimension"):
            title = f"{context.get('label', kpi)} alert for {group.get('dimension')} {group_label}"
        summary = agent_result.get("executive_summary") or anomaly.get("summary", "")
        action_draft = agent_result.get("action_draft") or {}
        actions = action_draft.get("actions") or action_draft.get("recommended_actions") or []
        ota_reasoning = agent_result.get("ota_reasoning") or {}
        c5_insight = ota_reasoning.get("c4_insight") or anomaly

        self.con.execute(
            """
            INSERT INTO dashboard_alerts (
                id, run_id, tenant_id, kpi, period, grain, severity, status,
                priority_score, priority_band, title, summary, root_cause_json,
                recommended_actions_json, context_json, generated_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                run_id = excluded.run_id,
                priority_score = excluded.priority_score,
                priority_band = excluded.priority_band,
                title = excluded.title,
                summary = excluded.summary,
                root_cause_json = excluded.root_cause_json,
                recommended_actions_json = excluded.recommended_actions_json,
                context_json = excluded.context_json,
                updated_at = excluded.updated_at
            """,
            [
                alert_id,
                run_id,
                tenant_id,
                kpi,
                period,
                grain,
                priority_band,
                "new",
                anomaly.get("priority_score", 0.0),
                priority_band,
                title,
                summary,
                json.dumps(agent_result.get("root_cause", {})),
                json.dumps(actions),
                json.dumps({"anomaly": anomaly, "agent": agent_result}),
                now,
                now,
            ],
        )
        self.con.execute(
            """
            UPDATE dashboard_alerts
            SET c4_confidence = ?, group_dimension = ?, group_name = ?,
                group_value = ?, group_sample_size = ?, group_sla_gap_pts = ?,
                group_breached = ?, c5_agent = ?, c5_model = ?, c5_status = ?,
                c5_reasoning_summary = ?, c5_narrative = ?,
                c5_recommended_next_step = ?, c5_evidence_used_json = ?,
                c5_confidence_note = ?, c5_source = ?, c5_insight_json = ?
            WHERE id = ?
            """,
            [
                anomaly.get("confidence"),
                group.get("dimension"),
                group.get("name"),
                group.get("value"),
                group.get("n"),
                group.get("sla_gap_pts"),
                group.get("breached"),
                ota_reasoning.get("agent"),
                ota_reasoning.get("model"),
                ota_reasoning.get("status") or agent_result.get("status"),
                ota_reasoning.get("reasoning_summary"),
                ota_reasoning.get("narrative"),
                ota_reasoning.get("recommended_next_step"),
                json.dumps(ota_reasoning.get("evidence_used", [])),
                ota_reasoning.get("confidence_note"),
                ota_reasoning.get("source"),
                json.dumps(c5_insight),
                alert_id,
            ],
        )
        return self.get_alert(alert_id)

    def list_alerts(
        self,
        tenant_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict]:
        clauses = []
        params = []
        if tenant_id:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.con.execute(
            f"""
            SELECT id, run_id, tenant_id, kpi, period, grain, severity, status,
                   priority_score, priority_band, title, summary,
                   root_cause_json, recommended_actions_json, context_json,
                   generated_at, updated_at, c4_confidence, group_dimension,
                   group_name, group_value, group_sample_size, group_sla_gap_pts,
                   group_breached, c5_agent, c5_model, c5_status,
                   c5_reasoning_summary, c5_narrative, c5_recommended_next_step,
                   c5_evidence_used_json, c5_confidence_note, c5_source,
                   c5_insight_json
            FROM dashboard_alerts {where}
            ORDER BY priority_score DESC NULLS LAST, generated_at DESC
            """,
            params,
        ).fetchall()
        columns = [item[0] for item in self.con.description]
        return [self._decode_alert(dict(zip(columns, row))) for row in rows]

    def get_alert(self, alert_id: str) -> dict:
        alerts = self.list_alerts()
        for alert in alerts:
            if alert["id"] == alert_id:
                return alert
        raise ValueError(f"Alert not found: {alert_id}")

    def update_status(self, alert_id: str, status: str) -> dict:
        updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        result = self.con.execute(
            """
            UPDATE dashboard_alerts
            SET status = ?, updated_at = ?
            WHERE id = ?
            RETURNING id
            """,
            [status, updated_at, alert_id],
        ).fetchone()
        if not result:
            raise ValueError(f"Alert not found: {alert_id}")
        return self.get_alert(alert_id)

    @staticmethod
    def _decode_alert(alert: dict) -> dict:
        for key in (
            "root_cause_json",
            "recommended_actions_json",
            "context_json",
            "c5_evidence_used_json",
            "c5_insight_json",
        ):
            value = alert.pop(key)
            alert[key.removesuffix("_json")] = json.loads(value) if value else {}
        return alert

    def close(self) -> None:
        self.con.close()