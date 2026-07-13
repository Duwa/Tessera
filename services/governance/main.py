"""
HACM Governance Service  —  port 8008
======================================
The SDK integration point for Tessara.

Implements Rajendra (2026c, 2026d) governance instruments:
  - Q:F ratio monitoring (quality-to-fitness ratio)
  - IT:HR ratio diagnostic (compute cost / governance cost)
  - Stage-1 alert: Q:F > T1 for N1 consecutive periods
  - Stage-2 precursor: alignment gap rising
  - Stage-2 confirmation: template drift > T3
  - Probe injection scheduling
  - LangChain / LangGraph / TruLens connector endpoints
  - Three-zone IT:HR governance classification

No equivalent in any HCM platform or AI observability tool.
LangSmith provides Q. This service computes Q:F and governs.

Architecture: stores Q:F time series in Redis alongside the
twin service state. Pushes governance alerts back to the
twin service which updates the org's governance mode.
"""

import os
import json
import math
import time
import hmac
import hashlib
import base64
import uuid
import asyncio
from collections import deque
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from enum import Enum

from fastapi import FastAPI, HTTPException, Query, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import httpx

# ── Redis setup ───────────────────────────────────────────────────────
# Falls back to in-memory if Redis is unavailable (demo mode)
try:
    import redis as redis_lib
    _redis = redis_lib.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True
    )
    _redis.ping()
    REDIS_AVAILABLE = True
except Exception:
    REDIS_AVAILABLE = False
    _mem_store: Dict[str, Any] = {}


def _rset(key: str, value: Any):
    if REDIS_AVAILABLE:
        _redis.set(key, json.dumps(value))
    else:
        _mem_store[key] = value


def _rget(key: str) -> Any:
    if REDIS_AVAILABLE:
        v = _redis.get(key)
        return json.loads(v) if v else None
    return _mem_store.get(key)


def _rpush(key: str, value: Any, maxlen: int = 200):
    """Append to a list, capped at maxlen."""
    if REDIS_AVAILABLE:
        _redis.rpush(key, json.dumps(value))
        _redis.ltrim(key, -maxlen, -1)
    else:
        lst = _mem_store.setdefault(key, [])
        lst.append(value)
        if len(lst) > maxlen:
            _mem_store[key] = lst[-maxlen:]


def _rlist(key: str) -> List[Any]:
    if REDIS_AVAILABLE:
        items = _redis.lrange(key, 0, -1)
        return [json.loads(i) for i in items]
    return _mem_store.get(key, [])


# ── Constants (Rajendra 2026c, 2026d) ─────────────────────────────────

QF_STAGE1_THRESHOLD = 1.15       # Q:F above this = Stage-1 alert
QF_STAGE1_PERIODS   = 3          # consecutive periods required
GAP_STAGE2_THRESHOLD = 0.03      # alignment gap above this = Stage-2 precursor
DRIFT_STAGE2_THRESHOLD = 0.10    # template drift above this = Stage-2 confirmation
ITHR_ZONE1_HIGH = 3.0            # IT:HR > 3.0 = IT-dominant (Zone 1)
ITHR_ZONE2_LOW  = 1.5            # IT:HR < 1.5 = HR-dominant (Zone 3)
PROBE_EFFECTIVE_INTERVAL = 10    # tau_probe <= 10 is effective
PROBE_BREAKEVEN_COST = 400.0     # max probe cost for positive expected value


# ── Enums ─────────────────────────────────────────────────────────────

class AlertLevel(str, Enum):
    GREEN   = "green"
    AMBER   = "amber"
    STAGE1  = "stage1"
    STAGE2_PRECURSOR = "stage2_precursor"
    STAGE2  = "stage2"


class ITHRZone(str, Enum):
    ZONE1_IT_DOMINANT  = "IT-dominant (IT:HR > 3.0) — Stage-2 risk elevated"
    ZONE2_EFFECTIVE    = "Effective (1.5 ≤ IT:HR ≤ 3.0) — governance balanced"
    ZONE3_HR_DOMINANT  = "HR-dominant (IT:HR < 1.5) — governance cost unsustainable"


class LangChainPattern(str, Enum):
    AGENT_EXECUTOR = "agent_executor"   # Legacy single-agent RAG
    LANGGRAPH_SEQ  = "langgraph_sequential"
    LANGGRAPH_HIER = "langgraph_hierarchical"
    LANGGRAPH_P2P  = "langgraph_peer_to_peer"
    CREWAI         = "crewai"
    AUTOGEN        = "autogen"
    BEDROCK        = "bedrock_agent"
    VERTEX         = "vertex_ai_agent"


# ── Pydantic models ───────────────────────────────────────────────────

class QFSignal(BaseModel):
    """
    A single Q:F observation from a connected AI system.
    Q (quality) comes from the AI observability layer (LangSmith, TruLens, etc).
    F (fitness) comes from the organisation's external outcome measurement system.
    The independence requirement: F must be computed from data NOT in the AI corpus.
    """
    org_id: str
    quality_score: float = Field(..., ge=0.0, le=2.0,
        description="Q: output quality relative to indexed corpus (0–2). "
                    "From LangSmith retrieval relevance, TruLens groundedness, etc.")
    fitness_score: float = Field(..., ge=0.0, le=2.0,
        description="F: external outcome fitness (0–2). Must be independent of AI corpus. "
                    "E.g. regulatory incident rate, production quality, win rate.")
    alignment_gap: Optional[float] = Field(None, ge=0.0, le=1.0,
        description="Optional: solution-problem alignment gap. "
                    "If provided, used for Stage-2 precursor detection.")
    source: str = Field("manual",
        description="Source system: langsmith, truelens, ragas, agentcore, manual")
    langchain_pattern: Optional[LangChainPattern] = None
    metadata: Optional[Dict[str, Any]] = None


class ITHRSignal(BaseModel):
    """IT:HR ratio observation — compute cost vs governance cost."""
    org_id: str
    compute_cost: float = Field(..., ge=0,
        description="Cumulative AI compute cost (token billing, inference). From AI infra billing.")
    governance_cost: float = Field(..., ge=0,
        description="Cumulative human governance cost (review labour + LLM audit cost). "
                    "From HR time-tracking records.")
    period_label: Optional[str] = None


class ProbeScheduleRequest(BaseModel):
    """Request a probe injection schedule based on current Q:F state."""
    org_id: str
    c_probe: float = Field(100.0, description="Per-cycle probe injection cost (token-equivalents)")
    c_remediate: float = Field(20000.0, description="One-time Stage-2 remediation cost")
    t_horizon: int = Field(16, description="Review horizon in periods")


class TemplateDriftSignal(BaseModel):
    """Template drift observation from the twin service."""
    org_id: str
    drift_value: float = Field(..., ge=0.0, le=1.0,
        description="Cosine distance between current and initial org belief template")
    current_epoch: int


class GovState(BaseModel):
    """Full governance state for an org."""
    org_id: str
    current_qf: float = 1.0
    qf_trend: List[float] = []
    qf_alert_level: AlertLevel = AlertLevel.GREEN
    consecutive_above_threshold: int = 0
    alignment_gap_trend: List[float] = []
    template_drift: float = 0.0
    ithr_ratio: Optional[float] = None
    ithr_zone: Optional[ITHRZone] = None
    probe_interval_recommended: int = 20
    probe_insurance_positive: bool = False
    last_updated: Optional[str] = None
    signal_count: int = 0


# ── App ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="HACM Governance Service",
    description=(
        "Q:F ratio monitoring, IT:HR governance diagnostic, "
        "Stage-1/Stage-2 alert engine, and LangChain SDK connector. "
        "Grounded in Rajendra (2026c, 2026d). "
        "No equivalent in any HCM platform or AI observability tool."
    ),
    version="1.0.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

TWIN_URL = os.getenv("TWIN_URL", "http://twin:8004")

# ── CPN Live Event Bus ────────────────────────────────────────────────────────
# Ring buffer of recent events — WebSocket clients drain from here
_cpn_event_buf: deque = deque(maxlen=500)
_cpn_event_seq: int = 0

_DOC_TYPE_COLOR = {
    "referral":     "#2dd4bf",
    "lab_result":   "#a78bfa",
    "prescription": "#60a5fa",
    "prior_auth":   "#f59e0b",
    "eob":          "#f59e0b",
    "unknown":      "#888888",
}

def _cpn_emit(event_type: str, **kwargs):
    """Append a CPN event to the ring buffer for WebSocket clients to consume."""
    global _cpn_event_seq
    _cpn_event_seq += 1
    _cpn_event_buf.append({
        "seq":  _cpn_event_seq,
        "type": event_type,
        "ts":   datetime.utcnow().isoformat(),
        **kwargs,
    })

@app.websocket("/cpn/stream")
async def cpn_stream(ws: WebSocket):
    """WebSocket endpoint — streams live CPN events to the Digital Twin visualization."""
    await ws.accept()
    # Start from the current tail so the client only sees new events
    cursor = _cpn_event_seq
    try:
        while True:
            events = [e for e in list(_cpn_event_buf) if e["seq"] > cursor]
            if events:
                for evt in events:
                    await ws.send_json(evt)
                cursor = events[-1]["seq"]
            await asyncio.sleep(0.12)
    except (WebSocketDisconnect, Exception):
        pass


# ── Helpers ───────────────────────────────────────────────────────────

def _get_gov_state(org_id: str) -> dict:
    state = _rget(f"gov:state:{org_id}")
    if state is None:
        state = {
            "org_id": org_id,
            "current_qf": 1.0,
            "qf_alert_level": AlertLevel.GREEN,
            "consecutive_above_threshold": 0,
            "template_drift": 0.0,
            "ithr_ratio": None,
            "ithr_zone": None,
            "probe_interval_recommended": 20,
            "probe_insurance_positive": False,
            "last_updated": None,
            "signal_count": 0,
        }
    return state


def _save_gov_state(org_id: str, state: dict):
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    _rset(f"gov:state:{org_id}", state)


def _compute_alert_level(
    qf: float,
    consecutive: int,
    gap: Optional[float],
    drift: float,
) -> AlertLevel:
    """Compute the current governance alert level per Rajendra (2026c, §6)."""
    if drift > DRIFT_STAGE2_THRESHOLD:
        return AlertLevel.STAGE2
    if gap is not None and gap > GAP_STAGE2_THRESHOLD:
        return AlertLevel.STAGE2_PRECURSOR
    if qf > QF_STAGE1_THRESHOLD and consecutive >= QF_STAGE1_PERIODS:
        return AlertLevel.STAGE1
    if qf > QF_STAGE1_THRESHOLD:
        return AlertLevel.AMBER
    return AlertLevel.GREEN


def _classify_ithr(ratio: float) -> ITHRZone:
    if ratio > ITHR_ZONE1_HIGH:
        return ITHRZone.ZONE1_IT_DOMINANT
    if ratio < ITHR_ZONE2_LOW:
        return ITHRZone.ZONE3_HR_DOMINANT
    return ITHRZone.ZONE2_EFFECTIVE


def _probe_insurance(c_probe: float, c_remediate: float,
                     t_horizon: int, tau: int,
                     p_stage2_no_probe: float = 0.85) -> dict:
    """
    Insurance premium calculation (Rajendra 2026c, Claim 10).
    E[savings] = P(Stage2 | no probe) × c_remediate − c_probe × (T / tau)
    """
    probe_cycles = t_horizon / tau
    expected_savings = p_stage2_no_probe * c_remediate - c_probe * probe_cycles
    breakeven_cost = (p_stage2_no_probe * c_remediate) / probe_cycles
    return {
        "expected_savings": round(expected_savings, 2),
        "breakeven_probe_cost": round(breakeven_cost, 2),
        "is_cost_justified": expected_savings > 0,
        "probe_cycles_in_horizon": round(probe_cycles, 1),
        "p_stage2_without_probe": p_stage2_no_probe,
    }


def _governance_recommendation(state: dict, alert: AlertLevel) -> dict:
    """Map alert level + IT:HR zone to governance recommendation (Rajendra 2026d, §6)."""
    zone = state.get("ithr_zone")
    recs = []
    topology = "flat"

    if alert == AlertLevel.STAGE2:
        recs = [
            "Stage-2 confirmed: corpus contamination active.",
            "Immediate action: rebuild orthogonal probe corpus from scratch.",
            "Reset belief template to pre-drift reference point.",
            "Freeze directed mutation until alignment is restored.",
            "Escalate to Board-level governance review.",
        ]
        topology = "sequential_gateway"

    elif alert == AlertLevel.STAGE1:
        recs = [
            "Stage-1 detected: Q:F sustained above 1.15 for 3+ periods.",
            "Activate probe injection at tau_probe ≤ 10 immediately.",
            "Switch AI agents from autonomous to assistant mode.",
            "Deploy two-stage alignment filter with coverage gap declarations.",
        ]
        topology = "sequential_gateway"
        if zone == ITHRZone.ZONE1_IT_DOMINANT:
            recs.append("IT:HR in Zone 1: increase human reviewer capacity urgently.")

    elif alert == AlertLevel.STAGE2_PRECURSOR:
        recs = [
            "Stage-2 precursor: alignment gap rising.",
            "Reduce probe injection interval to tau_probe = 20.",
            "Commission template stability audit against reference baseline.",
        ]
        topology = "sequential_gateway"

    elif alert == AlertLevel.AMBER:
        recs = [
            "Q:F trending above threshold but not yet sustained.",
            "Monitor for 2 more periods before escalating.",
            "Verify external fitness metric source is independent of AI corpus.",
        ]
        topology = "flat"

    else:
        recs = ["Governance healthy. Maintain current architecture with Q:F monitoring."]
        topology = "flat"

    if zone == ITHRZone.ZONE1_IT_DOMINANT and alert == AlertLevel.GREEN:
        recs.append("IT:HR in Zone 1 despite green Q:F: LLM self-defeat risk. "
                    "Deploy boundary-aware LLM audit to validate.")
    if zone == ITHRZone.ZONE3_HR_DOMINANT:
        recs.append("IT:HR in Zone 3: governance cost approaching compute parity. "
                    "Deploy sequential gateway triage to reduce review volume.")

    return {"recommendations": recs, "topology": topology, "alert_level": alert}


# ── Routes ────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "service": "governance",
        "port": 8008,
        "description": "Q:F monitoring, IT:HR diagnostic, Stage-1/2 alert engine",
        "grounded_in": "Rajendra (2026c, 2026d)",
    }


@app.get("/health")
def health():
    return {
        "status": "up",
        "service": "governance",
        "redis": REDIS_AVAILABLE,
        "twin_url": TWIN_URL,
    }


# ── Q:F Signal ingestion ──────────────────────────────────────────────

@app.post("/qf/signal")
def ingest_qf_signal(signal: QFSignal):
    """
    Ingest a Q:F observation from any connected AI system.

    Q = quality score from AI observability layer (LangSmith, TruLens, Ragas).
    F = fitness score from external outcome measurement (MUST be independent of AI corpus).

    The service maintains a rolling time series, computes the Q:F ratio,
    checks alert thresholds, and returns the current governance state.

    This is the primary SDK integration endpoint.
    """
    qf = signal.quality_score / max(signal.fitness_score, 0.001)
    ts = datetime.now(timezone.utc).isoformat()

    # Persist the signal
    record = {
        "ts": ts,
        "quality": signal.quality_score,
        "fitness": signal.fitness_score,
        "qf": round(qf, 4),
        "alignment_gap": signal.alignment_gap,
        "source": signal.source,
        "pattern": signal.langchain_pattern,
        "metadata": signal.metadata or {},
    }
    _rpush(f"gov:qf_series:{signal.org_id}", record)

    # Update governance state
    state = _get_gov_state(signal.org_id)
    state["current_qf"] = round(qf, 4)
    state["signal_count"] = state.get("signal_count", 0) + 1

    # Track consecutive periods above threshold
    if qf > QF_STAGE1_THRESHOLD:
        state["consecutive_above_threshold"] = state.get("consecutive_above_threshold", 0) + 1
    else:
        state["consecutive_above_threshold"] = 0

    # Track alignment gap trend
    if signal.alignment_gap is not None:
        gap_trend = _rlist(f"gov:gap_trend:{signal.org_id}")
        gap_trend.append(signal.alignment_gap)
        _rset(f"gov:gap_trend:{signal.org_id}", gap_trend[-50:])

    alert = _compute_alert_level(
        qf,
        state["consecutive_above_threshold"],
        signal.alignment_gap,
        state.get("template_drift", 0.0),
    )
    state["qf_alert_level"] = alert

    _save_gov_state(signal.org_id, state)

    rec = _governance_recommendation(state, alert)

    return {
        "org_id": signal.org_id,
        "qf_ratio": round(qf, 4),
        "quality": signal.quality_score,
        "fitness": signal.fitness_score,
        "alert_level": alert,
        "consecutive_above_threshold": state["consecutive_above_threshold"],
        "governance": rec,
        "ts": ts,
    }


# ── IT:HR Signal ──────────────────────────────────────────────────────

@app.post("/ithr/signal")
def ingest_ithr_signal(signal: ITHRSignal):
    """
    Ingest an IT:HR ratio observation.

    IT:HR = compute_cost / governance_cost.
    Compute cost from AI infra billing (token spend, inference).
    Governance cost from HR time-tracking records (review labour, audit cost).

    Classifies into Zone 1 (IT-dominant), Zone 2 (effective), Zone 3 (HR-dominant).
    Rajendra (2026d, §6.1).
    """
    if signal.governance_cost == 0:
        ratio = float("inf")
        zone = ITHRZone.ZONE1_IT_DOMINANT
    else:
        ratio = signal.compute_cost / signal.governance_cost
        zone = _classify_ithr(ratio)

    ts = datetime.now(timezone.utc).isoformat()
    record = {
        "ts": ts,
        "compute": signal.compute_cost,
        "governance": signal.governance_cost,
        "ratio": round(ratio, 4) if ratio != float("inf") else 999.0,
        "zone": zone,
        "period": signal.period_label,
    }
    _rpush(f"gov:ithr_series:{signal.org_id}", record)

    state = _get_gov_state(signal.org_id)
    state["ithr_ratio"] = round(ratio, 4) if ratio != float("inf") else None
    state["ithr_zone"] = zone
    _save_gov_state(signal.org_id, state)

    topology_rec = {
        ITHRZone.ZONE1_IT_DOMINANT: "sequential_gateway — deploy two-stage filter, increase human review",
        ITHRZone.ZONE2_EFFECTIVE: "maintain current architecture with Q:F monitoring",
        ITHRZone.ZONE3_HR_DOMINANT: "sequential gateway triage — concentrate review on high-risk decisions",
    }[zone]

    return {
        "org_id": signal.org_id,
        "ithr_ratio": round(ratio, 4) if ratio != float("inf") else "infinite",
        "zone": zone,
        "zone_description": zone.value,
        "topology_recommendation": topology_rec,
        "ts": ts,
    }


# ── Template Drift Signal ─────────────────────────────────────────────

@app.post("/drift/signal")
def ingest_drift_signal(signal: TemplateDriftSignal):
    """
    Ingest a template drift observation from the twin service.
    Drift = cosine distance between current and initial org belief template.
    Stage-2 confirmed when drift > 0.10 (Rajendra 2026c, §3.7).
    """
    state = _get_gov_state(signal.org_id)
    state["template_drift"] = round(signal.drift_value, 4)

    # Recompute alert with updated drift
    alert = _compute_alert_level(
        state.get("current_qf", 1.0),
        state.get("consecutive_above_threshold", 0),
        None,
        signal.drift_value,
    )
    state["qf_alert_level"] = alert
    _save_gov_state(signal.org_id, state)

    _rpush(f"gov:drift_series:{signal.org_id}", {
        "ts": datetime.now(timezone.utc).isoformat(),
        "drift": signal.drift_value,
        "epoch": signal.current_epoch,
        "alert": alert,
    })

    return {
        "org_id": signal.org_id,
        "template_drift": signal.drift_value,
        "stage2_confirmed": signal.drift_value > DRIFT_STAGE2_THRESHOLD,
        "alert_level": alert,
    }


# ── Probe Injection Scheduler ─────────────────────────────────────────

@app.post("/probe/schedule")
def get_probe_schedule(req: ProbeScheduleRequest):
    """
    Compute optimal probe injection schedule using the insurance premium
    calculation (Rajendra 2026c, Claim 10; §6.4).

    E[savings] = P(Stage2|no probe) × c_remediate − c_probe × (T / tau)
    Probe is justified when E[savings] > 0.
    Effective only at tau_probe ≤ 10.
    """
    state = _get_gov_state(req.org_id)
    alert = state.get("qf_alert_level", AlertLevel.GREEN)

    # P(Stage-2 | no probe) rises with alert level
    p_stage2_map = {
        AlertLevel.GREEN: 0.12,
        AlertLevel.AMBER: 0.35,
        AlertLevel.STAGE1: 0.72,
        AlertLevel.STAGE2_PRECURSOR: 0.88,
        AlertLevel.STAGE2: 0.98,
    }
    p = p_stage2_map.get(alert, 0.85)

    schedules = []
    for tau in [5, 10, 15, 20, 40, 80]:
        ins = _probe_insurance(req.c_probe, req.c_remediate, req.t_horizon, tau, p)
        schedules.append({
            "tau_probe": tau,
            "effective": tau <= PROBE_EFFECTIVE_INTERVAL,
            **ins,
        })

    # Find optimal
    optimal = max((s for s in schedules if s["is_cost_justified"]),
                  key=lambda s: s["expected_savings"],
                  default=schedules[0])

    state["probe_interval_recommended"] = optimal["tau_probe"]
    state["probe_insurance_positive"] = optimal["is_cost_justified"]
    _save_gov_state(req.org_id, state)

    return {
        "org_id": req.org_id,
        "current_alert": alert,
        "p_stage2_without_probe": p,
        "optimal_tau_probe": optimal["tau_probe"],
        "optimal_expected_savings": optimal["expected_savings"],
        "breakeven_probe_cost": optimal["breakeven_probe_cost"],
        "note": (
            "Probe injection is effective only at tau_probe ≤ 10. "
            "Standard probing (tau=20) does not significantly outperform no probing. "
            "Rajendra (2026c, §5.3) — honest null result."
        ),
        "all_schedules": schedules,
    }


# ── Dashboard ─────────────────────────────────────────────────────────

@app.get("/dashboard/{org_id}")
def governance_dashboard(org_id: str):
    """
    Full governance dashboard for the Tessara Early Warning page.
    Returns current state, time series, recommendations, and connector status.
    """
    state = _get_gov_state(org_id)
    qf_series = _rlist(f"gov:qf_series:{org_id}")
    ithr_series = _rlist(f"gov:ithr_series:{org_id}")
    drift_series = _rlist(f"gov:drift_series:{org_id}")
    gap_trend = _rlist(f"gov:gap_trend:{org_id}")

    alert = AlertLevel(state.get("qf_alert_level", AlertLevel.GREEN))
    rec = _governance_recommendation(state, alert)

    # Trend analysis
    recent_qf = [r["qf"] for r in qf_series[-20:]]
    qf_trend_dir = "stable"
    if len(recent_qf) >= 3:
        slope = (recent_qf[-1] - recent_qf[0]) / len(recent_qf)
        qf_trend_dir = "rising" if slope > 0.005 else ("falling" if slope < -0.005 else "stable")

    gap_volatility = 0.0
    if len(gap_trend) >= 3:
        mean_g = sum(gap_trend) / len(gap_trend)
        gap_volatility = round(
            math.sqrt(sum((g - mean_g) ** 2 for g in gap_trend) / len(gap_trend)), 4
        )

    return {
        "org_id": org_id,
        "alert_level": alert,
        "current_qf": state.get("current_qf", 1.0),
        "qf_trend_direction": qf_trend_dir,
        "qf_series": qf_series[-50:],
        "consecutive_above_threshold": state.get("consecutive_above_threshold", 0),
        "template_drift": state.get("template_drift", 0.0),
        "drift_series": drift_series[-50:],
        "alignment_gap_volatility": gap_volatility,
        "gap_trend": gap_trend[-50:],
        "ithr_ratio": state.get("ithr_ratio"),
        "ithr_zone": state.get("ithr_zone"),
        "ithr_series": ithr_series[-20:],
        "probe_interval_recommended": state.get("probe_interval_recommended", 20),
        "probe_insurance_positive": state.get("probe_insurance_positive", False),
        "governance": rec,
        "signal_count": state.get("signal_count", 0),
        "last_updated": state.get("last_updated"),
        "thresholds": {
            "qf_stage1": QF_STAGE1_THRESHOLD,
            "qf_stage1_periods": QF_STAGE1_PERIODS,
            "gap_stage2_precursor": GAP_STAGE2_THRESHOLD,
            "drift_stage2": DRIFT_STAGE2_THRESHOLD,
            "ithr_zone1_above": ITHR_ZONE1_HIGH,
            "ithr_zone3_below": ITHR_ZONE2_LOW,
            "probe_effective_at_or_below": PROBE_EFFECTIVE_INTERVAL,
        },
    }


# ── LangChain SDK Connectors ──────────────────────────────────────────

@app.get("/connectors")
def list_connectors():
    """
    Returns the SDK connector specifications for each supported
    LangChain multi-agent architecture.

    Each connector maps a specific LangGraph / LangChain pattern to the
    correct Tessara integration point and Q:F signal structure.
    """
    return {
        "connectors": [
            {
                "pattern": LangChainPattern.AGENT_EXECUTOR,
                "name": "LangChain AgentExecutor (legacy RAG)",
                "integration_point": "After each chain.invoke() — capture retrieval relevance from callbacks",
                "q_source": "LangSmith retrieval_score or TruLens groundedness",
                "f_source": "External: customer operational outcome system",
                "pathology_risk": "high — single belief domain, no architectural safeguard",
                "stage2_onset": "fast — single template consolidation path",
                "sdk_method": "tessera.governance.ingest_agent_executor(chain, outcome_fn)",
            },
            {
                "pattern": LangChainPattern.LANGGRAPH_SEQ,
                "name": "LangGraph Sequential Pipeline",
                "integration_point": "Monitor Q:F at each node handoff. "
                                     "Planner output misalignment is leading indicator.",
                "q_source": "Per-node LangSmith trace quality; aggregate across pipeline",
                "f_source": "External outcome at pipeline terminal node",
                "pathology_risk": "highest — misalignment at Planner corrupts all downstream nodes",
                "stage2_onset": "fastest — template drift propagates through entire pipeline",
                "sdk_method": "tessera.governance.ingest_langgraph(graph, node='planner', outcome_fn)",
            },
            {
                "pattern": LangChainPattern.LANGGRAPH_HIER,
                "name": "LangGraph Hierarchical (Supervisor)",
                "integration_point": "Monitor Q:F at supervisor routing decision. "
                                     "Supervisor belief domain is primary risk.",
                "q_source": "Supervisor routing quality + sub-agent task completion",
                "f_source": "External outcome per routed task",
                "pathology_risk": "medium — supervisor misalignment is invisible to sub-agent metrics",
                "stage2_onset": "medium — supervisor routing pattern is the belief-domain signal",
                "sdk_method": "tessera.governance.ingest_supervisor(supervisor_node, outcome_fn)",
            },
            {
                "pattern": LangChainPattern.LANGGRAPH_P2P,
                "name": "LangGraph Peer-to-Peer",
                "integration_point": "Monitor Q:F at shared state store. "
                                     "Drift manifests in shared context window.",
                "q_source": "Aggregate quality across all peer agents",
                "f_source": "External outcome from shared task completion",
                "pathology_risk": "medium — belief drift distributed and harder to localise",
                "stage2_onset": "slow — lateral propagation through shared state",
                "sdk_method": "tessera.governance.ingest_shared_state(state_store, outcome_fn)",
            },
            {
                "pattern": LangChainPattern.CREWAI,
                "name": "CrewAI Role-Based Crew",
                "integration_point": "Role definitions are the belief domain vector. "
                                     "Monitor Q:F per role AND at crew-level. "
                                     "Role definition revision requires governance action.",
                "q_source": "CrewAI AMP task completion + role acceptance rates",
                "f_source": "External: engagement conversion, client evaluation score",
                "pathology_risk": "high — role definitions embed belief domain at deployment, rarely revised",
                "stage2_onset": "medium — long-term memory accumulation reinforces old-vertical patterns",
                "sdk_method": "tessera.governance.ingest_crewai(crew, role_audit_fn, outcome_fn)",
            },
            {
                "pattern": LangChainPattern.BEDROCK,
                "name": "Amazon Bedrock Agent",
                "integration_point": "AgentCore Observability provides Q (with self-defeat caveat). "
                                     "Tessara adds external F and computes Q:F.",
                "q_source": "AgentCore CloudWatch metrics — NOTE: shared Knowledge Base = evaluator self-defeat",
                "f_source": "External: CloudWatch business metrics independent of Knowledge Base",
                "pathology_risk": "high — AgentCore continuous evaluation has structural self-defeat limitation",
                "stage2_onset": "fast — Knowledge Base contamination fully invisible to AgentCore",
                "sdk_method": "tessera.governance.ingest_bedrock(agent_id, cloudwatch_fn, outcome_fn)",
            },
            {
                "pattern": LangChainPattern.VERTEX,
                "name": "Google Vertex AI Agent",
                "integration_point": "Vertex AI Evaluation provides Q. "
                                     "Tessara adds external F and computes Q:F.",
                "q_source": "Vertex AI Evaluation response quality scores",
                "f_source": "External: BigQuery business outcomes independent of agent corpus",
                "pathology_risk": "medium — Vertex eval framework is more configurable than Bedrock",
                "stage2_onset": "medium",
                "sdk_method": "tessera.governance.ingest_vertex(agent, bq_fn, outcome_fn)",
            },
        ],
        "note": (
            "TruLens, Ragas, and LangSmith are empirical tools — they measure whether "
            "the agent answered the query correctly relative to its indexed corpus. "
            "Tessara is the governance layer above them. They provide Q. "
            "Tessara computes Q:F against external fitness F. "
            "Rajendra (2026c, §2.3) — the evaluator self-defeat problem."
        ),
    }


@app.post("/connectors/ingest/langsmith")
def ingest_from_langsmith(
    org_id: str,
    run_id: str,
    retrieval_score: float = Query(..., ge=0, le=1,
        description="LangSmith retrieval relevance score — this is Q"),
    fitness_score: float = Query(..., ge=0, le=1,
        description="External outcome fitness — this is F. Must be independent of AI corpus."),
    alignment_gap: Optional[float] = Query(None, ge=0, le=1),
    pattern: LangChainPattern = Query(LangChainPattern.AGENT_EXECUTOR),
):
    """
    Direct LangSmith connector.
    Ingests retrieval_score as Q and external fitness_score as F.
    Returns Q:F ratio and governance state.
    """
    return ingest_qf_signal(QFSignal(
        org_id=org_id,
        quality_score=retrieval_score,
        fitness_score=fitness_score,
        alignment_gap=alignment_gap,
        source="langsmith",
        langchain_pattern=pattern,
        metadata={"run_id": run_id},
    ))


@app.post("/connectors/ingest/truelens")
def ingest_from_truelens(
    org_id: str,
    groundedness: float = Query(..., ge=0, le=1,
        description="TruLens groundedness score — Q (empirical, transaction-level)"),
    answer_relevance: float = Query(..., ge=0, le=1,
        description="TruLens answer relevance — additional Q signal"),
    fitness_score: float = Query(..., ge=0, le=1,
        description="External outcome fitness — F. Independent of corpus."),
    pattern: LangChainPattern = Query(LangChainPattern.AGENT_EXECUTOR),
):
    """
    TruLens connector.
    TruLens is an empirical tool measuring transaction-level quality.
    It provides Q. Tessara computes Q:F.
    Note: TruLens cannot detect whether the corpus itself is aligned with
    operational reality — that requires F from an external source.
    """
    q = (groundedness + answer_relevance) / 2
    return ingest_qf_signal(QFSignal(
        org_id=org_id,
        quality_score=q,
        fitness_score=fitness_score,
        source="truelens",
        langchain_pattern=pattern,
        metadata={"groundedness": groundedness, "answer_relevance": answer_relevance},
    ))


# ── Time series retrieval ─────────────────────────────────────────────

@app.get("/qf/series/{org_id}")
def get_qf_series(org_id: str, last_n: int = Query(50, le=200)):
    """Return the Q:F time series for an org."""
    series = _rlist(f"gov:qf_series:{org_id}")
    return {
        "org_id": org_id,
        "count": len(series),
        "series": series[-last_n:],
    }


@app.get("/ithr/series/{org_id}")
def get_ithr_series(org_id: str, last_n: int = Query(20, le=100)):
    """Return the IT:HR time series for an org."""
    series = _rlist(f"gov:ithr_series:{org_id}")
    return {"org_id": org_id, "count": len(series), "series": series[-last_n:]}


@app.get("/state/{org_id}")
def get_state(org_id: str):
    """Return current governance state."""
    return _get_gov_state(org_id)


@app.delete("/state/{org_id}")
def reset_state(org_id: str):
    """Reset governance state for an org (testing / re-baseline)."""
    if REDIS_AVAILABLE:
        for key in [
            f"gov:state:{org_id}",
            f"gov:qf_series:{org_id}",
            f"gov:ithr_series:{org_id}",
            f"gov:drift_series:{org_id}",
            f"gov:gap_trend:{org_id}",
            f"gov:hav_series:{org_id}",
            f"gov:phi_series:{org_id}",
        ]:
            _redis.delete(key)
    else:
        for key in list(_mem_store.keys()):
            if org_id in key:
                del _mem_store[key]
    return {"status": "reset", "org_id": org_id}


# ── Paper 5: HAV Governance ───────────────────────────────────────────────────
#
# Human Alignment Value replaces man-hours as the unit of human contribution
# in hybrid H+AI organisations. (Rajendra 2026e, "Human Alignment Value and
# the Obsolescence of Man-Hours")
#
# HAV(h,T) = 0.50×NPF + 0.30×SRQ + 0.20×OC
# HAV Crossover Theorem (Theorem 4.1):
#   φ*(K=6)=0.25, φ*(K=4)=0.32, φ*(K≤2)=0.44
#   F_HAV(φ) > F_MH(φ) for all φ > φ*
#   Man-hours governance above φ* ACCELERATES Track 2 pathology.


def _phi_star(K: int) -> float:
    if K <= 2:
        return 0.44
    if K == 4:
        return 0.32
    return 0.25


def _alignment_premium_rate(phi: float) -> float:
    if phi < 0.25:
        return 0.05
    if phi > 0.75:
        return 0.25
    return 0.05 + (phi - 0.25) / 0.50 * 0.20


class HAVSignal(BaseModel):
    """HAV observation ingested from the twin service or HR systems."""
    org_id:     str
    phi:        float = Field(..., ge=0.0, le=1.0,
        description="AI autonomy level = n_ai / (n_human + n_ai)")
    K:          int   = Field(4, ge=1,
        description="NK landscape ruggedness — determines φ* crossover threshold")
    mean_hav:   float = Field(..., ge=0.0, le=1.0,
        description="Mean HAV composite across human agents this epoch")
    mean_npf:   float = Field(0.0, ge=0.0, le=1.0)
    mean_srq:   float = Field(0.0, ge=0.0, le=1.0)
    mean_oc:    float = Field(0.0, ge=0.0, le=1.0)
    measurement_regime: str = Field("HAV",
        description="MAN_HOURS or HAV")
    track2_nudge_active: bool = False
    org_type:   str   = Field("profit", description="profit or nonprofit")
    epoch:      Optional[int] = None


class AlignmentPremiumRequest(BaseModel):
    """Compute alignment premium for one or more human agents."""
    org_id:  str
    phi:     float = Field(..., ge=0.0, le=1.0)
    agents: List[Dict[str, Any]] = Field(...,
        description="List of {human_id, hav_composite, salary}")


@app.post("/hav/signal")
def ingest_hav_signal(signal: HAVSignal):
    """
    Ingest a HAV observation from the twin service or HR system.

    Tracks φ series, crossover status, and Track 2 nudge risk.
    Returns crossover alert when φ > φ*(K) with governance recommendation.
    """
    phi_star_val = _phi_star(signal.K)
    # Nonprofits cross earlier
    if signal.org_type == "nonprofit":
        phi_star_val = max(0.10, phi_star_val * 0.70)

    crossover = signal.phi > phi_star_val
    ts = datetime.now(timezone.utc).isoformat()

    record = {
        "ts":                    ts,
        "phi":                   round(signal.phi, 4),
        "phi_star":              round(phi_star_val, 4),
        "K":                     signal.K,
        "mean_hav":              round(signal.mean_hav, 4),
        "mean_npf":              round(signal.mean_npf, 4),
        "mean_srq":              round(signal.mean_srq, 4),
        "mean_oc":               round(signal.mean_oc, 4),
        "measurement_regime":    signal.measurement_regime,
        "track2_nudge_active":   signal.track2_nudge_active,
        "crossover":             crossover,
        "epoch":                 signal.epoch,
        "org_type":              signal.org_type,
    }
    _rpush(f"gov:hav_series:{signal.org_id}", record)
    _rpush(f"gov:phi_series:{signal.org_id}", {"ts": ts, "phi": signal.phi})

    # Update governance state
    state = _get_gov_state(signal.org_id)
    state["phi_current"]     = round(signal.phi, 4)
    state["phi_star"]        = round(phi_star_val, 4)
    state["mean_hav"]        = round(signal.mean_hav, 4)
    state["hav_crossover"]   = crossover
    state["track2_nudge"]    = signal.track2_nudge_active
    _save_gov_state(signal.org_id, state)

    r_ap = _alignment_premium_rate(signal.phi)
    alerts = []
    recommendation = "HAV governance healthy. Monitor φ as AI autonomy grows."

    if crossover and signal.measurement_regime == "MAN_HOURS":
        alerts.append(
            f"CRITICAL: φ={signal.phi:.2f} > φ*={phi_star_val:.2f} under MAN_HOURS regime. "
            "Man-hours governance is ACTIVELY ACCELERATING Track 2 belief convergence "
            f"at rate (1−NPF)×φ per epoch. Estimated Track 2 onset: {int((1-signal.phi)*10)} periods. "
            "Switch to HAV measurement immediately."
        )
        recommendation = "Switch to HAV measurement regime. Deploy Alignment Premium."
    elif crossover:
        alerts.append(
            f"φ={signal.phi:.2f} > φ*={phi_star_val:.2f}. HAV crossover reached. "
            f"HAV regime active — correctly incentivising Mode 3 SLA guardianship. "
            f"Current alignment premium rate: {r_ap:.0%} of salary."
        )
        recommendation = (
            "HAV regime at crossover. Ensure Alignment Premium is in compensation model. "
            "Protect Values Custodian roles from efficiency-driven elimination."
            if signal.org_type == "nonprofit"
            else "HAV regime at crossover. Alignment Premium correctly signals. Continue."
        )
    elif signal.phi > phi_star_val * 0.8:
        alerts.append(
            f"φ={signal.phi:.2f} approaching φ*={phi_star_val:.2f}. "
            "Crossover imminent. Prepare HAV measurement framework now."
        )

    if signal.track2_nudge_active:
        alerts.append(
            "Track 2 nudge ACTIVE: MAN_HOURS regime is shifting human beliefs toward "
            "AI centroid each epoch. Track 2 onset accelerating."
        )

    if signal.mean_hav < 0.20 and crossover:
        alerts.append(
            f"Mean HAV={signal.mean_hav:.3f} critically low at crossover. "
            "Human agents operating in RPA/Mode 2 with near-zero alignment value. "
            "Rebalance roles toward Mode 3 (SLA guardian) and OC (origination)."
        )

    if signal.org_type == "nonprofit" and crossover and signal.mean_oc < 0.10:
        alerts.append(
            "Nonprofit Elimination Paradox risk: low OC (origination capacity) at crossover "
            "suggests Values Custodian roles may have been eliminated for efficiency. "
            "Conduct Values Baseline Audit immediately."
        )

    return {
        "org_id":           signal.org_id,
        "phi":              signal.phi,
        "phi_star":         phi_star_val,
        "crossover":        crossover,
        "K":                signal.K,
        "r_ap":             r_ap,
        "measurement_regime": signal.measurement_regime,
        "alerts":           alerts,
        "recommendation":   recommendation,
        "ts":               ts,
    }


@app.get("/hav/crossover/{org_id}")
def hav_crossover_status(org_id: str):
    """
    HAV crossover status for an org.
    Returns φ series, crossover history, and current regime recommendation.
    """
    state      = _get_gov_state(org_id)
    hav_series = _rlist(f"gov:hav_series:{org_id}")
    phi_series = _rlist(f"gov:phi_series:{org_id}")

    phi_vals = [r["phi"] for r in hav_series if "phi" in r]
    crossover_events = [r for r in hav_series if r.get("crossover")]

    return {
        "org_id":             org_id,
        "phi_current":        state.get("phi_current"),
        "phi_star":           state.get("phi_star"),
        "crossover_active":   state.get("hav_crossover", False),
        "track2_nudge":       state.get("track2_nudge", False),
        "mean_hav_current":   state.get("mean_hav"),
        "phi_series":         phi_series[-50:],
        "crossover_count":    len(crossover_events),
        "first_crossover_ts": crossover_events[0]["ts"] if crossover_events else None,
        "hav_history":        hav_series[-20:],
        "phi_star_thresholds": {
            "K_ge_6": 0.25,
            "K_4":    0.32,
            "K_le_2": 0.44,
        },
    }


@app.post("/compensation/alignment-premium")
def compute_alignment_premium(req: AlignmentPremiumRequest):
    """
    Compute alignment premium for one or more human agents.

    Total Compensation = Salary + Token Budget + Alignment Premium
    AP(h,T) = r_AP × HAV(h,T) × Salary(h)
    r_AP: 5% at φ < 0.25; 25% at φ > 0.75 (interpolated)

    Rajendra (2026e, §8.1).
    """
    r_ap = _alignment_premium_rate(req.phi)
    results = []
    for agent in req.agents:
        hav       = float(agent.get("hav_composite", 0.0))
        salary    = float(agent.get("salary", 1.0))
        ap        = r_ap * hav * salary
        results.append({
            "human_id":          agent.get("human_id"),
            "hav_composite":     round(hav, 4),
            "salary":            salary,
            "alignment_premium_rate": round(r_ap, 4),
            "alignment_premium": round(ap, 4),
            "total_compensation_multiplier": round(1.0 + r_ap * hav, 4),
            "mode_diagnosis": (
                "RPA — zero HAV, full man-hours equivalent. AI can replace."
                if hav < 0.05
                else "Mode 3 SLA guardian — high SRQ, protect from efficiency cuts."
                if hav > 0.7
                else "Mode 2 ASSISTANT — review quality governs HAV. Increase NPF."
            ),
        })
    total_ap = sum(r["alignment_premium"] for r in results)
    return {
        "org_id":                req.org_id,
        "phi":                   req.phi,
        "r_ap":                  r_ap,
        "agents":                results,
        "total_alignment_budget": round(total_ap, 4),
        "note": (
            "Alignment premium rate rises with φ — humans are rewarded MORE "
            "for alignment contribution when AI agents handle more execution. "
            "Man-hours wages send the OPPOSITE signal. (Rajendra 2026e, §8.1)"
        ),
    }


@app.get("/hav/dashboard/{org_id}")
def hav_dashboard(org_id: str):
    """
    Full HAV governance dashboard for an org.
    Combines HAV crossover status, alignment premium budget, and Track 2 risk.
    """
    state      = _get_gov_state(org_id)
    hav_series = _rlist(f"gov:hav_series:{org_id}")
    phi_series = _rlist(f"gov:phi_series:{org_id}")

    phi_current = state.get("phi_current", 0.0)
    phi_star    = state.get("phi_star", 0.32)
    mean_hav    = state.get("mean_hav", 0.0)
    crossover   = state.get("hav_crossover", False)
    r_ap        = _alignment_premium_rate(phi_current or 0.0)

    phi_trend = [r["phi"] for r in phi_series[-20:]] if phi_series else []
    phi_slope = 0.0
    if len(phi_trend) >= 3:
        phi_slope = (phi_trend[-1] - phi_trend[0]) / len(phi_trend)

    hav_trend = [r["mean_hav"] for r in hav_series[-20:] if "mean_hav" in r]

    return {
        "org_id":                org_id,
        "phi_current":           phi_current,
        "phi_star":              phi_star,
        "crossover":             crossover,
        "phi_trend_slope":       round(phi_slope, 4),
        "phi_trajectory":        "rising" if phi_slope > 0.005 else "stable",
        "mean_hav_current":      mean_hav,
        "alignment_premium_rate": r_ap,
        "track2_nudge_active":   state.get("track2_nudge", False),
        "qf_alert_level":        state.get("qf_alert_level", AlertLevel.GREEN),
        "ithr_ratio":            state.get("ithr_ratio"),
        "ithr_zone":             state.get("ithr_zone"),
        "phi_series":            phi_series[-50:],
        "hav_series":            hav_series[-20:],
        "hav_trend":             hav_trend,
        "nonprofit_risk": (
            "Values Custodian Elimination Paradox: nonprofits above φ* that "
            "continue man-hours accounting will systematically eliminate their "
            "highest-HAV humans under the label of operational efficiency. "
            "Conduct Values Baseline Audit and protect Values Custodian roles."
            if state.get("hav_crossover") and state.get("org_type") == "nonprofit"
            else None
        ),
        "governance_actions": [
            "Switch to HAV measurement regime."
            if crossover and state.get("measurement_regime") == "MAN_HOURS"
            else "HAV regime active — maintain.",
            f"Deploy alignment premium at r_AP={r_ap:.0%}." if crossover else
            f"Monitor φ. Prepare HAV framework before φ reaches {phi_star:.2f}.",
            "Protect Mode 3 SLA guardians from headcount reduction."
            if crossover else "N/A at current φ.",
        ],
        "last_updated":          state.get("last_updated"),
        "signal_count":          state.get("signal_count", 0),
    }


# ── ROAI Framework (Paper 6) ──────────────────────────────────────────
# Rajendra (2026f) — "Fitness, Cost, and Return: A Multi-Construct
# Framework for AI Governance"
#
# ROAI(T) = (ΔF(T) × V_d) / (C_T(T) + C_H(T))
#   ΔF(T)  = fitness improvement attributable to AI deployment
#   V_d    = monetary value per unit fitness improvement (org-calibrated)
#   C_T(T) = AI token/compute cost  (IT:HR compute_cost)
#   C_H(T) = human governance cost  (IT:HR governance_cost)

V_D_DEFAULT = 50000.0   # $50k / unit fitness — demo calibration
RHO_STAR    = 0.42      # crossover threshold (Theorem 3.1, K=4)
THETA_KARMA = 1.0       # karma normalisation factor

TASK_CATEGORIES = [
    {"id": "code_review",        "label": "Code Review"},
    {"id": "ticket_triage",      "label": "Ticket Triage"},
    {"id": "doc_drafting",       "label": "Document Drafting"},
    {"id": "data_analysis",      "label": "Data Analysis"},
    {"id": "meeting_summary",    "label": "Meeting Summary"},
    {"id": "policy_compliance",  "label": "Policy Compliance"},
    {"id": "customer_response",  "label": "Customer Response"},
    {"id": "escalation_routing", "label": "Escalation Routing"},
]

DEMO_AGENTS = [
    {"id": "agent-001", "name": "Aria",   "role": "Triage Specialist"},
    {"id": "agent-002", "name": "Casper", "role": "Document Drafter"},
    {"id": "agent-003", "name": "Dex",    "role": "Code Reviewer"},
    {"id": "agent-004", "name": "Echo",   "role": "Data Analyst"},
    {"id": "agent-005", "name": "Felix",  "role": "Compliance Checker"},
    {"id": "agent-006", "name": "Gina",   "role": "Customer Responder"},
    {"id": "agent-007", "name": "Hiro",   "role": "Escalation Router"},
    {"id": "agent-008", "name": "Iris",   "role": "Meeting Summariser"},
]


def _phi_to_waste(phi: float, regime: str) -> float:
    """Interpolate compute waste fraction from Table 1 (Rajendra 2026f)."""
    if regime == "roai":
        pts = [(0.30, 0.01), (0.60, 0.06), (0.85, 0.12)]
    else:
        pts = [(0.30, 0.08), (0.60, 0.34), (0.85, 0.61)]
    if phi <= pts[0][0]:  return pts[0][1]
    if phi >= pts[-1][0]: return pts[-1][1]
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]; x1, y1 = pts[i + 1]
        if x0 <= phi <= x1:
            t = (phi - x0) / (x1 - x0)
            return round(y0 + t * (y1 - y0), 4)
    return 0.10


def _phi_to_fitness(phi: float, regime: str) -> float:
    """Interpolate terminal fitness from Table 1 (Rajendra 2026f)."""
    if regime == "roai":
        pts = [(0.30, 0.620), (0.60, 0.741), (0.85, 0.733)]
    else:
        pts = [(0.30, 0.618), (0.60, 0.612), (0.85, 0.541)]
    if phi <= pts[0][0]:  return pts[0][1]
    if phi >= pts[-1][0]: return pts[-1][1]
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]; x1, y1 = pts[i + 1]
        if x0 <= phi <= x1:
            t = (phi - x0) / (x1 - x0)
            return round(y0 + t * (y1 - y0), 4)
    return 0.60


@app.get("/roai/summary")
def get_roai_summary(org_id: str = "demo-org", v_d: float = V_D_DEFAULT):
    """
    ROAI(T) = (ΔF(T) × V_d) / (C_T(T) + C_H(T))

    Uses real Q:F and IT:HR series when available; falls back to
    synthetic demo data calibrated to Rajendra (2026f) Table 1.
    """
    state       = _get_gov_state(org_id)
    qf_series   = _rlist(f"gov:qf_series:{org_id}")
    ithr_series = _rlist(f"gov:ithr_series:{org_id}")

    phi = state.get("phi_current") or 0.60
    if phi == 0.0:
        phi = 0.60

    if qf_series and ithr_series:
        recent_qf   = qf_series[-20:]
        recent_ithr = ithr_series[-1] if ithr_series else {}
        fitness_vals = [r.get("fitness", 0.60) for r in recent_qf]
        delta_f = max(0.0, (sum(fitness_vals) / len(fitness_vals)) - 0.50)
        c_t = recent_ithr.get("compute", 45000.0)
        c_h = recent_ithr.get("governance", 20000.0)
    else:
        delta_f = round(0.118 * (phi / 0.60), 4)
        c_t     = round(45000 * phi / 0.60, 2)
        c_h     = round(20000 * (1.0 - phi * 0.3), 2)

    denom = c_t + c_h
    roai  = round((delta_f * v_d) / denom, 4) if denom > 0 else 0.0

    roai_fitness = _phi_to_fitness(phi, "roai")
    cpt_fitness  = _phi_to_fitness(phi, "cpt")
    waste        = _phi_to_waste(phi, "roai")
    d3           = round(0.07 + max(0, phi - 0.30) * 0.10, 4)
    crossover    = phi >= RHO_STAR
    fitness_gain = round(roai_fitness - cpt_fitness, 4)

    signals = []
    if roai < 1.0:
        signals.append({"type": "critical", "label": f"ROAI = {roai:.3f} < 1.0",
            "detail": "Deployment cost exceeds fitness return.",
            "action": "Investigate task-level composition of AI deployment."})
    if waste > 0.30:
        signals.append({"type": "warning", "label": f"Compute Waste {waste:.0%}",
            "detail": "High fraction of tokens on Q:F < 1.0 tasks.",
            "action": "Suppress AI on identified fitness-negative task categories."})
    if d3 > 0.15:
        signals.append({"type": "warning", "label": f"D₃ = {d3:.3f}",
            "detail": "Drift coefficient approaching misalignment threshold.",
            "action": "Monitor token growth / Q:F divergence ratio."})
    if crossover:
        signals.append({"type": "info",
            "label": f"φ = {phi:.2f} > ρ* = {RHO_STAR} — ROAI regime active",
            "detail": f"ROAI governance adds +{fitness_gain:.3f} fitness vs CPT.",
            "action": "Maintain ROAI governance regime."})
    else:
        signals.append({"type": "info",
            "label": f"φ = {phi:.2f} < ρ* = {RHO_STAR} — pre-crossover",
            "detail": "Regime choice has limited fitness consequences below ρ*.",
            "action": f"Prepare ROAI framework before φ crosses {RHO_STAR}."})

    return {
        "org_id": org_id, "phi": round(phi, 3), "rho_star": RHO_STAR,
        "crossover_active": crossover,
        "roai": roai, "delta_f": round(delta_f, 4),
        "v_d": v_d, "c_t": c_t, "c_h": c_h, "total_cost": round(denom, 2),
        "compute_waste_fraction": waste,
        "roai_fitness": roai_fitness, "cpt_fitness": cpt_fitness,
        "fitness_gain": fitness_gain, "d3_drift": d3,
        "governance_signals": signals,
    }


@app.get("/roai/tasks")
def get_roai_tasks(org_id: str = "demo-org"):
    """
    Per-task-type ROAI breakdown.
    Tasks where Q:F < 1.0 are fitness-negative — AI deployment on these
    consumes budget without improving organisational fitness.
    Selective suppression is the primary proximal lever (Rajendra 2026f, §8.2).
    """
    import random
    state = _get_gov_state(org_id)
    phi   = state.get("phi_current") or 0.60

    rng = random.Random(42)
    base_qf = {
        "code_review":        (1.12, 1.38),
        "ticket_triage":      (0.70, 0.94),
        "doc_drafting":       (0.82, 1.08),
        "data_analysis":      (1.04, 1.25),
        "meeting_summary":    (0.62, 0.88),
        "policy_compliance":  (1.10, 1.42),
        "customer_response":  (0.76, 0.99),
        "escalation_routing": (0.55, 0.80),
    }

    tasks = []
    for tc in TASK_CATEGORIES:
        lo, hi    = base_qf.get(tc["id"], (0.80, 1.20))
        qf        = round(rng.uniform(lo, hi), 3)
        tokens_k  = rng.randint(12, 130)
        neg       = qf < 1.0
        waste_pct = rng.uniform(0.42, 0.78) if neg else rng.uniform(0.01, 0.07)
        suppressed = _rget(f"gov:task_suppressed:{org_id}:{tc['id']}") or False
        tasks.append({
            "id": tc["id"], "label": tc["label"],
            "qf_ratio": qf, "tokens_k": tokens_k,
            "fitness_negative": neg,
            "waste_pct": round(waste_pct, 3),
            "roai_contribution": round((qf - 1.0) * tokens_k * 50.0, 1),
            "suppressed": suppressed,
        })

    tasks.sort(key=lambda t: t["qf_ratio"])
    total_tok = sum(t["tokens_k"] for t in tasks)
    waste_tok = sum(t["tokens_k"] for t in tasks if t["fitness_negative"])

    return {
        "org_id": org_id, "phi": round(phi, 3), "tasks": tasks,
        "total_tokens_k": total_tok, "waste_tokens_k": waste_tok,
        "measured_waste_fraction": round(waste_tok / max(total_tok, 1), 4),
    }


@app.post("/roai/tasks/{task_id}/suppress")
def suppress_task(task_id: str, org_id: str = "demo-org"):
    """Toggle AI suppression for a task category."""
    key     = f"gov:task_suppressed:{org_id}:{task_id}"
    current = _rget(key) or False
    _rset(key, not current)
    action  = "suppressed" if not current else "restored"
    toast_msg = f"AI deployment {action} for task: {task_id}"
    return {"task_id": task_id, "suppressed": not current, "action": action}


@app.get("/roai/trend")
def get_roai_trend(org_id: str = "demo-org", periods: int = 12):
    """ROAI vs CPT fitness trajectory over epochs (synthetic, calibrated to Table 1)."""
    import random
    state = _get_gov_state(org_id)
    phi   = state.get("phi_current") or 0.60
    rng   = random.Random(17)

    base_r = _phi_to_fitness(phi, "roai") * V_D_DEFAULT / 65000
    base_c = _phi_to_fitness(phi, "cpt")  * V_D_DEFAULT / 65000

    trend = []
    for i in range(periods):
        nr = rng.uniform(-0.03, 0.04)
        nc = rng.uniform(-0.03, 0.06)
        decay = (i / periods) * 0.08 * (phi / 0.85)
        trend.append({
            "epoch":      i + 1,
            "roai":       round(max(0.5, base_r + nr + i * 0.002), 3),
            "cpt":        round(max(0.4, base_c + nc - decay), 3),
            "waste_roai": round(max(0.0, _phi_to_waste(phi, "roai") + nr * 0.1), 3),
            "waste_cpt":  round(min(0.9, _phi_to_waste(phi, "cpt")  + nc * 0.3), 3),
        })

    return {"org_id": org_id, "phi": round(phi, 3), "periods": periods, "trend": trend}


class KarmaRecord(BaseModel):
    org_id: str
    agent_id: str
    contribution_type: str              # "SRQ" | "OC"
    hamming_distance: Optional[float] = 0.5   # δ(h,b) for SRQ
    framing_novelty:  Optional[float] = 0.3   # for OC: K_OC = (1−fn) × θ
    task_id:  Optional[str] = None
    note:     Optional[str] = None


@app.get("/karma/summary")
def get_karma_summary(org_id: str = "demo-org"):
    """
    Karma Economy: per-agent K_SRQ and K_OC balances.

    K_SRQ = δ(h,b) × θ_karma   — SRQ recovers AI failures (Hamming distance reward)
    K_OC  = (1−framing_novelty) × θ_karma  — OC expands ROAI-positive task space

    Karma balance determines compute access priority
    (Elokda et al. 2024 applied to intra-org AI resource allocation).
    """
    karma_series = _rlist(f"gov:karma:{org_id}")

    if not karma_series:
        import random
        rng = random.Random(99)
        agents = []
        for a in DEMO_AGENTS:
            k_srq   = round(rng.uniform(0.10, 0.90), 3)
            k_oc    = round(rng.uniform(0.05, 0.70), 3)
            balance = round(k_srq * 0.6 + k_oc * 0.4, 3)
            agents.append({**a, "k_srq": k_srq, "k_oc": k_oc,
                "karma_balance": balance,
                "compute_priority": "high" if balance > 0.5 else ("medium" if balance > 0.25 else "low"),
                "srq_events": rng.randint(2, 18), "oc_events": rng.randint(1, 12),
                "last_contribution": f"{rng.randint(1, 6)}h ago"})
        agents.sort(key=lambda x: x["karma_balance"], reverse=True)
        return {
            "org_id": org_id, "agents": agents,
            "total_karma_pool": round(sum(a["karma_balance"] for a in agents), 3),
            "theta_karma": THETA_KARMA,
            "karma_events_total": sum(a["srq_events"] + a["oc_events"] for a in agents),
            "top_contributor": agents[0]["name"] if agents else None,
        }

    amap: Dict[str, dict] = {}
    for r in karma_series:
        aid = r.get("agent_id", "unknown")
        if aid not in amap:
            amap[aid] = {"id": aid, "name": aid, "role": "—",
                         "k_srq": 0.0, "k_oc": 0.0, "srq_events": 0, "oc_events": 0}
        ct = r.get("contribution_type", "SRQ")
        k  = r.get("karma", 0.0)
        if ct == "SRQ":
            amap[aid]["k_srq"]     = round(amap[aid]["k_srq"] + k, 3)
            amap[aid]["srq_events"] += 1
        else:
            amap[aid]["k_oc"]     = round(amap[aid]["k_oc"] + k, 3)
            amap[aid]["oc_events"] += 1

    agents = []
    for a in amap.values():
        b = round(a["k_srq"] * 0.6 + a["k_oc"] * 0.4, 3)
        agents.append({**a, "karma_balance": b,
            "compute_priority": "high" if b > 0.5 else ("medium" if b > 0.25 else "low"),
            "last_contribution": "recent"})
    agents.sort(key=lambda x: x["karma_balance"], reverse=True)

    return {
        "org_id": org_id, "agents": agents,
        "total_karma_pool": round(sum(a["karma_balance"] for a in agents), 3),
        "theta_karma": THETA_KARMA,
        "karma_events_total": len(karma_series),
        "top_contributor": agents[0]["name"] if agents else None,
    }


@app.post("/karma/record")
def record_karma(rec: KarmaRecord):
    """
    Record a SRQ or OC karma contribution.

    SRQ: K_SRQ = δ(h,b) × θ_karma   (Hamming distance between AI prediction and outcome)
    OC:  K_OC  = (1−framing_novelty) × θ_karma
    """
    if rec.contribution_type == "SRQ":
        karma = round((rec.hamming_distance or 0.5) * THETA_KARMA, 4)
        formula = f"K_SRQ = δ({rec.hamming_distance:.2f}) × θ = {karma:.4f}"
    else:
        karma = round((1.0 - (rec.framing_novelty or 0.3)) * THETA_KARMA, 4)
        formula = f"K_OC = (1−{rec.framing_novelty:.2f}) × θ = {karma:.4f}"

    _rpush(f"gov:karma:{rec.org_id}", {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": rec.agent_id, "contribution_type": rec.contribution_type,
        "karma": karma, "task_id": rec.task_id, "note": rec.note,
    })

    return {"recorded": True, "agent_id": rec.agent_id,
            "contribution_type": rec.contribution_type,
            "karma_earned": karma, "formula": formula}


@app.get("/regime/compare")
def compare_regimes(
    org_id: str = "demo-org",
    phi: float = 0.60,
    k: int = 4,
):
    """
    Compare ROAI vs CPT governance at autonomy level φ.

    Crossover Theorem (Rajendra 2026f, Theorem 3.1):
    At φ = ρ* = 0.42 (K=4), ROAI governance dominates CPT on organisational
    fitness for all φ > ρ*. Below ρ*, no statistically significant difference.
    """
    roai_fitness = _phi_to_fitness(phi, "roai")
    cpt_fitness  = _phi_to_fitness(phi, "cpt")
    roai_waste   = _phi_to_waste(phi, "roai")
    cpt_waste    = _phi_to_waste(phi, "cpt")

    crossover   = phi >= RHO_STAR
    fit_gain    = round(roai_fitness - cpt_fitness, 4)
    waste_delta = round(cpt_waste - roai_waste, 4)

    cba_roai = round(min(0.50, 0.18 + max(0, phi - 0.30) * 0.06), 4)
    cba_cpt  = round(min(0.90, 0.31 + max(0, phi - 0.30) * 0.20), 4)
    d3_roai  = round(0.07 + max(0, phi - 0.30) * 0.04, 4)
    d3_cpt   = round(0.14 + max(0, phi - 0.30) * 0.16, 4)

    if phi < 0.45:
        cohen_d = 0.11; p_value = "0.21 (ns)"; sig = False
    elif phi < 0.72:
        t = (phi - 0.45) / (0.72 - 0.45)
        cohen_d = round(0.11 + t * (1.18 - 0.11), 3); p_value = "< 0.001"; sig = True
    else:
        t = (phi - 0.72) / (0.85 - 0.72)
        cohen_d = round(1.18 + t * (1.61 - 1.18), 3); p_value = "< 0.001"; sig = True

    pe_fitness = round(cpt_fitness - 0.003, 4)

    return {
        "org_id": org_id, "phi": phi, "k": k, "rho_star": RHO_STAR,
        "crossover_active": crossover,
        "roai_governance": {
            "terminal_fitness": roai_fitness, "compute_waste": roai_waste,
            "cba_misalignment_rate": cba_roai, "d3_drift_coefficient": d3_roai,
        },
        "cpt_governance": {
            "terminal_fitness": cpt_fitness, "compute_waste": cpt_waste,
            "cba_misalignment_rate": cba_cpt, "d3_drift_coefficient": d3_cpt,
        },
        "prompt_engineering": {
            "terminal_fitness": pe_fitness, "cost_reduction": 0.40,
            "note": "40% C_T reduction produces statistically indistinguishable fitness from CPT base (p=0.74, §7.3)",
        },
        "comparison": {
            "fitness_gain": fit_gain, "waste_reduction": waste_delta,
            "cohen_d": cohen_d, "p_value": p_value,
            "statistically_significant": sig,
            "crossover_satisfied": crossover,
        },
        "crossover_note": (
            f"φ={phi:.2f} > ρ*={RHO_STAR}: ROAI dominates CPT. "
            f"Fitness gain +{fit_gain:.3f}, waste −{waste_delta:.0%}."
            if crossover else
            f"φ={phi:.2f} < ρ*={RHO_STAR}: No statistically significant fitness difference between regimes."
        ),
    }


# ── LBI Framework (Paper 7) ───────────────────────────────────────────
# Rajendra (2026g) — "The Layer Bioelectric Index"
#
# LBI(L) = w1×(Afferent_seq/Total) + w2×(Leaps_originated/Total_novel) + w3×(1−Suppression_rate)
# Pathology: RELAY (LBI<0.2), SUPPRESSOR (supp>0.5), BALANCED (0.2–0.8), SENSOR (LBI>0.8)
# Safe elimination: LBI<0.2 AND ROAI(replacement)>ROAI(L) AND S_up_replacement≥S_up(L)

LBI_W1 = 0.40   # afferent fraction weight
LBI_W2 = 0.35   # leap origination rate weight
LBI_W3 = 0.25   # suppression complement weight
THETA_LEAP   = 0.70   # novelty threshold for leap channel
THETA_URGENT = 0.80   # urgency threshold for leap channel
ALPHA_FORGET = 0.20   # reset magnitude per epoch (T_forget)
D3_THRESHOLD = 0.15   # Track 3 onset threshold

ORGANISATIONAL_LAYERS = [
    {"id": "macro",  "name": "Executive / Macro",   "level": 3},
    {"id": "meso",   "name": "Middle Mgmt / Meso",  "level": 2},
    {"id": "micro",  "name": "Frontline / Micro",   "level": 1},
]

DEPARTMENTS = [
    {"id": "engineering",  "name": "Engineering",        "layer": "micro"},
    {"id": "product",      "name": "Product",             "layer": "meso"},
    {"id": "people",       "name": "People & Culture",    "layer": "meso"},
    {"id": "data",         "name": "Data",                "layer": "micro"},
    {"id": "design",       "name": "Design",              "layer": "micro"},
    {"id": "operations",   "name": "Operations",          "layer": "meso"},
    {"id": "finance",      "name": "Finance",             "layer": "meso"},
    {"id": "executive",    "name": "Executive",           "layer": "macro"},
]

LBI_SCENARIOS = {
    "A": {"label": "Status Quo",          "track3_pct": 0.22, "fitness": 0.741, "leap_reach": 0.38, "meso_lbi": 0.31, "cost_delta": 0.00},
    "B": {"label": "Eliminate Relay Only","track3_pct": 0.41, "fitness": 0.718, "leap_reach": 0.29, "meso_lbi": 0.24, "cost_delta":-0.18},
    "C": {"label": "Flatten Meso (Bolt/Intuit)", "track3_pct": 0.74, "fitness": 0.612, "leap_reach": 0.11, "meso_lbi": 0.09, "cost_delta":-0.35},
    "D": {"label": "HACM-Optimal",        "track3_pct": 0.19, "fitness": 0.758, "leap_reach": 0.47, "meso_lbi": 0.44, "cost_delta":-0.22},
}


def _lbi_pathology(lbi: float, suppression_rate: float) -> str:
    if suppression_rate > 0.5: return "SUPPRESSOR"
    if lbi < 0.2:  return "RELAY"
    if lbi > 0.8:  return "SENSOR"
    return "BALANCED"


def _lbi_for_phi(layer: str, phi: float) -> float:
    """LBI varies with AI autonomy (Experiment 7.1, Rajendra 2026g)."""
    if layer == "macro":
        return round(0.15 + (1.0 - phi) * 0.10, 3)
    elif layer == "meso":
        pts = [(0.30, 0.41), (0.60, 0.28), (0.85, 0.19)]
        if phi <= 0.30: return 0.41
        if phi >= 0.85: return 0.19
        for i in range(len(pts)-1):
            x0, y0 = pts[i]; x1, y1 = pts[i+1]
            if x0 <= phi <= x1:
                return round(y0 + (y1-y0)*(phi-x0)/(x1-x0), 3)
    else:  # micro
        return round(0.62 - phi * 0.15, 3)
    return 0.30


@app.get("/lbi/summary")
def get_lbi_summary(org_id: str = "demo-org"):
    """
    Per-layer Layer Bioelectric Index with pathology classification.
    LBI measures bidirectional fitness signalling capacity of each organisational layer.
    Rajendra (2026g, §3).
    """
    state = _get_gov_state(org_id)
    phi   = state.get("phi_current") or 0.60

    import random
    rng = random.Random(77)

    layers = []
    for lyr in ORGANISATIONAL_LAYERS:
        base_lbi = _lbi_for_phi(lyr["id"], phi)
        noise    = rng.uniform(-0.03, 0.03)
        lbi      = round(max(0.0, min(1.0, base_lbi + noise)), 3)
        supp     = round(rng.uniform(0.1, 0.4) if lyr["id"] != "macro" else rng.uniform(0.0, 0.2), 3)
        afferent_frac  = round(rng.uniform(0.35, 0.55) * lbi * 2.5, 3)
        leap_rate      = round(rng.uniform(0.10, 0.40) * (1.0 - supp), 3)
        pathology      = _lbi_pathology(lbi, supp)
        safe_eliminate = lbi < 0.2 and pathology != "SUPPRESSOR"

        layers.append({
            "id": lyr["id"], "name": lyr["name"], "level": lyr["level"],
            "lbi": lbi, "suppression_rate": supp,
            "afferent_fraction": afferent_frac, "leap_origination_rate": leap_rate,
            "pathology": pathology,
            "safe_to_eliminate": safe_eliminate,
            "efferent_fraction": round(1.0 - afferent_frac, 3),
        })

    overall_lbi = round(sum(l["lbi"] for l in layers) / len(layers), 3)
    afferent_health = round(sum(l["afferent_fraction"] for l in layers) / len(layers), 3)
    suppressor_count = sum(1 for l in layers if l["pathology"] == "SUPPRESSOR")

    return {
        "org_id": org_id, "phi": round(phi, 3),
        "layers": layers,
        "overall_lbi": overall_lbi,
        "afferent_health": afferent_health,
        "suppressor_layers": suppressor_count,
        "lbi_warning": (
            "Meso LBI approaching RELAY threshold — hierarchy reduction risk elevated."
            if _lbi_for_phi("meso", phi) < 0.22 else None
        ),
    }


@app.get("/lbi/hierarchy")
def get_lbi_hierarchy(org_id: str = "demo-org"):
    """
    Department-level LBI analysis with safe-elimination decision support.
    Applies the three-condition elimination rule (Rajendra 2026g, §3.3).
    """
    state = _get_gov_state(org_id)
    phi   = state.get("phi_current") or 0.60

    import random
    rng = random.Random(55)

    depts = []
    for dept in DEPARTMENTS:
        base = _lbi_for_phi(dept["layer"], phi)
        lbi  = round(max(0.05, min(0.95, base + rng.uniform(-0.08, 0.08))), 3)
        supp = round(rng.uniform(0.05, 0.55), 3)
        path = _lbi_pathology(lbi, supp)
        roai_current     = round(rng.uniform(0.8, 2.2), 3)
        roai_replacement = round(roai_current * rng.uniform(0.7, 1.3), 3)
        s_up_current     = round(rng.uniform(0.1, 0.9), 3)
        s_up_replacement = round(s_up_current * rng.uniform(0.5, 1.1), 3)
        cond1 = lbi < 0.20
        cond2 = roai_replacement > roai_current
        cond3 = s_up_replacement >= s_up_current
        depts.append({
            "id": dept["id"], "name": dept["name"], "layer": dept["layer"],
            "lbi": lbi, "suppression_rate": supp, "pathology": path,
            "roai_current": roai_current, "roai_replacement": roai_replacement,
            "s_up_current": s_up_current, "s_up_replacement": s_up_replacement,
            "condition_lbi":  cond1,
            "condition_roai": cond2,
            "condition_sup":  cond3,
            "safe_to_eliminate": cond1 and cond2 and cond3,
            "risk": "low" if (cond1 and cond2 and cond3) else ("critical" if path == "BALANCED" and not cond3 else "medium"),
        })

    depts.sort(key=lambda d: d["lbi"])
    return {
        "org_id": org_id, "phi": round(phi, 3),
        "departments": depts,
        "safe_count":    sum(1 for d in depts if d["safe_to_eliminate"]),
        "critical_count":sum(1 for d in depts if d["risk"] == "critical"),
    }


@app.get("/lbi/scenarios")
def get_lbi_scenarios(phi: float = 0.70, k: int = 4):
    """
    Hierarchy reduction scenario comparison from Experiment 7.2 (Rajendra 2026g, Table 2).
    φ=0.70, K=4, N=30 seeds. Scenario D (HACM-optimal) dominates all alternatives.
    """
    results = []
    for sid, s in LBI_SCENARIOS.items():
        fit = round(s["fitness"] * (0.9 + phi * 0.15), 4)
        tr3 = round(s["track3_pct"] * (0.7 + phi * 0.5), 4)
        results.append({
            "scenario": sid, "label": s["label"],
            "track3_onset_pct": min(0.99, tr3),
            "terminal_fitness": min(0.90, fit),
            "leap_reach_rate": s["leap_reach"],
            "meso_lbi": s["meso_lbi"],
            "cost_delta": s["cost_delta"],
            "recommended": sid == "D",
        })
    return {
        "phi": phi, "k": k, "scenarios": results,
        "insight": "Scenario D achieves lower Track 3 risk and higher fitness than status quo at lower governance cost.",
    }


class LeapSignal(BaseModel):
    org_id: str
    source_layer: str   # "micro" | "meso" | "macro"
    novelty:  float = Field(..., ge=0.0, le=1.0)
    urgency:  float = Field(..., ge=0.0, le=1.0)
    content:  str
    agent_id: Optional[str] = None


@app.post("/leap/signal")
def submit_leap_signal(signal: LeapSignal):
    """
    Submit a signal for leap channel evaluation.
    Fires if novelty > θ_leap=0.70 OR urgency > θ_urgent=0.80 (Rajendra 2026g, §4.1).
    """
    qualifies = signal.novelty > THETA_LEAP or signal.urgency > THETA_URGENT
    reason = []
    if signal.novelty > THETA_LEAP:   reason.append(f"novelty={signal.novelty:.2f} > θ_leap={THETA_LEAP}")
    if signal.urgency > THETA_URGENT: reason.append(f"urgency={signal.urgency:.2f} > θ_urgent={THETA_URGENT}")

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source_layer": signal.source_layer,
        "novelty": signal.novelty, "urgency": signal.urgency,
        "content": signal.content[:200],
        "qualifies": qualifies,
        "agent_id": signal.agent_id,
    }
    _rpush(f"gov:leap:{signal.org_id}", entry)

    return {
        "qualifies_for_leap": qualifies,
        "reason": reason if qualifies else [f"novelty={signal.novelty:.2f} ≤ θ_leap={THETA_LEAP} AND urgency={signal.urgency:.2f} ≤ θ_urgent={THETA_URGENT}"],
        "routing": "LEAP — direct micro→macro" if qualifies else "SEQUENTIAL — normal afferent propagation",
        "thresholds": {"theta_leap": THETA_LEAP, "theta_urgent": THETA_URGENT},
    }


@app.get("/leap/summary")
def get_leap_summary(org_id: str = "demo-org"):
    """
    Leap channel statistics: suppression rate, recent leaps, novelty/urgency distribution.
    """
    leaps = _rlist(f"gov:leap:{org_id}")
    state = _get_gov_state(org_id)
    phi   = state.get("phi_current") or 0.60

    if not leaps:
        import random
        rng = random.Random(13)
        leaps = []
        demo_content = [
            "Novel AI failure: coding agent bypassed security review in 3 consecutive PRs",
            "Regulatory signal: GDPR audit flagged AI-generated customer correspondence",
            "Founding belief drift: product roadmap no longer references core mission statement",
            "Crisis: data pipeline agent deleted staging environment due to ambiguous prompt",
            "Innovation: frontline engineer identified new Q:F-positive task category not in current framework",
            "Misalignment: SRQ score dropping in customer response agent over last 5 epochs",
        ]
        for i in range(10):
            nov = round(rng.uniform(0.40, 0.95), 3)
            urg = round(rng.uniform(0.30, 0.90), 3)
            leaps.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "source_layer": rng.choice(["micro","micro","meso"]),
                "novelty": nov, "urgency": urg,
                "content": demo_content[i % len(demo_content)],
                "qualifies": nov > THETA_LEAP or urg > THETA_URGENT,
                "agent_id": f"agent-{rng.randint(1,8):03d}",
            })

    qualified   = [l for l in leaps if l.get("qualifies")]
    suppressed  = [l for l in leaps if not l.get("qualifies")]
    supp_rate   = round(len(suppressed) / max(len(leaps), 1), 3)
    leap_rate   = round(len(qualified) / max(len(leaps), 1), 3)
    avg_novelty = round(sum(l["novelty"] for l in leaps) / max(len(leaps), 1), 3)
    avg_urgency = round(sum(l["urgency"] for l in leaps) / max(len(leaps), 1), 3)

    pathology = None
    if supp_rate > 0.5:
        pathology = {"type": "SUPPRESSOR", "detail": "More than half of signals intercepted — innovation and crisis detection both compromised."}
    elif leap_rate < 0.1:
        pathology = {"type": "LEAP_STARVED", "detail": "Very few signals reaching leap threshold — possible novelty suppression in lower layers."}

    return {
        "org_id": org_id, "phi": round(phi, 3),
        "total_signals": len(leaps),
        "leap_qualified": len(qualified),
        "sequential_routed": len(suppressed),
        "suppression_rate": supp_rate,
        "leap_reach_rate": leap_rate,
        "avg_novelty": avg_novelty,
        "avg_urgency": avg_urgency,
        "thresholds": {"theta_leap": THETA_LEAP, "theta_urgent": THETA_URGENT},
        "pathology": pathology,
        "recent": leaps[-8:],
    }


@app.get("/unlearning/status")
def get_unlearning_status(org_id: str = "demo-org"):
    """
    T_forget–T_afferent–T_leap triad status and D₃ recovery tracking.
    Rajendra (2026g, §5, Experiment 7.4).

    Recovery times from Experiment 7.4:
      No governance: 18.3 epochs
      T_forget alone: 9.1 epochs (re-onset: 4.1 epochs)
      T_afferent alone: 12.4 epochs
      Full triad: 3.2 epochs  (5.7x faster than no governance)
      Full triad + karma: no re-onset within window
    """
    state      = _get_gov_state(org_id)
    phi        = state.get("phi_current") or 0.60
    drift      = state.get("template_drift", 0.0)
    qf_alert   = state.get("qf_alert_level", AlertLevel.GREEN)
    karma_data = _rlist(f"gov:karma:{org_id}")

    # Triad component status
    karma_active    = len(karma_data) > 0 or True  # karma always considered active in demo
    d3_above_thresh = drift > D3_THRESHOLD

    # T_forget: fires when Q:F Stage1 or drift > threshold
    t_forget_active = qf_alert in [AlertLevel.STAGE1, AlertLevel.STAGE2, AlertLevel.STAGE2_PRECURSOR]
    t_forget_reset  = round(ALPHA_FORGET * drift, 4) if d3_above_thresh else 0.0

    # T_afferent: depends on meso LBI
    meso_lbi = _lbi_for_phi("meso", phi)
    t_afferent_strength = round(meso_lbi / 0.50, 3)  # normalised to 0.50 baseline

    # T_leap: depends on recent qualified leaps
    leaps = _rlist(f"gov:leap:{org_id}")
    recent_qualified = sum(1 for l in leaps[-20:] if l.get("qualifies", False))
    t_leap_active = recent_qualified > 0

    # D₃ trend synthetic (calibrated to Experiment 7.3)
    import random
    rng = random.Random(42)
    meso_supp = round(rng.uniform(0.2, 0.5), 3)  # demo suppression rate
    d3_current = round(0.07 + meso_supp * 0.28, 4)

    triad_complete = t_forget_active and t_afferent_strength > 0.5 and t_leap_active
    recovery_epochs = 3.2 if triad_complete else (9.1 if t_forget_active else (12.4 if t_afferent_strength > 0.5 else 18.3))
    durability = "no_re_onset" if (triad_complete and karma_active) else ("11.8 epochs" if karma_active else "4.1 epochs")

    # Lambda calculation for durability condition
    lambda_consolidate = round(0.08 + phi * 0.04, 4)
    lambda_karma       = round(0.043 * (1 if karma_active else 0), 4)
    lambda_net         = round(lambda_consolidate - lambda_karma, 4)
    reset_magnitude    = round(ALPHA_FORGET * d3_current, 4)
    durable            = reset_magnitude > lambda_net * 1.0  # per epoch

    return {
        "org_id": org_id, "phi": round(phi, 3),
        "d3_current": d3_current,
        "d3_threshold": D3_THRESHOLD,
        "track3_active": d3_current > D3_THRESHOLD,
        "meso_suppression_rate": meso_supp,
        "triad": {
            "t_forget": {"active": t_forget_active, "alpha": ALPHA_FORGET, "reset_per_epoch": t_forget_reset},
            "t_afferent": {"strength": t_afferent_strength, "meso_lbi": meso_lbi, "active": t_afferent_strength > 0.3},
            "t_leap": {"active": t_leap_active, "recent_qualified": recent_qualified},
            "karma_economy": {"active": karma_active, "lambda_reduction": lambda_karma},
        },
        "triad_complete": triad_complete,
        "recovery_epochs": recovery_epochs,
        "re_onset_durability": durability,
        "durability_condition": {
            "reset_magnitude": reset_magnitude,
            "lambda_net": lambda_net,
            "durable": durable,
            "formula": f"α_forget × d(B,B₀) = {reset_magnitude:.4f} {'>' if durable else '≤'} (λ_c − λ_k) × Δt = {lambda_net:.4f}",
        },
        "experiment_benchmarks": {
            "no_governance": 18.3, "t_forget_alone": 9.1,
            "t_afferent_alone": 12.4, "full_triad": 3.2,
        },
    }


# ── DAILY FEED / NOTIFICATION CENTRE ──────────────────────────────────────
# The daily habit loop — designed to be opened every morning.
# Governance alerts (ROAI, LBI, leaps) appear mixed with routine items
# (tickets, approvals, agent reviews) so they feel operational, not academic.

_FEED_TEMPLATES = [
    {
        "id": "f001", "type": "ticket_assigned",
        "icon": "🎫", "color": "#E5A83A", "urgency": "high",
        "title": "VPN access issue — Engineering",
        "body": "Ticket #1042 assigned to you. Reported by Marcus Chen. Unresolved 2h. Agent attempted resolution — flagged for human review.",
        "action_label": "Review ticket", "action_target": "itsm",
        "source_module": "ITSM", "minutes_ago": 120,
    },
    {
        "id": "f002", "type": "agent_review",
        "icon": "🤖", "color": "#4A8EE5", "urgency": "medium",
        "title": "Marketing copy agent — 3 outputs need review",
        "body": "Completed 47 tasks this cycle. 3 flagged for human review (correction rate 6.4%, above 5% threshold). Q:F ratio: 0.82.",
        "action_label": "Review outputs", "action_target": "agents",
        "source_module": "Agent Factory", "minutes_ago": 45,
    },
    {
        "id": "f003", "type": "approval_needed",
        "icon": "👤", "color": "#E5A83A", "urgency": "high",
        "title": "Leave request — Priya Sharma, 3 days",
        "body": "Dec 15–17. No coverage conflict detected. One agent task series scheduled during this period — reassignment may be needed.",
        "action_label": "Approve / Decline", "action_target": "timeattendance",
        "source_module": "Time & Attendance", "minutes_ago": 30,
    },
    {
        "id": "f004", "type": "leap_signal",
        "icon": "⚡", "color": "#6FCF4A", "urgency": "high",
        "title": "Leap signal — coding agent bypassed security review",
        "body": "Micro-layer signal: coding agent submitted 3 consecutive PRs without triggering security scan. Novelty=0.88. Routed directly to you from the frontline team.",
        "action_label": "View signal", "action_target": "leap-channel",
        "source_module": "Leap Channel", "minutes_ago": 15,
    },
    {
        "id": "f005", "type": "roai_alert",
        "icon": "◈", "color": "#E5504A", "urgency": "medium",
        "title": "ROAI dropped 12% this week — Engineering",
        "body": "ROAI fell from 0.741 → 0.651. Compute waste rose to 28%. Primary cause: 14 low-fitness task assignments to AI agents. Review task category distribution.",
        "action_label": "View ROAI", "action_target": "roai-dashboard",
        "source_module": "ROAI Governance", "minutes_ago": 240,
    },
    {
        "id": "f006", "type": "approval_needed",
        "icon": "👤", "color": "#E5A83A", "urgency": "medium",
        "title": "Headcount request — +2 Data Engineers",
        "body": "Submitted by Data team lead. Justification: agent task volume up 340% QoQ, human review capacity at 94%. Approval needed before Dec 10 hiring freeze.",
        "action_label": "Review request", "action_target": "workforce",
        "source_module": "Workforce Planning", "minutes_ago": 480,
    },
    {
        "id": "f007", "type": "lbi_warning",
        "icon": "◫", "color": "#A07BE5", "urgency": "medium",
        "title": "Meso LBI approaching RELAY threshold — Product",
        "body": "Product layer LBI: 0.21 (threshold: 0.20). Suppression rate rising. If AI autonomy increases further at current φ=0.68, Product leads risk becoming passive ratifiers rather than active interpreters.",
        "action_label": "View LBI", "action_target": "lbi-dashboard",
        "source_module": "LBI Monitor", "minutes_ago": 360,
    },
    {
        "id": "f008", "type": "karma_earned",
        "icon": "◑", "color": "#6FCF4A", "urgency": "low",
        "title": "You earned 2.4 karma — model drift catch",
        "body": "K_SRQ credit for flagging marketing agent output drift before client delivery. Hamming distance δ(h,b)=0.31. Your priority tier: HIGH. Lambda reduction applied.",
        "action_label": "View karma", "action_target": "karma-economy",
        "source_module": "Karma Economy", "minutes_ago": 600,
    },
    {
        "id": "f009", "type": "agent_review",
        "icon": "🤖", "color": "#4A8EE5", "urgency": "low",
        "title": "Analytics agent — weekly performance summary",
        "body": "89% task accuracy this week. 11% required human correction (down from 16% last week). Compute cost: $142. ROAI contribution: +0.034. Trending positive.",
        "action_label": "View details", "action_target": "agents",
        "source_module": "Agent Factory", "minutes_ago": 1440,
    },
    {
        "id": "f010", "type": "digest_ready",
        "icon": "📊", "color": "#4A8EE5", "urgency": "low",
        "title": "Weekly governance digest — ready",
        "body": "ROAI up 8% vs last week. 2 new leap signals fired. LBI stable across macro/micro. Compute waste: 19% (below 25% target). One meso layer needs attention.",
        "action_label": "Read digest", "action_target": "my-feed",
        "source_module": "Tessera", "minutes_ago": 480,
    },
]

_URGENCY_ORDER = {"high": 0, "medium": 1, "low": 2}


@app.get("/notifications/feed")
def get_notifications_feed(org_id: str = "demo-org", user_id: str = "user-001"):
    """
    Unified personal feed: ITSM tickets, agent reviews, governance alerts,
    workforce approvals, karma events — all in one stream.
    Governance items mixed naturally with operational items.
    """
    import random
    rng = random.Random(hash(org_id) % 9999)

    items = []
    for t in _FEED_TEMPLATES:
        read = rng.random() < 0.3  # 70% unread
        items.append({**t, "read": read})

    # Sort: action-required (high/medium, unread) first, then by recency
    items.sort(key=lambda x: (
        1 if x["read"] else 0,
        _URGENCY_ORDER.get(x["urgency"], 2),
        x["minutes_ago"],
    ))

    action_required = [i for i in items if not i["read"] and i["urgency"] in ("high", "medium")]
    team_feed       = [i for i in items if i not in action_required]
    unread_count    = sum(1 for i in items if not i["read"])

    state = _get_gov_state(org_id)
    phi   = state.get("phi_current") or 0.60

    digest = {
        "roai_delta_pct": 8,
        "compute_waste_pct": 19,
        "leap_signals_this_week": 2,
        "karma_earned": 2.4,
        "lbi_status": "stable",
        "meso_lbi": round(_lbi_for_phi("meso", phi), 3),
        "actions_taken": 14,
        "highlight": "Compute waste below 25% target for the first time this quarter.",
    }

    return {
        "org_id": org_id,
        "user_id": user_id,
        "unread_count": unread_count,
        "action_required": action_required,
        "team_feed": team_feed,
        "digest": digest,
        "phi": round(phi, 3),
    }


# ── INTEGRATION CONNECTORS & RUNBOOKS ─────────────────────────────────────

_CONNECTORS = [
    {
        "id": "servicenow", "name": "ServiceNow", "category": "IT",
        "logo": "SN", "color": "#00C06F",
        "description": "Sync incidents, requests and CMDB entries. Tessera ITSM becomes the primary UI; ServiceNow stays as the system of record during transition.",
        "replaces_module": "ITSM",
        "sync_direction": "bidirectional",
        "setup_method": "OAuth 2.0",
        "sync_interval_mins": 5,
        "data_pulled": ["tickets", "categories", "assignees", "SLA status", "CMDB CIs"],
        "available": True,
    },
    {
        "id": "jira_sm", "name": "Jira Service Management", "category": "IT",
        "logo": "JS", "color": "#0052CC",
        "description": "Pull issues and queues from Jira SM. Agents can triage and resolve from Tessera without leaving the platform.",
        "replaces_module": "ITSM",
        "sync_direction": "bidirectional",
        "setup_method": "API Token",
        "sync_interval_mins": 2,
        "data_pulled": ["issues", "queues", "sprints", "SLAs", "assets"],
        "available": True,
    },
    {
        "id": "adp", "name": "ADP Workforce Now", "category": "Labor",
        "logo": "AD", "color": "#D4002A",
        "description": "Import employee records, pay groups, and time data. Tessera handles T&A and leave; ADP remains the payroll source of truth.",
        "replaces_module": "Time & Attendance",
        "sync_direction": "pull + export",
        "setup_method": "SFTP + API",
        "sync_interval_mins": 60,
        "data_pulled": ["employees", "pay groups", "cost centres", "time entries", "leave balances"],
        "available": True,
    },
    {
        "id": "workday", "name": "Workday HCM", "category": "Labor",
        "logo": "WD", "color": "#F5820E",
        "description": "Sync org structure, positions, and headcount plans. Tessera workforce planning replaces Workday planning modules while Workday retains core HCM data.",
        "replaces_module": "Workforce Planning",
        "sync_direction": "pull + write-back",
        "setup_method": "OAuth 2.0 + RAAS",
        "sync_interval_mins": 30,
        "data_pulled": ["org hierarchy", "positions", "headcount", "job profiles", "compensation bands"],
        "available": True,
    },
    {
        "id": "bamboohr", "name": "BambooHR", "category": "Labor",
        "logo": "BH", "color": "#73AA24",
        "description": "Import employee profiles and time-off policies. Light HR teams can replace BambooHR entirely within 60 days.",
        "replaces_module": "Time & Attendance + Onboarding",
        "sync_direction": "bidirectional",
        "setup_method": "API Key",
        "sync_interval_mins": 15,
        "data_pulled": ["employees", "time-off", "onboarding tasks", "documents", "org chart"],
        "available": True,
    },
    {
        "id": "ms_teams", "name": "Microsoft Teams", "category": "Notifications",
        "logo": "MT", "color": "#6264A7",
        "description": "Push My Feed alerts and governance digests to Teams channels. No workflow migration needed — just a notification bridge.",
        "replaces_module": "Notifications",
        "sync_direction": "push",
        "setup_method": "Webhook",
        "sync_interval_mins": 0,
        "data_pulled": [],
        "available": True,
    },
    {
        "id": "slack", "name": "Slack", "category": "Notifications",
        "logo": "SL", "color": "#4A154B",
        "description": "Send My Feed items and leap signals to Slack channels. Approve leave requests and triage tickets directly from Slack.",
        "replaces_module": "Notifications",
        "sync_direction": "push + action",
        "setup_method": "Slack App install",
        "sync_interval_mins": 0,
        "data_pulled": [],
        "available": True,
    },
    {
        "id": "csv_upload", "name": "CSV / Excel Import", "category": "Manual",
        "logo": "CS", "color": "#217346",
        "description": "One-time or scheduled file drop for systems without APIs. Supports employee lists, ticket exports, timesheet templates.",
        "replaces_module": "Any",
        "sync_direction": "pull",
        "setup_method": "File upload",
        "sync_interval_mins": None,
        "data_pulled": ["any tabular data"],
        "available": True,
    },
    {
        "id": "langchain", "name": "LangChain Agent Pipeline", "category": "AI Agents",
        "logo": "LC", "color": "#1C3C3C",
        "description": "Add one TesseraCallback to your LangChain chain. Every agent run, escalation, and correction event flows into Tessera governance exhaust automatically — no other changes to your code.",
        "replaces_module": None,
        "sync_direction": "push (webhook)",
        "setup_method": "Callback handler",
        "sync_interval_mins": 0,
        "data_pulled": ["agent events", "escalations", "correction signals", "ROAI metrics", "token spend"],
        "available": True,
    },
    {
        "id": "langsmith", "name": "LangSmith Observability", "category": "AI Agents",
        "logo": "LS", "color": "#2D5A27",
        "description": "Pull LangSmith traces directly into Tessera Signals. Latency, cost, error rate, and hallucination flags surface in the ROAI dashboard alongside your people data.",
        "replaces_module": None,
        "sync_direction": "pull",
        "setup_method": "API Key",
        "sync_interval_mins": 5,
        "data_pulled": ["traces", "runs", "feedback", "latency", "cost per run", "error rate"],
        "available": True,
    },
    {
        "id": "opentelemetry", "name": "OpenTelemetry / OTEL", "category": "AI Agents",
        "logo": "OT", "color": "#4A5568",
        "description": "Generic agent telemetry via OTEL spans. Works with any LLM framework — CrewAI, AutoGen, custom agents. Tessera reads span attributes to compute ROAI.",
        "replaces_module": None,
        "sync_direction": "push (OTLP)",
        "setup_method": "OTLP endpoint",
        "sync_interval_mins": 0,
        "data_pulled": ["spans", "traces", "agent steps", "tool calls", "latency"],
        "available": True,
    },
    {
        "id": "efax", "name": "eFax / SRFax", "category": "Healthcare",
        "logo": "EF", "color": "#E5504A",
        "description": "Receive inbound faxes as webhook events with PDF/TIFF attachments. Used by the Fax Triage agent — every fax becomes a structured event; PHI fields are logged to the audit trail automatically.",
        "replaces_module": None,
        "sync_direction": "push (webhook)",
        "setup_method": "Webhook URL + API Key",
        "sync_interval_mins": 0,
        "data_pulled": ["fax image", "sender number", "received timestamp", "page count"],
        "available": True,
    },
    {
        "id": "ehr", "name": "EHR (Epic / Cerner FHIR)", "category": "Healthcare",
        "logo": "EH", "color": "#005EB8",
        "description": "Push structured tasks to Epic MyChart or Cerner PowerChart via HL7 FHIR R4. The Fax Triage agent creates a FHIR Task resource for each routed document, updating the provider's work queue without manual entry.",
        "replaces_module": None,
        "sync_direction": "push (FHIR R4)",
        "setup_method": "SMART on FHIR OAuth",
        "sync_interval_mins": 0,
        "data_pulled": ["FHIR Task", "Patient resource", "ServiceRequest", "DiagnosticReport"],
        "available": True,
    },
]

# ─── Automation Engine ────────────────────────────────────────────────────────

_DEFAULT_AUTO_RULES = [
    {
        "id": "auto-roai-digest",
        "name": "ROAI Weekly Digest",
        "blueprint_id": "gov.roai-digest",
        "blueprint_name": "ROAI Weekly Digest",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 8 * * 1", "label": "Every Monday 08:00"},
        "enabled": True,
        "run_count": 12,
        "last_run_at": "2026-06-23T08:00:00Z",
        "last_run_status": "ok",
        "created_at": "2026-06-01T00:00:00Z",
    },
    {
        "id": "auto-lbi-watchdog",
        "name": "LBI Watchdog",
        "blueprint_id": "gov.lbi-watchdog",
        "blueprint_name": "LBI Watchdog",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 */6 * * *", "label": "Every 6 hours"},
        "enabled": True,
        "run_count": 47,
        "last_run_at": "2026-06-29T12:00:00Z",
        "last_run_status": "ok",
        "created_at": "2026-06-01T00:00:00Z",
    },
    {
        "id": "auto-compliance-monthly",
        "name": "Monthly Compliance Report",
        "blueprint_id": "gov.compliance-reporter",
        "blueprint_name": "Compliance Evidence Compiler",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 6 1 * *", "label": "1st of every month, 06:00"},
        "enabled": True,
        "run_count": 1,
        "last_run_at": "2026-06-01T06:00:00Z",
        "last_run_status": "ok",
        "created_at": "2026-06-01T00:00:00Z",
    },
    {
        "id": "auto-ticket-triage",
        "name": "IT Ticket Triage",
        "blueprint_id": "it.ticket-triage",
        "blueprint_name": "IT Ticket Triage",
        "trigger_type": "event",
        "trigger_config": {"event_type": "itsm_ticket_created", "label": "When: new ITSM ticket"},
        "enabled": True,
        "run_count": 142,
        "last_run_at": "2026-06-29T14:22:00Z",
        "last_run_status": "ok",
        "created_at": "2026-06-15T00:00:00Z",
    },
    {
        "id": "auto-onboarding",
        "name": "New Hire Onboarding",
        "blueprint_id": "hr.onboarding-coordinator",
        "blueprint_name": "Onboarding Coordinator",
        "trigger_type": "event",
        "trigger_config": {"event_type": "employee_hired", "label": "When: new hire in HRIS"},
        "enabled": True,
        "run_count": 7,
        "last_run_at": "2026-06-25T09:00:00Z",
        "last_run_status": "ok",
        "created_at": "2026-06-15T00:00:00Z",
    },
    {
        "id": "auto-leave-approval",
        "name": "Leave Request Processing",
        "blueprint_id": "hr.leave-approver",
        "blueprint_name": "Leave Request Approver",
        "trigger_type": "event",
        "trigger_config": {"event_type": "leave_request_submitted", "label": "When: leave request submitted"},
        "enabled": True,
        "run_count": 23,
        "last_run_at": "2026-06-27T16:41:00Z",
        "last_run_status": "ok",
        "created_at": "2026-06-15T00:00:00Z",
    },
    {
        "id": "auto-fax-triage",
        "name": "Fax Triage",
        "blueprint_id": "health.fax-triage",
        "blueprint_name": "Healthcare Fax Triage",
        "trigger_type": "event",
        "trigger_config": {"event_type": "fax_received", "label": "When: fax arrives via eFax"},
        "enabled": False,
        "run_count": 0,
        "last_run_at": None,
        "last_run_status": None,
        "created_at": "2026-06-28T00:00:00Z",
    },
]

_DEFAULT_AUTO_RUNS = [
    {"id": "run-001", "rule_id": "auto-ticket-triage",    "rule_name": "IT Ticket Triage",       "blueprint_id": "it.ticket-triage",         "triggered_by": "event",    "trigger_detail": "event: itsm_ticket_created", "started_at": "2026-06-29T14:22:00Z", "duration_ms": 1840, "status": "ok",    "output_summary": "Ticket TKT-0882 classified → routed to Tier-1 queue"},
    {"id": "run-002", "rule_id": "auto-lbi-watchdog",     "rule_name": "LBI Watchdog",           "blueprint_id": "gov.lbi-watchdog",         "triggered_by": "schedule", "trigger_detail": "cron: 0 */6 * * *",         "started_at": "2026-06-29T12:00:00Z", "duration_ms": 920,  "status": "ok",    "output_summary": "Meso score 0.34 — above 0.20 threshold, no alert"},
    {"id": "run-003", "rule_id": "auto-ticket-triage",    "rule_name": "IT Ticket Triage",       "blueprint_id": "it.ticket-triage",         "triggered_by": "event",    "trigger_detail": "event: itsm_ticket_created", "started_at": "2026-06-29T11:47:00Z", "duration_ms": 2110, "status": "ok",    "output_summary": "Ticket TKT-0881 auto-resolved (password reset)"},
    {"id": "run-004", "rule_id": "auto-leave-approval",   "rule_name": "Leave Request Processing","blueprint_id": "hr.leave-approver",        "triggered_by": "event",    "trigger_detail": "event: leave_request_submitted","started_at": "2026-06-27T16:41:00Z","duration_ms": 1340,"status": "ok",    "output_summary": "Leave approved for EMP-2241 — no conflicts detected"},
    {"id": "run-005", "rule_id": "auto-lbi-watchdog",     "rule_name": "LBI Watchdog",           "blueprint_id": "gov.lbi-watchdog",         "triggered_by": "schedule", "trigger_detail": "cron: 0 */6 * * *",         "started_at": "2026-06-29T06:00:00Z", "duration_ms": 870,  "status": "ok",    "output_summary": "Meso score 0.31 — above threshold"},
    {"id": "run-006", "rule_id": "auto-roai-digest",      "rule_name": "ROAI Weekly Digest",     "blueprint_id": "gov.roai-digest",          "triggered_by": "schedule", "trigger_detail": "cron: 0 8 * * 1",           "started_at": "2026-06-23T08:00:00Z", "duration_ms": 3210, "status": "ok",    "output_summary": "Digest sent — deflection rate 67%, ROAI 4.2x, 3 agents tracked"},
    {"id": "run-007", "rule_id": "auto-onboarding",       "rule_name": "New Hire Onboarding",    "blueprint_id": "hr.onboarding-coordinator","triggered_by": "event",    "trigger_detail": "event: employee_hired",      "started_at": "2026-06-25T09:00:00Z", "duration_ms": 4480, "status": "ok",    "output_summary": "Onboarding checklist started for EMP-2249 — IT provisioning triggered"},
    {"id": "run-008", "rule_id": "auto-compliance-monthly","rule_name": "Monthly Compliance Report","blueprint_id": "gov.compliance-reporter","triggered_by": "schedule", "trigger_detail": "cron: 0 6 1 * *",           "started_at": "2026-06-01T06:00:00Z", "duration_ms": 8920, "status": "ok",    "output_summary": "Evidence package compiled — 14 SOC2 controls, 10 HIPAA controls mapped"},
]


def _get_auto_rules(org_id: str) -> list:
    key = f"automation:rules:{org_id}"
    try:
        rules = _rget(key)
    except Exception:
        # Stale key with wrong Redis type — delete and start fresh
        if REDIS_AVAILABLE:
            try:
                _redis.delete(key)
            except Exception:
                pass
        rules = None
    if not rules:
        rules = [dict(r) for r in _DEFAULT_AUTO_RULES]
        _rset(key, rules)
    return rules


def _fire_auto_rule(rule: dict, org_id: str, triggered_by: str = "schedule",
                    trigger_detail: str = "") -> dict:
    """Record a rule execution and update its stats."""
    import random as _rand
    now = datetime.utcnow().isoformat()
    run_rec = {
        "id": f"run-{uuid.uuid4().hex[:8]}",
        "rule_id": rule["id"],
        "rule_name": rule["name"],
        "blueprint_id": rule["blueprint_id"],
        "triggered_by": triggered_by,
        "trigger_detail": trigger_detail or rule["trigger_config"].get("label", ""),
        "started_at": now,
        "duration_ms": _rand.randint(600, 5000),
        "status": "ok",
        "output_summary": f"{rule['blueprint_name']} completed",
    }
    _rpush(f"automation:runs:{org_id}", run_rec, maxlen=200)

    # Update rule stats
    key = f"automation:rules:{org_id}"
    rules = _get_auto_rules(org_id)
    for r in rules:
        if r["id"] == rule["id"]:
            r["run_count"] = r.get("run_count", 0) + 1
            r["last_run_at"] = now
            r["last_run_status"] = "ok"
    _rset(key, rules)

    # Governance event
    _rpush(f"agent_events:{org_id}", {
        "ts": now, "event_type": "automation_triggered", "source": "automation_engine",
        "rule_id": rule["id"], "blueprint_id": rule["blueprint_id"],
        "triggered_by": triggered_by,
    }, maxlen=500)

    _cpn_emit("auto_routed", color="#f59e0b", doc_type="automation",
               dept=rule.get("blueprint_name", "Automation"), confidence=0.99,
               source=f"rule:{rule['id']}")

    return run_rec


@app.get("/automation/rules")
def list_auto_rules(org_id: str = "demo-org"):
    rules = _get_auto_rules(org_id)
    scheduled    = [r for r in rules if r["trigger_type"] == "schedule"]
    event_driven = [r for r in rules if r["trigger_type"] == "event"]
    return {
        "rules": rules,
        "scheduled": scheduled,
        "event_driven": event_driven,
        "total": len(rules),
        "enabled": sum(1 for r in rules if r["enabled"]),
        "runs_today": sum(r.get("run_count", 0) for r in rules
                          if (r.get("last_run_at") or "")[:10] == datetime.utcnow().date().isoformat()),
    }


class AutoRuleCreate(BaseModel):
    name: str
    blueprint_id: str
    blueprint_name: str
    trigger_type: str               # schedule | event
    trigger_config: dict            # {cron, label} or {event_type, label}


@app.post("/automation/rules", status_code=201)
def create_auto_rule(body: AutoRuleCreate, org_id: str = "demo-org"):
    rule = {
        "id": f"auto-{uuid.uuid4().hex[:8]}",
        "name": body.name,
        "blueprint_id": body.blueprint_id,
        "blueprint_name": body.blueprint_name,
        "trigger_type": body.trigger_type,
        "trigger_config": body.trigger_config,
        "enabled": True,
        "run_count": 0,
        "last_run_at": None,
        "last_run_status": None,
        "created_at": datetime.utcnow().isoformat(),
    }
    rules = _get_auto_rules(org_id)
    rules.append(rule)
    _rset(f"automation:rules:{org_id}", rules)
    return rule


@app.patch("/automation/rules/{rule_id}/toggle")
def toggle_auto_rule(rule_id: str, org_id: str = "demo-org"):
    rules = _get_auto_rules(org_id)
    for r in rules:
        if r["id"] == rule_id:
            r["enabled"] = not r["enabled"]
            _rset(f"automation:rules:{org_id}", rules)
            return {"rule_id": rule_id, "enabled": r["enabled"]}
    raise HTTPException(404, f"Rule {rule_id} not found")


@app.post("/automation/rules/{rule_id}/run")
def run_auto_rule_now(rule_id: str, org_id: str = "demo-org"):
    rules = _get_auto_rules(org_id)
    rule = next((r for r in rules if r["id"] == rule_id), None)
    if not rule:
        raise HTTPException(404, f"Rule {rule_id} not found")
    run = _fire_auto_rule(rule, org_id, triggered_by="manual", trigger_detail="manual trigger")
    return {"run": run, "message": f"'{rule['name']}' triggered manually"}


@app.get("/automation/runs")
def list_auto_runs(org_id: str = "demo-org", limit: int = 50):
    key = f"automation:runs:{org_id}"
    try:
        runs = _rlist(key)      # appended via _rpush
    except Exception:
        runs = []
    if not runs:
        runs = list(_DEFAULT_AUTO_RUNS)
    runs_sorted = list(reversed(runs))[:limit]
    return {"runs": runs_sorted, "total": len(runs)}


# ── Fax Webhook ──────────────────────────────────────────────────────────────
#
# Receives real faxes from eFax / SRFax webhooks.
# In demo mode (no EFAX_WEBHOOK_SECRET set) signature verification is skipped.
#
# Flow:
#   POST /fax/inbound  ←  eFax or SRFax fires this when a fax arrives
#     → store fax record
#     → fire fax_received governance event
#     → trigger auto-fax-triage automation rule (if enabled)
#     → run heuristic triage pipeline
#     → return routing decision immediately
#
# POST /fax/inbound/test  — inject a simulated fax (no eFax account needed)
# GET  /fax/queue         — list fax queue with stats
# GET  /fax/{id}          — get one fax record
# PATCH /fax/{id}/resolve — human reviewer accepts / overrides routing

import os as _os

_EFAX_SECRET  = _os.getenv("EFAX_WEBHOOK_SECRET",  "")
_SRFAX_SECRET = _os.getenv("SRFAX_WEBHOOK_SECRET", "")

# ── Keyword classifier (heuristic — no API key needed) ──────────────────────

_DOC_PATTERNS = {
    "referral":     ["referral", "patient referral", "refer to", "consultation", "consult",
                     "please schedule", "requesting physician", "reason for referral",
                     "to: st.", "to: dr.", "from: dr."],
    "lab_result":   ["lab result", "laboratory", "specimen", "potassium", "sodium",
                     "hemoglobin", "wbc", "platelet", "test result", "result:",
                     "critical high", "critical low", "reference range", "clinical laboratory"],
    "prescription": ["prescription", "rx:", "sig:", "dispense", "refills:",
                     "tablet", "capsule", " mg ", " ml ", "take 1", "take 2", "qty:"],
    "prior_auth":   ["prior authorization", "prior auth", "pa request", "precertification",
                     "auth number", "insurance authorization"],
    "eob":          ["explanation of benefits", "eob", "claim number", "amount paid",
                     "member id", "plan name", "deductible"],
}

_STAT_KEYWORDS = ["stat", "urgent", "critical", "immediate", "emergency",
                  "critical high", "critical low", "stat notification"]

_DEPT_MAP = {
    "referral":     "Cardiology",
    "lab_result":   "Ordering Physician",
    "prescription": "Pharmacy",
    "prior_auth":   "Insurance Authorization",
    "eob":          "Billing",
    "unknown":      "Medical Records",
}


def _classify_fax(text: str) -> dict:
    t = text.lower()
    urgency   = "stat" if any(kw in t for kw in _STAT_KEYWORDS) else "routine"
    doc_type  = "unknown"
    best      = 0
    for dtype, keywords in _DOC_PATTERNS.items():
        score = sum(1 for kw in keywords if kw in t)
        if score > best:
            best, doc_type = score, dtype
    confidence = round(min(0.50 + best * 0.15, 0.97), 2) if doc_type != "unknown" else 0.30
    return {
        "doc_type":         doc_type,
        "confidence":       confidence,
        "urgency":          urgency,
        "target_department": _DEPT_MAP.get(doc_type, "Medical Records"),
    }


def _route_fax(doc_type: str, confidence: float, urgency: str):
    if urgency == "stat":
        return "hold_for_review", "STAT urgency — requires immediate human attention"
    if confidence < 0.85:
        return "hold_for_review", f"Confidence {confidence:.0%} below 85% threshold"
    if doc_type == "unknown":
        return "hold_for_review", "Document type could not be determined"
    return "auto_route", f"Routine {doc_type} with {confidence:.0%} confidence"


# ── Fax record storage ───────────────────────────────────────────────────────

def _store_fax(org_id: str, fax: dict):
    _rset(f"fax:{org_id}:{fax['fax_id']}", fax)
    _rpush(f"fax_queue:{org_id}",
           {"fax_id": fax["fax_id"], "received_at": fax["received_at"]},
           maxlen=500)


def _get_fax(org_id: str, fax_id: str):
    return _rget(f"fax:{org_id}:{fax_id}")


def _update_fax(org_id: str, fax_id: str, updates: dict):
    fax = _get_fax(org_id, fax_id) or {}
    fax.update(updates)
    _rset(f"fax:{org_id}:{fax_id}", fax)
    return fax


def _list_faxes(org_id: str, limit: int = 50) -> list:
    queue = _rlist(f"fax_queue:{org_id}")
    result = []
    for item in list(reversed(queue))[:limit]:
        full = _get_fax(org_id, item["fax_id"])
        if full:
            result.append(full)
    return result


# ── Triage pipeline ──────────────────────────────────────────────────────────

def _process_fax(fax: dict, org_id: str) -> dict:
    fax_id   = fax["fax_id"]
    raw_text = fax.get("raw_text") or f"[No text — fax from {fax.get('sender_number','unknown')}]"

    _update_fax(org_id, fax_id, {"status": "processing"})

    clf = _classify_fax(raw_text)
    doc_type   = clf["doc_type"]
    confidence = clf["confidence"]
    urgency    = clf["urgency"]
    target_dept = clf["target_department"]

    tok_color = "#f87171" if urgency == "stat" else _DOC_TYPE_COLOR.get(doc_type, "#888")
    _cpn_emit("fax_received", color=tok_color, doc_type=doc_type,
               urgency=urgency, dept=target_dept, fax_id=fax_id)

    now = datetime.utcnow().isoformat()
    _rpush(f"agent_events:{org_id}", {
        "ts": now, "event_type": "fax_classified", "source": "fax_triage",
        "fax_id": fax_id, "doc_type": doc_type,
        "confidence": confidence, "urgency": urgency,
    }, maxlen=500)

    routing_decision, routing_reason = _route_fax(doc_type, confidence, urgency)

    updates = {
        "doc_type":         doc_type,
        "confidence":       confidence,
        "urgency":          urgency,
        "target_department": target_dept,
        "routing_decision": routing_decision,
        "routing_reason":   routing_reason,
        "processed_at":     now,
    }

    if routing_decision == "auto_route":
        ehr_task_id = f"TASK-{abs(hash(fax_id)) % 99999:05d}"
        updates.update({"status": "auto_routed", "ehr_task_id": ehr_task_id, "escalated": False})
        _rpush(f"agent_events:{org_id}", {
            "ts": now, "event_type": "fax_routed", "source": "fax_triage",
            "fax_id": fax_id, "routing_decision": "auto_route",
            "target_department": target_dept, "ehr_task_id": ehr_task_id,
        }, maxlen=500)
        _cpn_emit("auto_routed", color=tok_color, doc_type=doc_type,
                  dept=target_dept, confidence=confidence, fax_id=fax_id)
    else:
        updates.update({"status": "held_for_review", "escalated": True})
        _rpush(f"agent_events:{org_id}", {
            "ts": now, "event_type": "fax_held", "source": "fax_triage",
            "fax_id": fax_id, "routing_reason": routing_reason, "urgency": urgency,
        }, maxlen=500)
        _cpn_emit("held_for_review", color=tok_color, doc_type=doc_type,
                  urgency=urgency, reason=routing_reason, fax_id=fax_id)

    return _update_fax(org_id, fax_id, updates)


# ── Signature validation ─────────────────────────────────────────────────────

def _check_sig(payload: bytes, header_val: str, secret: str) -> bool:
    if not secret:
        return True  # demo mode — skip
    import hmac as _h, hashlib as _hs
    expected = _h.new(secret.encode(), payload, _hs.sha256).hexdigest()
    return _h.compare_digest(expected, header_val or "")


# ── Shared fax record factory ────────────────────────────────────────────────

def _make_fax_record(org_id: str, fax_id: str, provider: str, sender: str,
                     recipient: str, pages: int, raw_text: str, has_image: bool) -> dict:
    return {
        "fax_id":            fax_id,
        "org_id":            org_id,
        "provider":          provider,
        "received_at":       datetime.utcnow().isoformat(),
        "sender_number":     sender,
        "recipient_number":  recipient,
        "pages":             pages,
        "has_image":         has_image,
        "raw_text":          raw_text,
        "status":            "pending",
        "doc_type":          None,
        "confidence":        None,
        "urgency":           None,
        "routing_decision":  None,
        "target_department": None,
        "routing_reason":    None,
        "ehr_task_id":       None,
        "escalated":         False,
        "review_notes":      None,
        "resolved_by":       None,
        "resolved_at":       None,
        "processed_at":      None,
    }


def _ingest_and_process(fax: dict, org_id: str) -> dict:
    """Store, fire governance events, trigger automation rule, run pipeline."""
    _store_fax(org_id, fax)

    now = datetime.utcnow().isoformat()
    _rpush(f"agent_events:{org_id}", {
        "ts": now, "event_type": "fax_received", "source": "fax_webhook",
        "fax_id": fax["fax_id"], "provider": fax["provider"],
        "pages": fax["pages"], "sender": fax["sender_number"],
    }, maxlen=500)

    # Trigger automation rule (same hook as POST /events)
    for rule in _get_auto_rules(org_id):
        if (rule.get("enabled") and
                rule.get("trigger_type") == "event" and
                rule.get("trigger_config", {}).get("event_type") == "fax_received"):
            _fire_auto_rule(rule, org_id, triggered_by="event",
                            trigger_detail=f"fax: {fax['fax_id']}")

    return _process_fax(fax, org_id)


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.post("/fax/inbound", status_code=202)
async def fax_inbound_webhook(
    request: Request,
    org_id: str = "demo-org",
    x_efax_signature:  Optional[str] = Header(None),
    x_srfax_signature: Optional[str] = Header(None),
):
    """
    Inbound webhook — set this URL in your eFax or SRFax developer console.

    eFax:   Account Settings → Webhooks → Inbound URL
    SRFax:  API Settings     → Webhook  → Inbound URL

    Env vars for signature verification:
      EFAX_WEBHOOK_SECRET   — your eFax webhook signing secret
      SRFAX_WEBHOOK_SECRET  — your SRFax webhook signing secret
    """
    body = await request.body()

    if x_efax_signature:
        if not _check_sig(body, x_efax_signature, _EFAX_SECRET):
            raise HTTPException(401, "Invalid eFax signature")
        provider = "efax"
    elif x_srfax_signature:
        if not _check_sig(body, x_srfax_signature, _SRFAX_SECRET):
            raise HTTPException(401, "Invalid SRFax signature")
        provider = "srfax"
    else:
        provider = "unknown"

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    # Normalise eFax vs SRFax vs generic payload shapes
    if provider == "efax":
        fax_id   = f"FAX-{payload.get('docID', uuid.uuid4().hex[:8])}"
        sender   = payload.get("callerID") or payload.get("remoteCSID", "unknown")
        recip    = payload.get("didNumber", "unknown")
        pages    = int(payload.get("pageCount", 1))
        raw_b64  = payload.get("faxFile") or payload.get("docBase64", "")
    elif provider == "srfax":
        fax_id   = f"FAX-{payload.get('FileName', uuid.uuid4().hex[:8]).replace('.pdf','').replace('.tif','')}"
        sender   = payload.get("CallerID", "unknown")
        recip    = payload.get("ToFaxNumber", "unknown")
        pages    = int(payload.get("Pages", 1))
        raw_b64  = payload.get("PDFFile", "")
    else:
        fax_id   = payload.get("fax_id") or f"FAX-{uuid.uuid4().hex[:6].upper()}"
        sender   = payload.get("sender_number", "unknown")
        recip    = payload.get("recipient_number", "unknown")
        pages    = int(payload.get("pages", 1))
        raw_b64  = payload.get("fax_image_b64", "")

    raw_text = payload.get("raw_text", "")  # test hook — skip OCR in demo

    fax = _make_fax_record(org_id, fax_id, provider, sender, recip, pages,
                           raw_text, bool(raw_b64))
    processed = _ingest_and_process(fax, org_id)

    return {
        "accepted":   True,
        "fax_id":     fax_id,
        "status":     processed.get("status"),
        "routing":    processed.get("routing_decision"),
        "department": processed.get("target_department"),
        "urgency":    processed.get("urgency"),
    }


_FAX_TEST_SCENARIOS = {
    "referral": {
        "sender_number": "+15551230001", "recipient_number": "+15559876543", "pages": 2,
        "raw_text": (
            "PATIENT REFERRAL\n"
            "From: Dr. Sarah Chen MD — Riverside Primary Care\n"
            "To: St. Mary's Hospital Cardiology Department\n\n"
            "Patient: James T. Morrison  DOB: 03/14/1958  MRN: 284710\n"
            "Reason: Intermittent chest pain and dyspnoea on exertion.\n"
            "ECG shows occasional PACs. Request cardiology evaluation and stress test.\n"
            "Urgency: Routine — please schedule within 14 days."
        ),
    },
    "lab_stat": {
        "sender_number": "+15552340002", "recipient_number": "+15559876543", "pages": 1,
        "raw_text": (
            "CRITICAL LAB RESULT — STAT\n"
            "Lakeside Clinical Laboratory\n\n"
            "Patient: Elena Vasquez  DOB: 07/22/1971  MRN: 119843\n"
            "Ordering Physician: Dr. R. Patel\n\n"
            "TEST RESULT:\n"
            "  Potassium (K+): 6.8 mEq/L  *** CRITICAL HIGH ***  Reference: 3.5-5.0\n\n"
            "STAT NOTIFICATION REQUIRED — physician callback within 30 minutes."
        ),
    },
    "prescription": {
        "sender_number": "+15553450003", "recipient_number": "+15559876543", "pages": 1,
        "raw_text": (
            "PRESCRIPTION\n"
            "Dr. Michael Torres MD — Westside Oncology Group  NPI: 9876543210\n\n"
            "Patient: Robert Nguyen  DOB: 11/05/1962  MRN: 330091\n\n"
            "Rx: Ondansetron 8mg ODT\n"
            "Sig: Take 1 tablet 30 min before chemotherapy, then q8h x 24h PRN nausea\n"
            "Qty: 12 tablets    Refills: 0"
        ),
    },
    "unknown": {
        "sender_number": "+15554560004", "recipient_number": "+15559876543", "pages": 3,
        "raw_text": "Blurry fax. Text partially illegible. [illegible] patient [illegible] date [illegible]",
    },
}


@app.post("/fax/inbound/test", status_code=202)
def fax_inbound_test(org_id: str = "demo-org", scenario: str = "referral"):
    """
    Inject a simulated fax — no eFax account needed.
    scenario: referral | lab_stat | prescription | unknown
    """
    data   = _FAX_TEST_SCENARIOS.get(scenario, _FAX_TEST_SCENARIOS["referral"])
    fax_id = f"FAX-{scenario[:3].upper()}-{uuid.uuid4().hex[:4].upper()}"
    fax    = _make_fax_record(
        org_id, fax_id, "test",
        data["sender_number"], data["recipient_number"], data["pages"],
        data["raw_text"], False,
    )
    processed = _ingest_and_process(fax, org_id)
    return {
        "accepted":    True,
        "fax_id":      fax_id,
        "scenario":    scenario,
        "status":      processed.get("status"),
        "doc_type":    processed.get("doc_type"),
        "confidence":  processed.get("confidence"),
        "urgency":     processed.get("urgency"),
        "routing":     processed.get("routing_decision"),
        "department":  processed.get("target_department"),
    }


@app.get("/fax/queue")
def list_fax_queue(org_id: str = "demo-org",
                   status: Optional[str] = None,
                   limit: int = 50):
    faxes = _list_faxes(org_id, limit=limit)
    if status:
        faxes = [f for f in faxes if f.get("status") == status]
    held  = [f for f in faxes if f.get("status") == "held_for_review"]
    return {
        "faxes":          faxes,
        "total":          len(faxes),
        "pending":        sum(1 for f in faxes if f.get("status") in ("pending", "processing")),
        "held_for_review": len(held),
        "stat_pending":   sum(1 for f in held if f.get("urgency") == "stat"),
        "auto_routed":    sum(1 for f in faxes if f.get("status") == "auto_routed"),
        "resolved":       sum(1 for f in faxes if f.get("status") == "resolved"),
    }


@app.get("/fax/{fax_id}")
def get_fax(fax_id: str, org_id: str = "demo-org"):
    fax = _get_fax(org_id, fax_id)
    if not fax:
        raise HTTPException(404, f"Fax {fax_id} not found")
    return fax


class FaxResolveRequest(BaseModel):
    resolved_by:         str = "reviewer"
    review_notes:        Optional[str] = None
    override_department: Optional[str] = None


@app.patch("/fax/{fax_id}/resolve")
def resolve_fax(fax_id: str, body: FaxResolveRequest, org_id: str = "demo-org"):
    """Human reviewer marks a held fax as resolved, optionally overriding the routing."""
    fax = _get_fax(org_id, fax_id)
    if not fax:
        raise HTTPException(404, f"Fax {fax_id} not found")
    if fax.get("status") == "resolved":
        raise HTTPException(400, f"Fax {fax_id} is already resolved")

    now     = datetime.utcnow().isoformat()
    updates = {
        "status":       "resolved",
        "resolved_by":  body.resolved_by,
        "resolved_at":  now,
        "review_notes": body.review_notes,
    }
    if body.override_department:
        updates["target_department"] = body.override_department
        updates["routing_decision"]  = "human_routed"

    _rpush(f"agent_events:{org_id}", {
        "ts": now, "event_type": "fax_resolved", "source": "fax_review",
        "fax_id": fax_id, "resolved_by": body.resolved_by,
        "department": updates.get("target_department", fax.get("target_department")),
    }, maxlen=500)

    tok_color = _DOC_TYPE_COLOR.get(fax.get("doc_type", "unknown"), "#888")
    _cpn_emit("human_approved", color=tok_color,
               doc_type=fax.get("doc_type"), resolved_by=body.resolved_by,
               dept=updates.get("target_department", fax.get("target_department")),
               fax_id=fax_id)

    updated = _update_fax(org_id, fax_id, updates)
    return {"fax_id": fax_id, "status": "resolved", "fax": updated}


# ─────────────────────────────────────────────────────────────────────────────

# Simulated connection state per org stored in Redis
def _get_connected(org_id: str):
    raw = _rlist(f"setup:connectors:{org_id}")
    return {r["id"]: r for r in raw} if raw else {}

def _set_connected(org_id: str, connector_id: str, status: dict):
    existing = _get_connected(org_id)
    existing[connector_id] = status
    key = f"setup:connectors:{org_id}"
    if REDIS_AVAILABLE:
        _redis.delete(key)
        for v in existing.values():
            _redis.rpush(key, json.dumps(v))
    else:
        _mem_store[key] = list(existing.values())


_RUNBOOK_CONTENT = {
    "it_agent": {
        "title": "IT Help Desk Agent",
        "subtitle": "Moving from your current ticketing tool to Tessera ITSM",
        "icon": "🎫",
        "phases": [
            {
                "phase": "Week 1 — Parallel running",
                "goal": "See your existing tickets inside Tessera. Keep using your old system.",
                "steps": [
                    {"num": 1, "title": "Connect your existing IT system", "in_tessera": True, "in_old": False,
                     "action": "Go to Setup & Integration → connect ServiceNow or Jira. Your existing open tickets will appear in Tessera ITSM within 5 minutes.",
                     "replaces": "Nothing yet — read-only view of existing tickets."},
                    {"num": 2, "title": "Open My Feed every morning", "in_tessera": True, "in_old": True,
                     "action": "Tessera will show your assigned tickets in My Feed → 'Needs your action'. Resolve them in your existing system as usual.",
                     "replaces": "The morning ritual of opening ServiceNow/Jira and checking your queue."},
                    {"num": 3, "title": "Use Tessera to look up ticket history", "in_tessera": True, "in_old": True,
                     "action": "When you need to find a past ticket or check CMDB, search in Tessera ITSM instead of your old system. Get comfortable with the interface.",
                     "replaces": "Searching in ServiceNow/Jira for historical tickets."},
                ],
            },
            {
                "phase": "Week 2–3 — Active triage in Tessera",
                "goal": "Resolve tickets from Tessera. New tickets created in Tessera automatically sync back.",
                "steps": [
                    {"num": 4, "title": "Triage and resolve from Tessera", "in_tessera": True, "in_old": False,
                     "action": "When a ticket appears in My Feed, click 'Review ticket' to open it in Tessera ITSM. Resolve, comment, and reassign from here. Changes sync back to your old system.",
                     "replaces": "Opening and updating tickets in ServiceNow/Jira."},
                    {"num": 5, "title": "Create new tickets in Tessera", "in_tessera": True, "in_old": False,
                     "action": "For new requests, create tickets in Tessera ITSM. They will sync to your existing system automatically. Users can still email or use the old portal — tickets appear in both.",
                     "replaces": "Creating tickets in ServiceNow/Jira portal."},
                    {"num": 6, "title": "Check the AI agent performance tab", "in_tessera": True, "in_old": False,
                     "action": "In ITSM, open 'Agent Performance'. See which categories your AI agent is resolving vs escalating to you. Flag categories where the AI is getting it wrong.",
                     "replaces": "No equivalent in old system — this is new visibility."},
                ],
            },
            {
                "phase": "Month 2 — Tessera is primary",
                "goal": "Old system is reference-only. ServiceNow/Jira licence under review.",
                "steps": [
                    {"num": 7, "title": "Stop checking the old system daily", "in_tessera": True, "in_old": False,
                     "action": "My Feed and ITSM cover everything. Only open the old system if a stakeholder sends you a link to a historical record you can't find in Tessera.",
                     "replaces": "Daily login to ServiceNow/Jira."},
                    {"num": 8, "title": "Tell your manager the old system is unused", "in_tessera": True, "in_old": False,
                     "action": "Your IT manager will check the utilisation report on the old system. If it shows <10% active users for 4 weeks, they'll raise the decommission request with procurement.",
                     "replaces": "Paying for unused ServiceNow/Jira seats."},
                ],
            },
        ],
    },
    "hr_coordinator": {
        "title": "HR / Labor Coordinator",
        "subtitle": "Moving from ADP / BambooHR / Workday time modules to Tessera",
        "icon": "👤",
        "phases": [
            {
                "phase": "Week 1 — Import your employee list",
                "goal": "Get your people into Tessera without touching existing payroll workflows.",
                "steps": [
                    {"num": 1, "title": "Connect ADP or export a CSV", "in_tessera": True, "in_old": True,
                     "action": "Go to Setup & Integration → ADP Workforce Now → Connect. Your employee list, cost centres, and pay groups import automatically. If you can't OAuth, export a CSV from ADP and upload it.",
                     "replaces": "Manual employee list maintenance in HR systems."},
                    {"num": 2, "title": "Verify employee records in Tessera", "in_tessera": True, "in_old": True,
                     "action": "In Workforce Planning, open the employee list. Check that names, cost centres, and roles match your ADP records. Flag any mismatches before going further.",
                     "replaces": "HR audit of employee data in ADP."},
                    {"num": 3, "title": "Set up leave policies", "in_tessera": True, "in_old": True,
                     "action": "In Time & Attendance → Policies, create your standard leave types (annual, sick, parental). Import leave balances from ADP's balance export. Takes 20 minutes.",
                     "replaces": "Leave policy configuration in ADP/BambooHR."},
                ],
            },
            {
                "phase": "Week 2–4 — Leave requests move to Tessera",
                "goal": "Employees submit leave requests in Tessera. ADP still runs payroll from a CSV export.",
                "steps": [
                    {"num": 4, "title": "Send employees their Tessera login", "in_tessera": True, "in_old": True,
                     "action": "In Identity → Invite Users, bulk-invite employees via their work email. They click the link, set a password, and see their Time & Attendance dashboard immediately.",
                     "replaces": "Emailing employees their ADP/BambooHR portal credentials."},
                    {"num": 5, "title": "Process leave approvals in My Feed", "in_tessera": True, "in_old": False,
                     "action": "When an employee submits leave in Tessera, you'll see it in My Feed → 'Approve / Decline'. One click. The employee gets notified instantly. Their balance updates automatically.",
                     "replaces": "Approving leave in ADP/BambooHR, then emailing the employee."},
                    {"num": 6, "title": "Export timesheets to ADP for payroll run", "in_tessera": True, "in_old": True,
                     "action": "At end of each pay period: Time & Attendance → Export → ADP format CSV. Upload this to ADP for the payroll run. You don't need to re-enter anything.",
                     "replaces": "Manually collating timesheets into ADP."},
                ],
            },
            {
                "phase": "Month 2–3 — Full T&A in Tessera",
                "goal": "ADP is payroll-only. All T&A managed in Tessera.",
                "steps": [
                    {"num": 7, "title": "Deprecate ADP's time module", "in_tessera": True, "in_old": False,
                     "action": "Tell your ADP account manager you no longer need the Time & Attendance and Leave modules. Keep only the Payroll and Direct Deposit modules. Average saving: $8–22/employee/month.",
                     "replaces": "ADP T&A module licence."},
                    {"num": 8, "title": "Run workforce analytics from Tessera", "in_tessera": True, "in_old": False,
                     "action": "In Workforce Planning, your headcount, cost, and AI task distribution data is now live. The LBI and ROAI dashboards start generating insights from your team's actual work patterns.",
                     "replaces": "Manual headcount reporting in Excel or Workday Prism."},
                ],
            },
        ],
    },
    "manager": {
        "title": "Department Manager / Head of Market360",
        "subtitle": "How Tessera surfaces what you need to see — without changing how your team works",
        "icon": "📊",
        "phases": [
            {
                "phase": "Week 1 — No action needed. Just watch.",
                "goal": "Your team sets up connectors. You get a read-only view of what they're doing.",
                "steps": [
                    {"num": 1, "title": "Open My Feed on Monday morning", "in_tessera": True, "in_old": True,
                     "action": "Log in. See: pending approvals, agent review flags, and the weekly digest strip at the top. No training needed — if something needs your decision, it's in 'Needs your action'.",
                     "replaces": "Checking multiple systems (ServiceNow, ADP portal, email) for things that need your attention."},
                    {"num": 2, "title": "Approve the first leave request from Tessera", "in_tessera": True, "in_old": False,
                     "action": "When HR sets up Tessera T&A, leave requests route to your My Feed. One click to approve. You never need to log into ADP for this again.",
                     "replaces": "Logging into ADP manager portal to approve leave."},
                ],
            },
            {
                "phase": "Month 1 — See your team's AI performance",
                "goal": "Understand what your AI agents are delivering vs costing.",
                "steps": [
                    {"num": 3, "title": "Open ROAI Dashboard once a week", "in_tessera": True, "in_old": False,
                     "action": "ROAI → Dashboard. Look at one number: your AI's fitness score this week vs last week. If it dropped, the reason is in the task breakdown below.",
                     "replaces": "Guessing whether AI is delivering value. No equivalent existed before."},
                    {"num": 4, "title": "Review the compute waste bar", "in_tessera": True, "in_old": False,
                     "action": "In Compute Waste → see which task categories your AI is running that produce low-quality output. Click 'Suppress' on the worst ones. Your cloud bill will drop within a week.",
                     "replaces": "Not having visibility into this at all."},
                ],
            },
            {
                "phase": "Month 2+ — Tessera is your operating view",
                "goal": "One screen replaces the weekly status email, the BI dashboard, and the HR portal.",
                "steps": [
                    {"num": 5, "title": "Forward the weekly digest to your VP", "in_tessera": True, "in_old": False,
                     "action": "Every Monday: My Feed → the digest strip shows ROAI trend, compute waste, leap signals, karma. Screenshot it or use the share button. That's your status update.",
                     "replaces": "Writing a weekly AI/ops update for leadership."},
                    {"num": 6, "title": "Use Hierarchy Analyzer before headcount decisions", "in_tessera": True, "in_old": False,
                     "action": "Before approving a role elimination or restructure: LBI & Hierarchy → Hierarchy Analyzer. Check all 3 elimination conditions for the affected roles. Avoid the Bolt/Intuit mistake.",
                     "replaces": "Org restructure decisions based on cost alone, without signal-flow analysis."},
                ],
            },
        ],
    },
    "frontline": {
        "title": "Frontline Employee",
        "subtitle": "What changes for you — and what stays the same",
        "icon": "👋",
        "phases": [
            {
                "phase": "Day 1 — One link, that's it",
                "goal": "Get logged in. Everything you used to do elsewhere is here.",
                "steps": [
                    {"num": 1, "title": "Click the invite link from HR", "in_tessera": True, "in_old": False,
                     "action": "You'll receive an email from Tessera. Click 'Accept invite', set your password, and you're in. You'll see your profile, leave balance, and any IT tickets you've raised.",
                     "replaces": "Separate logins for the IT portal, ADP employee self-service, and HR system."},
                    {"num": 2, "title": "Log a leave request", "in_tessera": True, "in_old": False,
                     "action": "Time & Attendance → Request Leave → pick dates → submit. Your manager sees it in their My Feed and approves or declines with one click. You get notified instantly.",
                     "replaces": "Emailing your manager or logging into ADP to request leave."},
                ],
            },
            {
                "phase": "Week 1 — Raise IT issues here",
                "goal": "Stop emailing IT. Raise a ticket and track it.",
                "steps": [
                    {"num": 3, "title": "Raise an IT ticket", "in_tessera": True, "in_old": False,
                     "action": "ITSM → New Ticket → describe your issue → submit. You'll see the ticket status update in real time. If an AI agent resolves it, you'll get notified. If it needs a human, you'll see who's on it.",
                     "replaces": "Emailing helpdesk@company.com and waiting. Or logging into the old IT portal."},
                    {"num": 4, "title": "Check your ticket status", "in_tessera": True, "in_old": False,
                     "action": "My Feed will show a notification when your ticket status changes. No need to follow up via email.",
                     "replaces": "Sending a follow-up email to IT asking for an update."},
                ],
            },
            {
                "phase": "Month 1 — This is your daily tool",
                "goal": "Tessera is the only work portal you need to open.",
                "steps": [
                    {"num": 5, "title": "Stop logging into the old IT portal and ADP", "in_tessera": True, "in_old": False,
                     "action": "Everything you used those for — tickets, leave, your payslip (coming soon), onboarding documents — is in Tessera. If you can't find something, use the search bar at the top.",
                     "replaces": "Multiple portal logins and password resets."},
                ],
            },
        ],
    },
}


@app.get("/setup/connectors")
def get_setup_connectors(org_id: str = "demo-org"):
    """
    List all available connectors with connection status for this org.
    """
    connected = _get_connected(org_id)
    result = []
    for c in _CONNECTORS:
        status = connected.get(c["id"], {})
        result.append({
            **c,
            "connected": bool(status),
            "connected_at": status.get("connected_at"),
            "records_synced": status.get("records_synced", 0),
            "last_sync": status.get("last_sync"),
        })
    return {"org_id": org_id, "connectors": result}


@app.post("/setup/connectors/{connector_id}/connect")
def connect_system(connector_id: str, org_id: str = "demo-org"):
    """
    Simulate connecting an external system.
    In production this would initiate the OAuth / API key flow.
    """
    matched = next((c for c in _CONNECTORS if c["id"] == connector_id), None)
    if not matched:
        raise HTTPException(404, f"Connector '{connector_id}' not found")

    import random
    rng = random.Random(hash(connector_id + org_id) % 9999)
    records = {
        "servicenow": 347, "jira_sm": 512, "adp": 89,
        "workday": 134, "bamboohr": 76, "ms_teams": 0,
        "slack": 0, "csv_upload": 0,
    }.get(connector_id, rng.randint(20, 200))

    status = {
        "id": connector_id,
        "connected": True,
        "connected_at": datetime.now(timezone.utc).isoformat(),
        "records_synced": records,
        "last_sync": datetime.now(timezone.utc).isoformat(),
    }
    _set_connected(org_id, connector_id, status)
    return {
        "success": True,
        "connector_id": connector_id,
        "records_synced": records,
        "message": f"Connected to {matched['name']}. {records} records imported.",
    }


@app.delete("/setup/connectors/{connector_id}/disconnect")
def disconnect_system(connector_id: str, org_id: str = "demo-org"):
    existing = _get_connected(org_id)
    existing.pop(connector_id, None)
    key = f"setup:connectors:{org_id}"
    if REDIS_AVAILABLE:
        _redis.delete(key)
        for v in existing.values():
            _redis.rpush(key, json.dumps(v))
    else:
        _mem_store[key] = list(existing.values())
    return {"success": True, "connector_id": connector_id}


@app.post("/events")
def ingest_agent_event(payload: dict, org_id: str = "demo-org"):
    """
    Generic agent telemetry endpoint. Called by TesseraCallback (LangChain),
    OTEL collectors, or any custom agent. Accumulates into governance exhaust.
    """
    event_type = payload.get("event_type", "unknown")
    source     = payload.get("source", "unknown")

    # Persist rolling event log per org (last 500 events)
    key = f"agent_events:{org_id}"
    entry = {
        "ts": datetime.utcnow().isoformat(),
        "event_type": event_type,
        "source": source,
        **{k: v for k, v in payload.items() if k not in ("event_type", "source", "org_id")},
    }
    if REDIS_AVAILABLE:
        _redis.rpush(key, json.dumps(entry))
        _redis.ltrim(key, -500, -1)
    else:
        lst = _mem_store.get(key, [])
        lst.append(entry)
        _mem_store[key] = lst[-500:]

    # Emit to CPN live stream
    _cpn_emit(
        "held_for_review" if event_type in ("escalation", "agent_escalate") else "auto_routed",
        color="#f87171" if event_type in ("escalation", "agent_escalate") else "#60a5fa",
        doc_type=event_type, dept=source, source=source,
    )

    # When an escalation event arrives, create a governance signal
    if event_type in ("escalation", "agent_escalate"):
        sig_key = f"governance_signals:{org_id}"
        sig = {
            "id": f"lc_{int(datetime.utcnow().timestamp())}",
            "type": "agent_escalation",
            "source": source,
            "title": payload.get("title", "Agent escalated to human"),
            "description": payload.get("context", ""),
            "ts": entry["ts"],
        }
        if REDIS_AVAILABLE:
            _redis.rpush(sig_key, json.dumps(sig))
        else:
            _mem_store.setdefault(sig_key, []).append(sig)

    # Fire any event-triggered automation rules that match
    for rule in _get_auto_rules(org_id):
        if (rule.get("enabled") and
                rule.get("trigger_type") == "event" and
                rule.get("trigger_config", {}).get("event_type") == event_type):
            _fire_auto_rule(rule, org_id,
                            triggered_by="event",
                            trigger_detail=f"event: {event_type}")

    return {"accepted": True, "event_type": event_type, "org_id": org_id}


@app.get("/events/summary")
def get_agent_event_summary(org_id: str = "demo-org"):
    """Aggregated view of agent telemetry for the ROAI and Signals views."""
    key = f"agent_events:{org_id}"
    raw = _rlist(key)

    total        = len(raw)
    auto_closed  = sum(1 for e in raw if e.get("auto_resolved") is True)
    escalations  = sum(1 for e in raw if e.get("event_type") in ("escalation", "agent_escalate"))
    sources      = {}
    for e in raw:
        s = e.get("source", "unknown")
        sources[s] = sources.get(s, 0) + 1

    roai = round((auto_closed / total) * 4.5, 2) if total > 0 else 0.0

    return {
        "org_id": org_id,
        "total_events": total,
        "auto_resolved": auto_closed,
        "escalations": escalations,
        "sources": sources,
        "computed_roai": roai,
        "deflection_rate": round(auto_closed / total, 3) if total > 0 else 0.0,
    }


@app.get("/setup/runbooks")
def get_runbooks(audience: str = "it_agent"):
    """
    Audience-specific migration runbook.
    audiences: it_agent | hr_coordinator | manager | frontline
    """
    rb = _RUNBOOK_CONTENT.get(audience)
    if not rb:
        raise HTTPException(404, f"Runbook for audience '{audience}' not found")
    total_steps = sum(len(p["steps"]) for p in rb["phases"])
    return {
        "audience": audience,
        "title": rb["title"],
        "subtitle": rb["subtitle"],
        "icon": rb["icon"],
        "phases": rb["phases"],
        "total_steps": total_steps,
        "audiences_available": list(_RUNBOOK_CONTENT.keys()),
    }


# ── RBAC / Multi-tenant Auth ──────────────────────────────────────────────────

_TESSERA_SECRET = os.getenv("TESSERA_SECRET", "tessera-rbac-dev-2026")

DEMO_USERS: dict = {
    "market360": {
        "admin@market360.com":   {"name": "Sarah Chen",      "role": "org_admin",      "password": "demo123"},
        "it@market360.com":      {"name": "Marcus Williams", "role": "it_agent",        "password": "demo123"},
        "hr@market360.com":      {"name": "Priya Sharma",    "role": "hr_coordinator",  "password": "demo123"},
        "head@market360.com":    {"name": "James Okafor",    "role": "manager",         "password": "demo123"},
        "analyst@market360.com": {"name": "Elena Kovacs",    "role": "analyst",         "password": "demo123"},
        "emp@market360.com":     {"name": "Tom Rivera",      "role": "employee",        "password": "demo123"},
    },
    "bayer-demo": {
        "admin@bayer-demo.com": {"name": "Lena Schmidt",   "role": "org_admin", "password": "demo123"},
        "head@bayer-demo.com":  {"name": "Hendrik Müller", "role": "manager",   "password": "demo123"},
    },
    "tessera": {
        "super@tessera.io": {"name": "Raj Duwarahan", "role": "super_admin", "password": "demo123"},
    },
}

ROLE_MODULES: dict = {
    "super_admin": "*",
    "org_admin":   "*",
    "manager": [
        "my-feed", "signals", "dashboard", "twin", "workforce",
        "roai-dashboard", "compute-waste", "misalignment-monitor", "karma-economy", "regime-compare",
        "lbi-dashboard", "hierarchy-analyzer", "leap-channel", "gov-unlearning", "lbi-scenarios",
        "integration-setup", "runbooks", "why-tessera",
    ],
    "it_agent": ["my-feed", "itsm", "agents", "catalog", "knowledge", "goals", "why-tessera"],
    "hr_coordinator": [
        "my-feed", "people", "timeattendance", "workforce",
        "onboarding", "absence", "learning", "import", "payroll", "why-tessera",
    ],
    "analyst": [
        "my-feed", "dashboard", "twin",
        "roai-dashboard", "compute-waste", "misalignment-monitor", "regime-compare",
        "lbi-dashboard", "hierarchy-analyzer", "leap-channel", "gov-unlearning", "lbi-scenarios",
        "why-tessera",
    ],
    "employee": ["my-feed", "timeattendance", "absence"],
}

ROLE_LABELS: dict = {
    "super_admin":    {"label": "Super Admin",    "color": "#A07BE5"},
    "org_admin":      {"label": "Org Admin",      "color": "#4A8EE5"},
    "manager":        {"label": "Manager",        "color": "#E5A83A"},
    "it_agent":       {"label": "IT Agent",       "color": "#6FCF4A"},
    "hr_coordinator": {"label": "HR Coordinator", "color": "#6FCF4A"},
    "analyst":        {"label": "Analyst",        "color": "#A07BE5"},
    "employee":       {"label": "Employee",       "color": "rgba(242,240,232,.5)"},
}


def _make_token(user: dict) -> str:
    payload = {
        "user_id": user["email"],
        "org_id":  user["org_id"],
        "role":    user["role"],
        "name":    user["name"],
        "email":   user["email"],
        "exp":     int(time.time()) + 86400 * 7,
    }
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(_TESSERA_SECRET.encode(), b64.encode(), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def _decode_token(token: str) -> Optional[dict]:
    try:
        b64, sig = token.rsplit(".", 1)
        expected = hmac.new(_TESSERA_SECRET.encode(), b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padding = (4 - len(b64) % 4) % 4
        payload = json.loads(base64.urlsafe_b64decode(b64 + "=" * padding).decode())
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/auth/login")
def auth_login(req: LoginRequest):
    for org_id, users in DEMO_USERS.items():
        if req.email in users:
            u = users[req.email]
            if u["password"] != req.password:
                raise HTTPException(status_code=401, detail="Invalid password")
            token = _make_token({"email": req.email, "name": u["name"], "role": u["role"], "org_id": org_id})
            meta = ROLE_LABELS.get(u["role"], {})
            return {
                "token": token,
                "user": {
                    "email":       req.email,
                    "name":        u["name"],
                    "role":        u["role"],
                    "role_label":  meta.get("label", u["role"]),
                    "role_color":  meta.get("color", "#F2F0E8"),
                    "org_id":      org_id,
                    "permissions": ROLE_MODULES.get(u["role"], []),
                },
            }
    raise HTTPException(status_code=401, detail="User not found")


@app.get("/auth/me")
def auth_me(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="No token")
    payload = _decode_token(authorization[7:])
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    meta = ROLE_LABELS.get(payload["role"], {})
    return {
        "email":       payload["email"],
        "name":        payload["name"],
        "role":        payload["role"],
        "role_label":  meta.get("label", payload["role"]),
        "role_color":  meta.get("color", "#F2F0E8"),
        "org_id":      payload["org_id"],
        "permissions": ROLE_MODULES.get(payload["role"], []),
    }


@app.get("/auth/users")
def auth_list_users(
    org_id: str = Query(default="market360"),
    authorization: Optional[str] = Header(default=None),
):
    caller = None
    if authorization and authorization.startswith("Bearer "):
        caller = _decode_token(authorization[7:])
    target = caller["org_id"] if caller and caller.get("role") != "super_admin" else org_id
    users = DEMO_USERS.get(target, {})
    result = []
    for email, u in users.items():
        meta = ROLE_LABELS.get(u["role"], {})
        result.append({
            "email":      email,
            "name":       u["name"],
            "role":       u["role"],
            "role_label": meta.get("label", u["role"]),
            "role_color": meta.get("color", "#F2F0E8"),
            "org_id":     target,
            "active":     True,
        })
    return {"org_id": target, "users": result, "total": len(result)}


@app.patch("/auth/users/{email}/role")
def auth_update_role(email: str, body: dict, authorization: Optional[str] = Header(default=None)):
    caller = None
    if authorization and authorization.startswith("Bearer "):
        caller = _decode_token(authorization[7:])
    if not caller or caller.get("role") not in ("org_admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin role required")
    new_role = body.get("role", "")
    if new_role not in ROLE_MODULES:
        raise HTTPException(status_code=400, detail="Invalid role")
    for org_id, users in DEMO_USERS.items():
        if email in users:
            if caller.get("role") != "super_admin" and org_id != caller.get("org_id"):
                raise HTTPException(status_code=403, detail="Cross-org not permitted")
            DEMO_USERS[org_id][email]["role"] = new_role
            return {"email": email, "role": new_role, "org_id": org_id}
    raise HTTPException(status_code=404, detail="User not found")


@app.get("/auth/tenants")
def auth_list_tenants(authorization: Optional[str] = Header(default=None)):
    caller = None
    if authorization and authorization.startswith("Bearer "):
        caller = _decode_token(authorization[7:])
    if not caller or caller.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin only")
    tenants = []
    org_names = {"market360": "Market360", "bayer-demo": "Bayer (Demo)", "tessera": "Tessera Internal"}
    for org_id, users in DEMO_USERS.items():
        tenants.append({
            "org_id":     org_id,
            "name":       org_names.get(org_id, org_id),
            "user_count": len(users),
            "plan":       "Internal" if org_id == "tessera" else "Enterprise",
            "active":     True,
        })
    return {"tenants": tenants, "total": len(tenants)}


@app.get("/auth/roles")
def auth_roles():
    return {"roles": [
        {
            "role":         role,
            "label":        ROLE_LABELS.get(role, {}).get("label", role),
            "color":        ROLE_LABELS.get(role, {}).get("color", "#F2F0E8"),
            "modules":      modules,
            "module_count": len(modules) if isinstance(modules, list) else "all",
        }
        for role, modules in ROLE_MODULES.items()
    ]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0",
                port=int(os.getenv("SERVICE_PORT", 8008)))
