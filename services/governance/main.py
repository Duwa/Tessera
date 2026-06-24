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
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from enum import Enum

from fastapi import FastAPI, HTTPException, Query
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0",
                port=int(os.getenv("SERVICE_PORT", 8008)))
