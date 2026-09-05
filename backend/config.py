"""Config: SLA targets, thresholds, emission factors.

These are NOT in the dataset — they are supplied here with stated assumptions,
per HLD §2.4. Editable per tenant/vendor.
"""

import os

try:
    from dotenv import load_dotenv
    _base_dir = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(_base_dir, ".env"))
    load_dotenv(os.path.join(os.path.dirname(_base_dir), ".env"))
except ImportError:
    pass

# On-time-arrival threshold: a trip is "on time" if delay <= this many minutes.
OTA_THRESHOLD_MIN = 10

# Default SLA target for OTA (fraction). Overridable per tenant/vendor below.
DEFAULT_OTA_SLA = 0.90

# Per-tenant SLA overrides (business_unit -> ota target). Assumption: same 90%
# unless a specific enterprise contract says otherwise.
TENANT_OTA_SLA = {
    # "pinnacle-Slc": 0.92,
}

# Optional per-vendor OTA overrides. Keyed by (tenant_id, vendor) when tenant is
# known, or (None, vendor) for a global vendor contract assumption.
VENDOR_OTA_SLA = {
    # ("pinnacle-Slc", "Pooja Mikhailov Travel"): 0.92,
}

# CO2 emission factors (kg CO2 per km) by fuel type. Stated assumptions —
# rough public averages for a shared 4-seater; adjust with real factors.
EMISSION_KG_PER_KM = {
    "PETROL": 0.171,
    "DIESEL": 0.171,
    "CNG": 0.120,
    "HYD": 0.120,      # hybrid
    "HYBRID": 0.120,
    "EV": 0.050,       # grid-charged EV, upstream emissions
    "ELECTRIC": 0.050,
}
DEFAULT_EMISSION_KG_PER_KM = 0.150

# Cost sanity bounds (INR per trip). Rows outside this are quarantined
# (dataset has negatives to -2.2M and highs to 104k => credits/adjustments).
COST_MIN_INR = 0.0
COST_MAX_INR = 20000.0

# Valid alert severities. Anything else (NA, False, ...) => severity_unknown.
VALID_SEVERITIES = {"Sev-1", "Sev-2", "Sev-3"}

# Minimum sample size for a vendor/office to appear in peer comparisons
# (so "worst vendor" is not just the smallest-sample one).
PEER_MIN_TRIPS = 2000

# --- C3 benchmarking / context config -----------------------------------------
#
# Registry of every KPI the context engine understands. For each:
#   method            -> the Metrics method name (C2) that computes it
#   good              -> "up" or "down": which direction is a GOOD movement
#                        (OTA up = good; cost / no-show / CO2 / safety down = good)
#   sla               -> optional target the metric is judged against (None if n/a)
#   industry_norm     -> configurable market benchmark (stated assumption; None if n/a)
#   attribution_dim   -> the dimension to decompose "drivers of change" by
#   label / unit      -> display metadata
#
# This is the single place polarity/targets/norms live, so every downstream
# consumer (badges, briefing, escalation) renders consistently.
METRIC_REGISTRY = {
    "ota": {
        "method": "ota", "good": "up", "sla": DEFAULT_OTA_SLA * 100,
        "industry_norm": 94.0, "attribution_dim": "vendor",
        "label": "On-time arrival", "unit": "%",
    },
    "noshow_rate": {
        "method": "noshow_rate", "good": "down", "sla": None,
        "industry_norm": 6.0, "attribution_dim": "vendor",
        "label": "No-show rate", "unit": "%",
    },
    "cost_per_trip": {
        "method": "cost_per_trip", "good": "down", "sla": None,
        "industry_norm": 1300.0, "attribution_dim": "vendor",
        "label": "Cost per trip", "unit": "INR",
    },
    "occupancy": {
        "method": "occupancy", "good": "up", "sla": None,
        "industry_norm": 65.0, "attribution_dim": "vendor",
        "label": "Occupancy", "unit": "%",
    },
    "co2_per_trip": {
        "method": "co2_per_trip", "good": "down", "sla": None,
        "industry_norm": 2.5, "attribution_dim": "vendor",
        "label": "CO2 per trip", "unit": "kg",
    },
    "safety_score": {
        "method": "safety_score", "good": "down", "sla": None,
        "industry_norm": 40.0, "attribution_dim": "office",
        "label": "Safety alerts", "unit": "per 1k",
    },
    "escort_compliance": {
        "method": "escort_compliance", "good": "up", "sla": 95.0,
        "industry_norm": 98.0, "attribution_dim": "vendor",
        "label": "Night-escort compliance", "unit": "%",
    },
    "feedback_score": {
        "method": "feedback_score", "good": "up", "sla": None,
        "industry_norm": 4.5, "attribution_dim": "vendor",
        "label": "Experience rating", "unit": "1-5",
    },
}

# Months present in the dataset, oldest -> newest (for trend defaults).
DATA_MONTHS = ["2026-05", "2026-06", "2026-07"]

# Time-bucketing grains. strftime formats are chosen to sort lexicographically.
GRAIN_FMT = {"month": "%Y-%m", "week": "%Y-W%V", "day": "%Y-%m-%d"}
DEFAULT_GRAIN = "month"

# --- C4 insight / anomaly detection config -----------------------------------
#
# C4 consumes C3 context objects. It does not reason like an LLM; it evaluates
# configured thresholds and produces repeatable signals + priority scores.
C4_MIN_SAMPLE_SIZE = 200
C4_OTA_MIN_SAMPLE_BY_GRAIN = {
    "day": 20,
    "week": 100,
    "month": 200,
}
C4_ANOMALY_SCORE_THRESHOLD = 50
C4_PRIORITY_BANDS = {
    "critical": 85,
    "high": 70,
    "medium": 50,
}

C4_RULE_DEFAULTS = {
    "sla_gap_pts": 3.0,
    "trend_delta": 5.0,
    "peer_percentile": 25,
    "industry_delta": 5.0,
    "driver_contribution_pct": 35.0,
    "large_sample_size": 1000,
}

C4_RULES = {
    "ota": {
        "sla_gap_pts": 3.0,
        "trend_delta": 2.0,
        "industry_delta": 1.0,
    },
    "noshow_rate": {
        "trend_delta": 1.5,
        "industry_delta": 1.0,
    },
    "cost_per_trip": {
        "trend_delta": 100.0,
        "industry_delta": 100.0,
    },
    "occupancy": {
        "trend_delta": 5.0,
        "industry_delta": 5.0,
    },
    "co2_per_trip": {
        "trend_delta": 0.5,
        "industry_delta": 0.5,
    },
    "safety_score": {
        "trend_delta": 3.0,
        "industry_delta": 5.0,
        "peer_percentile": 25,
    },
    "escort_compliance": {
        "sla_gap_pts": 2.0,
        "trend_delta": 3.0,
        "industry_delta": 2.0,
    },
    "feedback_score": {
        "trend_delta": 0.1,
        "industry_delta": 0.1,
    },
}

# --- C5 agent orchestration config --------------------------------------------
#
# Configurable reasoning mode:
# If ENABLE_REASONING is True: Sense + Reason + Act (root cause investigation, driver breakdown, reasoning trace)
# If ENABLE_REASONING is False: Sense + Act (direct anomaly-to-action payload without reasoning trace)
ENABLE_REASONING = os.environ.get("ENABLE_REASONING", "true").strip().lower() in {
    "1", "true", "yes", "on",
}

GROQ_API_KEY = os.environ.get("GROQ_API_KEY") or os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY", "")
GROQ_BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "meta-llama/llama-prompt-guard-2-86m")

# Backward compatibility aliases
GROK_API_KEY = GROQ_API_KEY
GROK_BASE_URL = GROQ_BASE_URL
GROK_MODEL = GROQ_MODEL

# Persona routing rules based on priority bands and KPI domain
PERSONA_ROUTING = {
    "critical": ["transport_manager", "facilities_head"],
    "high": ["transport_manager", "facilities_head"],
    "medium": ["transport_manager"],
    "low": ["line_manager"],
}

# Path to the persistent DuckDB file.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mobility.duckdb")

import os
_default_input_data = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "input_data"))
_fallback_data = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_data_dir_default = _default_input_data if os.path.exists(_default_input_data) else _fallback_data

DATA_DIR = os.environ.get("MOVEINSYNC_DATA_DIR", _data_dir_default)

# --- C5 lightweight reasoning config -----------------------------------------
#
# Groq exposes an OpenAI-compatible API. Prefer the environment for secrets.
# For demo-only local runs, set GROQ_API_KEY in .env; do not commit a real key.
GROQ_API_KEY_ENV = "GROQ_API_KEY"
GROQ_API_BASE_URL = os.environ.get(
    "GROQ_API_BASE_URL", GROQ_BASE_URL
)
GROQ_REASONING_MODEL = os.environ.get(
    "GROQ_REASONING_MODEL", GROQ_MODEL
)
GROQ_TIMEOUT_SECONDS = float(os.environ.get("GROQ_TIMEOUT_SECONDS", "20"))
GROQ_MAX_TOKENS = int(os.environ.get("GROQ_MAX_TOKENS", "700"))
GROQ_TEMPERATURE = float(os.environ.get("GROQ_TEMPERATURE", "0.2"))

# Raw file names (the thin adapter: change these + the column map in ingest.py
# if the real schema shifts).
TRIP_FILES = [
    "Ride_data _trip-may_2026.csv",
    "Ride_data _trip-June_2026.csv",
    "Ride_data _trip-July_2026.csv",
]
EMP_FILE = "emp_Data.csv"
BILL_FILE = "bill_data.csv"
FEEDBACK_FILE = "trip_feedback.csv"
ALERTS_FILE = "alerts_data.csv"


