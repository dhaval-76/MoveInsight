import {useEffect, useMemo, useState} from "react";
import "./App.css";

const severityColors = {
  critical: "#dc2626",
  high: "#f97316",
  medium: "#fbbf24",
  low: "#22c55e",
};

async function fetchAlerts(apiUrl) {
  const response = await fetch(`${apiUrl}/alerts`);
  if (!response.ok) {
    throw new Error(`Alert API returned ${response.status}`);
  }
  return response.json();
}

async function fetchLatestPeriod(apiUrl) {
  const response = await fetch(`${apiUrl}/alerts/periods`);
  if (!response.ok) {
    throw new Error(`Period API returned ${response.status}`);
  }
  const data = await response.json();
  return data.latest_period;
}

function App() {
  const apiUrl = import.meta.env.VITE_API_URL || "http://localhost:8000";
  const [alerts, setAlerts] = useState([]);
  const [statusFilter, setStatusFilter] = useState("all");
  const [tenantFilter, setTenantFilter] = useState("all");
  const [selectedAlertId, setSelectedAlertId] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [apiError, setApiError] = useState("");
  const [latestPeriod, setLatestPeriod] = useState("");

  useEffect(() => {
    let cancelled = false;
    fetchAlerts(apiUrl)
      .then(data => {
        if (!cancelled) {
          setAlerts(data);
          setApiError("");
        }
      })
      .catch(error => {
        if (!cancelled) {
          setApiError(error.message || "Unable to load alerts");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoading(false);
        }
      });

    fetchLatestPeriod(apiUrl)
      .then(period => {
        if (!cancelled) setLatestPeriod(period || "");
      })
      .catch(error => {
        if (!cancelled)
          setApiError(error.message || "Unable to load data period");
      });

    return () => {
      cancelled = true;
    };
  }, [apiUrl]);

  const loadAlerts = async () => {
    setIsLoading(true);
    try {
      setAlerts(await fetchAlerts(apiUrl));
      setApiError("");
    } catch (error) {
      setApiError(error.message || "Unable to load alerts");
    } finally {
      setIsLoading(false);
    }
  };

  const updateAlertStatus = async status => {
    if (!selectedAlert) return;
    try {
      const response = await fetch(
        `${apiUrl}/alerts/${encodeURIComponent(selectedAlert.id)}/status`,
        {
          method: "PATCH",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({status}),
        },
      );
      if (!response.ok) {
        throw new Error(`Status update returned ${response.status}`);
      }
      const updatedAlert = await response.json();
      setAlerts(currentAlerts =>
        currentAlerts.map(alert =>
          alert.id === updatedAlert.id ? updatedAlert : alert,
        ),
      );
      setApiError("");
    } catch (error) {
      setApiError(error.message || "Unable to update alert status");
    }
  };

  const tenants = useMemo(
    () => [...new Set(alerts.map(alert => alert.tenant_id))],
    [alerts],
  );

  const filteredAlerts = useMemo(() => {
    return alerts.filter(alert => {
      const statusMatch =
        statusFilter === "all" || alert.status === statusFilter;
      const tenantMatch =
        tenantFilter === "all" || alert.tenant_id === tenantFilter;
      return statusMatch && tenantMatch;
    });
  }, [alerts, statusFilter, tenantFilter]);

  const selectedAlert =
    filteredAlerts.find(alert => alert.id === selectedAlertId) ??
    filteredAlerts[0] ??
    null;
  const rootCauseText =
    selectedAlert ?
      typeof selectedAlert.root_cause === "string" ? selectedAlert.root_cause
      : selectedAlert.root_cause?.top_driver_label ?
        `${selectedAlert.root_cause.top_driver_label} is the leading contributor.`
      : "No additional root-cause detail was recorded."
    : "";
  const actionDraft = selectedAlert?.context?.agent?.action_draft;
  const recommendedActions =
    selectedAlert?.recommended_actions?.length ?
      selectedAlert.recommended_actions
    : actionDraft ?
      [
        `Route ${actionDraft.type.replaceAll("_", " ")} to ${actionDraft.recipient}.`,
        actionDraft.evidence_attached?.root_cause_summary ||
          "Review the attached operational evidence.",
        "Submit a corrective action plan within 24 hours.",
      ]
    : ["No recommended actions were recorded."];

  const summary = {
    total: filteredAlerts.length,
    critical: filteredAlerts.filter(a => a.severity === "critical").length,
    high: filteredAlerts.filter(a => a.severity === "high").length,
    new: filteredAlerts.filter(a => a.status === "new").length,
  };

  return (
    <div className='dashboard-shell'>
      <header className='topbar'>
        <div>
          <p className='eyebrow'>MoveInsight</p>
          <h1>Tenant Alert Overview</h1>
          <p className='muted'>
            Latest completed period: {latestPeriod || "loading..."}
          </p>
        </div>
        <button
          className='primary-btn'
          onClick={loadAlerts}
          disabled={isLoading}>
          {isLoading ? "Refreshing alerts..." : "Refresh alerts"}
        </button>
      </header>

      {apiError && <p className='api-error'>{apiError}</p>}

      <section className='summary-grid'>
        <div className='summary-card'>
          <span>Total alerts</span>
          <strong>{summary.total}</strong>
        </div>
        <div className='summary-card warning'>
          <span>Critical</span>
          <strong>{summary.critical}</strong>
        </div>
        <div className='summary-card warning'>
          <span>High</span>
          <strong>{summary.high}</strong>
        </div>
        <div className='summary-card'>
          <span>New</span>
          <strong>{summary.new}</strong>
        </div>
      </section>

      <section className='toolbar'>
        <label>
          Status
          <select
            value={statusFilter}
            onChange={e => setStatusFilter(e.target.value)}>
            <option value='all'>All</option>
            <option value='new'>New</option>
            <option value='acknowledged'>Acknowledged</option>
            <option value='resolved'>Resolved</option>
            <option value='dismissed'>Dismissed</option>
          </select>
        </label>

        <label>
          Tenant
          <select
            value={tenantFilter}
            onChange={e => setTenantFilter(e.target.value)}>
            <option value='all'>All tenants</option>
            {tenants.map(tenant => (
              <option key={tenant} value={tenant}>
                {tenant}
              </option>
            ))}
          </select>
        </label>
      </section>

      <section className='content-grid'>
        <div className='list-panel'>
          <div className='list-header'>
            <h2>Alerts</h2>
            <span>{filteredAlerts.length} results</span>
          </div>

          {isLoading ?
            <p className='empty-state'>Loading alerts...</p>
          : filteredAlerts.map(alert => (
              <article
                key={alert.id}
                className={`alert-row ${selectedAlert?.id === alert.id ? "selected" : ""}`}
                onClick={() => setSelectedAlertId(alert.id)}
                role='button'
                tabIndex={0}
                onKeyDown={event => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    setSelectedAlertId(alert.id);
                  }
                }}>
                <div className='alert-meta-row'>
                  <span
                    className='badge'
                    style={{
                      background: `${severityColors[alert.severity]}22`,
                      color: severityColors[alert.severity],
                    }}>
                    {alert.severity}
                  </span>
                  <span className='pill'>{alert.kpi}</span>
                  <span className='muted'>{alert.period}</span>
                </div>

                <div className='alert-title-row'>
                  <h3>{alert.title}</h3>
                  <span className='status-tag'>{alert.status}</span>
                </div>

                <p className='tenant-name'>{alert.tenant_id}</p>
                <p className='summary-text'>{alert.summary}</p>
              </article>
            ))
          }
          {!isLoading && !filteredAlerts.length && !apiError && (
            <p className='empty-state'>No alerts match the current filters.</p>
          )}
        </div>

        <aside className='detail-panel'>
          <h2>Selected alert</h2>
          {selectedAlert ?
            <>
              <div className='detail-header'>
                <span
                  className='badge'
                  style={{
                    background: `${severityColors[selectedAlert.severity]}22`,
                    color: severityColors[selectedAlert.severity],
                  }}>
                  {selectedAlert.severity}
                </span>
                <span className='pill'>{selectedAlert.priority_band}</span>
              </div>

              <h3>{selectedAlert.title}</h3>
              <p className='detail-tenant'>{selectedAlert.tenant_id}</p>
              <p>{selectedAlert.summary}</p>

              <div className='status-actions'>
                {[
                  ["acknowledged", "Acknowledge"],
                  ["resolved", "Resolve"],
                  ["dismissed", "Dismiss"],
                ].map(([status, label]) => (
                  <button
                    key={status}
                    className={
                      selectedAlert.status === status ?
                        "status-action active"
                      : "status-action"
                    }
                    onClick={() => updateAlertStatus(status)}>
                    {label}
                  </button>
                ))}
              </div>

              <div className='detail-section'>
                <h4>Root cause</h4>
                <p>{rootCauseText}</p>
              </div>

              <div className='detail-section'>
                <h4>Recommended actions</h4>
                <ul>
                  {recommendedActions.map(action => (
                    <li key={action}>{action}</li>
                  ))}
                </ul>
              </div>
            </>
          : <p>No alerts match the current filters.</p>}
        </aside>
      </section>
    </div>
  );
}

export default App;
