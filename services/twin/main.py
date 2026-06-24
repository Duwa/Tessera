"""
Tessera Digital Twin Service  —  port 8004
==========================================
Full 5-paper CPN-NK engine with persistence and live org calibration.

  Paper 1: Garbage Can CPN on NK landscape (macro-agent, org-memory)
  Paper 2: Hybrid Human-AI CPN — RAG, 3 learning regimes, token budget
  Paper 3: MisalignedCPN — belief-misalignment pathology (Q:F, gap, drift)
  Paper 4: TopologyCPN — BPMN topology, V_net cost model, IT:HR ratio
  Paper 5: HAVCPN — Human Alignment Value, HAV crossover theorem, alignment premium

Capital unit types supported:
  human       — biological, has HAV (contributes to mean_hav)
  ai_agent    — software autonomous unit (counted in φ numerator)
  autonomous  — physical autonomous unit (weighted by autonomy_level in φ)

Calibration pipeline:
  POST /orgs/{org_id}/calibrate
    → reads people service for capital composition (n_human, n_ai, n_autonomous)
    → reads performance service for real mean_hav
    → initialises calibrated twin from real data
    → persists φ history to PostgreSQL

Governance contract:
  GET /sim/{id}/early-warning   stage 0/1/2 + plain-English alerts
  GET /sim/{id}/hav             HAV dashboard per epoch
  GET /sim/{id}/vnet            V_net + IT:HR history
  GET /sim/{id}/phi             φ + crossover status
  GET /orgs/{id}/phi-history    persistent φ series across restarts
"""

import os
import sys
import json
import uuid
import asyncpg
import httpx
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "sim"))

from tokens import OrgMemory
from landscape import NKLandscape
from agents import AgentPool
from macro import MacroAgent
from tokens_p4 import ProcessTopology, RoleMode
from tokens_p5 import MeasurementRegime
from net_p5 import HAVCPN

DATABASE_URL      = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@localhost:5432/tessera_twin")
PEOPLE_URL        = os.getenv("PEOPLE_URL",      "http://people:8005")
PERFORMANCE_URL   = os.getenv("PERFORMANCE_URL", "http://performance:8020")
TIME_ATTEND_URL   = os.getenv("TIME_ATTENDANCE_URL", "http://time-attendance:8017")

db: asyncpg.Pool | None = None

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS sim_registry (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL,
    label               TEXT DEFAULT 'default',
    params              JSONB NOT NULL,
    epochs_run          INT DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    last_run_at         TIMESTAMPTZ,
    last_calibrated_at  TIMESTAMPTZ,
    calibration_source  JSONB
);
CREATE INDEX IF NOT EXISTS idx_sim_org ON sim_registry(org_id);

CREATE TABLE IF NOT EXISTS phi_history (
    id              BIGSERIAL PRIMARY KEY,
    sim_id          TEXT NOT NULL REFERENCES sim_registry(id) ON DELETE CASCADE,
    org_id          TEXT NOT NULL,
    epoch_num       INT NOT NULL,
    recorded_at     TIMESTAMPTZ DEFAULT NOW(),
    phi             FLOAT,
    phi_star        FLOAT,
    mean_hav        FLOAT,
    alignment_gap   FLOAT,
    template_drift  FLOAT,
    macro_fitness   FLOAT,
    track2_nudge    BOOLEAN DEFAULT FALSE,
    n_human         INT,
    n_ai            INT,
    n_autonomous    INT DEFAULT 0,
    full_result     JSONB
);
CREATE INDEX IF NOT EXISTS idx_phi_sim     ON phi_history(sim_id, epoch_num);
CREATE INDEX IF NOT EXISTS idx_phi_org     ON phi_history(org_id, recorded_at DESC);
"""

# ── In-memory registry (rebuilt from DB on startup) ──────────────────────────
_sims: Dict[str, dict] = {}

# ── Calibrated baseline (Papers 1-5) ─────────────────────────────────────────
BASELINE: Dict[str, Any] = {
    "lambda_P": 0.8, "lambda_S": 0.6,
    "n_human": 20, "n_ai": 10, "max_ai": 30,
    "budget_ceiling": 10_000, "r_replenish": 2_000,
    "c_part": 50, "c_rag": 30, "c_gen": 20, "c_probe": 100,
    "onboarding_cost": 100, "rag_depth": 2, "eps_rag": 0.3,
    "deadline_min": 20, "deadline_max": 60,
    "theta_oversight": 15, "tau_epoch": 50, "tau_probe": 20,
    "theta_replace": 0.25, "xi_trust": 3, "hcm_rate": 0.03,
    "consolidation_lr": 0.55, "N": 12, "K": 4,
    "corr_failure_rate": 0.18, "corr_failure_interval": 35,
    "alignment_coupling_prob": 0.45, "alignment_weight": 0.6,
    "template_drift_rate": 0.15,
    "topology": ProcessTopology.FLAT.value,
    "role_mode": RoleMode.ASSISTANT.value,
    "lambda_mis": 0.50, "theta_gateway": 0.50,
    "theta_gap_gate": 0.07, "v_d": 1000.0,
    "c_comp": 100.0, "c_gov": 66.0, "c_rem": 5000.0,
    "measurement_regime": MeasurementRegime.HAV.value,
    "theta_novel": 0.30, "theta_replace_hav": 0.10,
    "org_type": "profit",
}

LANDSCAPE_SEED = 42


def _build_sim(params: dict, seed: int) -> dict:
    rng       = np.random.default_rng(seed)
    landscape = NKLandscape(N=params["N"], K=params["K"], seed=LANDSCAPE_SEED)
    macro     = MacroAgent(params["N"], landscape, rng,
                           consolidation_lr=params.get("consolidation_lr", 0.55))
    org_memory = OrgMemory()
    pool = AgentPool(
        rng, landscape, params["N"], macro.template,
        n_human=params["n_human"], n_ai=params["n_ai"],
        hcm_rate=params.get("hcm_rate", 0.03),
        token_balance_init=params.get("budget_ceiling", 10_000) // 10,
        rag_depth=params.get("rag_depth", 2),
    )
    net = HAVCPN(params, rng, landscape, pool, macro, org_memory)
    return {"net": net, "pool": pool, "macro": macro, "org_memory": org_memory,
            "landscape": landscape, "params": params, "seed": seed, "epochs_run": 0}


def _safe(r) -> Any:
    if isinstance(r, dict):   return {k: _safe(v) for k, v in r.items()}
    if isinstance(r, (list, tuple)): return [_safe(v) for v in r]
    if isinstance(r, np.ndarray):   return r.tolist()
    if isinstance(r, np.integer):   return int(r)
    if isinstance(r, np.floating):  return float(r)
    return r


async def _persist_epochs(sim_id: str, org_id: str, results: list,
                           phi_star: float, n_human: int, n_ai: int,
                           n_autonomous: int = 0, offset: int = 0):
    """Write epoch results to phi_history table."""
    if not db or not results:
        return
    async with db.acquire() as conn:
        for i, r in enumerate(results):
            rs = _safe(r)
            await conn.execute("""
                INSERT INTO phi_history
                  (sim_id, org_id, epoch_num, phi, phi_star, mean_hav, alignment_gap,
                   template_drift, macro_fitness, track2_nudge, n_human, n_ai, n_autonomous, full_result)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::JSONB)
                ON CONFLICT DO NOTHING
            """, sim_id, org_id, offset + i,
                 rs.get("phi") or rs.get("ai_fraction"), phi_star,
                 rs.get("mean_hav"), rs.get("alignment_gap"),
                 rs.get("template_drift"), rs.get("macro_fitness"),
                 bool(rs.get("track2_nudge_active", False)),
                 n_human, n_ai, n_autonomous, json.dumps(rs))
        await conn.execute(
            "UPDATE sim_registry SET epochs_run=$1, last_run_at=NOW() WHERE id=$2",
            offset + len(results), sim_id)


# ── Lifespan: DB init + reload sims from registry ────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(CREATE_TABLES)
        # Reload registered sims into memory
        rows = await conn.fetch("SELECT id, org_id, label, params, epochs_run FROM sim_registry")
        for row in rows:
            try:
                params = dict(row["params"])
                entry  = _build_sim(params, seed=42)
                entry.update({"org_id": row["org_id"], "label": row["label"],
                               "epochs_run": row["epochs_run"],
                               "created_at": datetime.now(timezone.utc).isoformat()})
                _sims[row["id"]] = entry
            except Exception:
                pass  # skip corrupted entries
    yield
    await db.close()


app = FastAPI(title="Tessera Digital Twin", version="5.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Pydantic models ───────────────────────────────────────────────────────────
class InitRequest(BaseModel):
    org_id:    str
    label:     str   = "default"
    seed:      int   = 42
    org_type:  str   = "profit"
    n_human:   Optional[int]   = None
    n_ai:      Optional[int]   = None
    K:         Optional[int]   = None
    alignment_coupling_prob: Optional[float] = None
    alignment_weight:        Optional[float] = None
    tau_probe:               Optional[int]   = None
    theta_replace:           Optional[float] = None
    hcm_rate:                Optional[float] = None
    topology:  Optional[str]   = None
    role_mode: Optional[str]   = None
    lambda_mis: Optional[float] = None
    v_d:        Optional[float] = None
    c_comp:     Optional[float] = None
    c_gov:      Optional[float] = None
    measurement_regime: Optional[str]   = None
    theta_novel:        Optional[float] = None
    theta_replace_hav:  Optional[float] = None


class RunRequest(BaseModel):
    epochs: int = 5


class ScenarioRequest(BaseModel):
    label:              str
    seed:               Optional[int]   = None
    n_human:            Optional[int]   = None
    n_ai:               Optional[int]   = None
    n_autonomous:       Optional[int]   = None
    K:                  Optional[int]   = None
    alignment_coupling_prob: Optional[float] = None
    alignment_weight:        Optional[float] = None
    tau_probe:               Optional[int]   = None
    theta_replace:           Optional[float] = None
    hcm_rate:                Optional[float] = None
    topology:           Optional[str]   = None
    role_mode:          Optional[str]   = None
    lambda_mis:         Optional[float] = None
    measurement_regime: Optional[str]   = None
    epochs:             int = 10


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "twin", "version": "5.0.0",
            "paper_stack": "Papers 1-5 (HAVCPN)", "active_sims": len(_sims)}


# ── Calibrate from real org data ──────────────────────────────────────────────
@app.post("/orgs/{org_id}/calibrate", status_code=201)
async def calibrate_twin(org_id: str, seed: int = 42, epochs: int = 5):
    """
    Read real capital composition from people service + real HAV from performance.
    Create a calibrated digital twin seeded from live org data.

    Capital types:
      human      → n_human in simulation
      ai_agent   → n_ai in simulation
      autonomous → weighted by autonomy_level, added to effective n_ai
    """
    calibration_source = {}

    async with httpx.AsyncClient(timeout=10.0) as client:
        # 1. Capital composition from people service
        try:
            pr = await client.get(f"{PEOPLE_URL}/composition")
            if pr.status_code == 200:
                comp = pr.json()
                n_human = comp.get("n_human", 10)
                n_ai    = comp.get("n_ai_agent", 5)
                n_auto  = comp.get("n_autonomous", 0)
                calibration_source["people"] = comp
            else:
                n_human, n_ai, n_auto = 10, 5, 0
        except Exception:
            n_human, n_ai, n_auto = 10, 5, 0

        # autonomous units weighted at 0.8 autonomy by default → add to effective n_ai
        effective_n_ai = n_ai + round(n_auto * 0.8)

        # 2. Real mean HAV — prefer time-attendance (measured) over performance (seeded)
        real_mean_hav = None
        try:
            ta_url = f"{TIME_ATTEND_URL}/org-hav-summary?last_days=90&org_id={org_id}"
            ta = await client.get(ta_url)
            if ta.status_code == 200:
                ta_data = ta.json()
                if ta_data.get("mean_hav") is not None:
                    real_mean_hav = ta_data["mean_hav"]
                    calibration_source["time_attendance"] = {
                        "mean_hav": real_mean_hav,
                        "n_sessions": ta_data.get("n_sessions"),
                        "source": "measured",
                    }
        except Exception:
            pass

        if real_mean_hav is None:
            try:
                pr2 = await client.get(f"{PERFORMANCE_URL}/reviews")
                if pr2.status_code == 200:
                    revs = pr2.json().get("reviews", [])
                    havs = [r["mean_hav"] for r in revs if r.get("mean_hav") is not None]
                    if havs:
                        real_mean_hav = round(sum(havs) / len(havs), 4)
                        calibration_source["performance"] = {
                            "mean_hav": real_mean_hav,
                            "n_reviews": len(havs),
                            "source": "seeded",
                        }
            except Exception:
                pass

    # Build params from real data
    params = {
        **BASELINE,
        "n_human": max(1, n_human),
        "n_ai":    max(1, effective_n_ai),
    }

    # Close any existing sim for this org
    existing = [sid for sid, e in _sims.items() if e["org_id"] == org_id and e.get("label") == "calibrated"]
    for sid in existing:
        _sims.pop(sid, None)

    sim_id = str(uuid.uuid4())
    entry  = _build_sim(params, seed)
    entry.update({"org_id": org_id, "label": "calibrated",
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "n_autonomous": n_auto})
    _sims[sim_id] = entry

    # Run baseline epochs
    net = entry["net"]
    net.run(max_time=epochs * params.get("tau_epoch", 50))
    entry["epochs_run"] = len(net.results)

    # Persist to DB
    if db:
        async with db.acquire() as conn:
            await conn.execute("""
                INSERT INTO sim_registry (id, org_id, label, params, epochs_run, last_calibrated_at, calibration_source)
                VALUES ($1,$2,$3,$4::JSONB,$5,NOW(),$6::JSONB)
                ON CONFLICT (id) DO UPDATE SET
                    epochs_run=$5, last_calibrated_at=NOW(), calibration_source=$6::JSONB
            """, sim_id, org_id, "calibrated",
                 json.dumps(params), entry["epochs_run"], json.dumps(calibration_source))

        await _persist_epochs(sim_id, org_id, net.results, net._phi_star,
                              n_human, effective_n_ai, n_auto)

    phi = net._phi_history[-1] if net._phi_history else (effective_n_ai / max(1, n_human + effective_n_ai))
    return {
        "sim_id":   sim_id,
        "org_id":   org_id,
        "label":    "calibrated",
        "calibrated_from": {
            "n_human":     n_human,
            "n_ai_agent":  n_ai,
            "n_autonomous": n_auto,
            "effective_n_ai": effective_n_ai,
            "real_mean_hav": real_mean_hav,
        },
        "phi":          round(phi, 4),
        "phi_star":     round(net._phi_star, 4),
        "crossover":    phi > net._phi_star,
        "epochs_run":   entry["epochs_run"],
        "calibration_source": calibration_source,
        "next": f"POST /sim/{sim_id}/run to project forward · POST /sim/{sim_id}/scenario to model capital changes",
    }


# ── Get current sim for an org ────────────────────────────────────────────────
@app.get("/orgs/{org_id}/sim")
def org_sim(org_id: str):
    """Return the active calibrated sim for an org."""
    matches = [(sid, e) for sid, e in _sims.items() if e["org_id"] == org_id]
    if not matches:
        raise HTTPException(404, f"No simulation found for org '{org_id}'. POST /orgs/{org_id}/calibrate first.")
    sid, e = sorted(matches, key=lambda x: x[1].get("created_at", ""), reverse=True)[0]
    net = e["net"]
    phi = net._phi_history[-1] if net._phi_history else 0.0
    return {
        "sim_id":    sid,
        "org_id":    org_id,
        "label":     e["label"],
        "epochs_run": e["epochs_run"],
        "phi":       round(phi, 4),
        "phi_star":  round(net._phi_star, 4),
        "crossover": phi > net._phi_star,
        "measurement_regime": net.regime.value,
        "n_human":   e["pool"].n_human,
        "n_ai":      e["pool"].n_ai,
        "n_autonomous": e.get("n_autonomous", 0),
    }


# ── Persistent φ history (survives restarts) ──────────────────────────────────
@app.get("/orgs/{org_id}/phi-history")
async def org_phi_history(org_id: str, last_n: int = 50):
    """φ time series from PostgreSQL — survives service restarts."""
    if not db:
        raise HTTPException(503, "Database not available")
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT epoch_num, recorded_at, phi, phi_star, mean_hav, alignment_gap,
                   template_drift, macro_fitness, track2_nudge, n_human, n_ai, n_autonomous
            FROM phi_history WHERE org_id=$1
            ORDER BY recorded_at DESC LIMIT $2
        """, org_id, last_n)
    history = [dict(r) for r in rows]
    for h in history:
        h["recorded_at"] = h["recorded_at"].isoformat() if h["recorded_at"] else None
    return {
        "org_id":  org_id,
        "records": len(history),
        "history": list(reversed(history)),
    }


# ── Init ──────────────────────────────────────────────────────────────────────
@app.post("/sim/init", status_code=201)
async def init_sim(body: InitRequest):
    params = {**BASELINE}
    for f in ("n_human", "n_ai", "K", "alignment_coupling_prob",
              "alignment_weight", "tau_probe", "theta_replace", "hcm_rate",
              "topology", "role_mode", "lambda_mis", "v_d", "c_comp", "c_gov",
              "measurement_regime", "theta_novel", "theta_replace_hav"):
        val = getattr(body, f)
        if val is not None:
            params[f] = val
    if body.org_type:
        params["org_type"] = body.org_type

    sim_id = str(uuid.uuid4())
    entry  = _build_sim(params, body.seed)
    entry.update({"org_id": body.org_id, "label": body.label,
                  "created_at": datetime.now(timezone.utc).isoformat()})
    _sims[sim_id] = entry

    if db:
        async with db.acquire() as conn:
            await conn.execute("""
                INSERT INTO sim_registry (id, org_id, label, params, epochs_run)
                VALUES ($1,$2,$3,$4::JSONB,$5)
            """, sim_id, body.org_id, body.label, json.dumps(params), 0)

    return {"sim_id": sim_id, "org_id": body.org_id, "label": body.label,
            "params": params, "seed": body.seed,
            "phi_star": entry["net"]._phi_star,
            "paper_stack": "HAVCPN (Papers 1-5)",
            "measurement_regime": params["measurement_regime"]}


# ── Run ───────────────────────────────────────────────────────────────────────
@app.post("/sim/{sim_id}/run")
async def run_sim(sim_id: str, body: RunRequest):
    entry = _sims.get(sim_id)
    if not entry:
        raise HTTPException(404, "Simulation not found")

    net       = entry["net"]
    tau_epoch = entry["params"].get("tau_epoch", 50)
    prev      = len(net.results)
    net.run(max_time=net.time + body.epochs * tau_epoch)

    new_results = [_safe(r) for r in net.results[prev:]]
    entry["epochs_run"] += len(new_results)

    # Persist new epochs
    await _persist_epochs(sim_id, entry["org_id"], new_results,
                          net._phi_star, entry["pool"].n_human, entry["pool"].n_ai,
                          entry.get("n_autonomous", 0), offset=prev)

    return {"sim_id": sim_id, "epochs_run": entry["epochs_run"],
            "new_epochs": len(new_results), "results": new_results}


# ── State ─────────────────────────────────────────────────────────────────────
@app.get("/sim/{sim_id}/state")
def sim_state(sim_id: str):
    entry = _sims.get(sim_id)
    if not entry:
        raise HTTPException(404, "Simulation not found")
    net   = entry["net"]
    pool  = entry["pool"]
    macro = entry["macro"]
    phi   = pool.n_ai / max(1, pool.n_human + pool.n_ai)
    latest = _safe(net.results[-1]) if net.results else {}
    return {
        "sim_id": sim_id, "org_id": entry["org_id"], "label": entry["label"],
        "sim_time": net.time, "epochs_run": entry["epochs_run"],
        "n_human": pool.n_human, "n_ai": pool.n_ai,
        "n_autonomous": entry.get("n_autonomous", 0),
        "phi": round(phi, 4), "phi_star": round(net._phi_star, 4),
        "crossover": phi > net._phi_star,
        "measurement_regime": net.regime.value,
        "topology": net.topology.value,
        "role_mode": net.role_mode.value,
        "macro_fitness": macro.current_fitness,
        "token_balance": net.token_reservoir.balance,
        "latest_epoch": latest,
        "vnet": _safe(net.vnet_summary()),
    }


# ── Metrics history ───────────────────────────────────────────────────────────
@app.get("/sim/{sim_id}/metrics")
def sim_metrics(sim_id: str, last_n: int = 0):
    entry = _sims.get(sim_id)
    if not entry:
        raise HTTPException(404, "Simulation not found")
    results = [_safe(r) for r in entry["net"].results]
    if last_n > 0:
        results = results[-last_n:]
    return {"sim_id": sim_id, "epochs": len(results), "results": results}


# ── Early warning ─────────────────────────────────────────────────────────────
@app.get("/sim/{sim_id}/early-warning")
def early_warning(sim_id: str):
    entry = _sims.get(sim_id)
    if not entry:
        raise HTTPException(404, "Simulation not found")
    net     = entry["net"]
    results = net.results
    if not results:
        return {"sim_id": sim_id, "stage": 0, "ok": True, "alerts": [], "evidence": {}}

    latest    = results[-1]
    last4     = results[-4:] if len(results) >= 4 else results
    gap       = latest.get("alignment_gap", 0.0)
    drift     = latest.get("template_drift", 0.0)
    s2        = bool(latest.get("stage2_active", 0))
    fitness   = latest.get("macro_fitness", 1.0)
    ai_frac   = latest.get("ai_fraction", 0.0)
    gap_streak = sum(1 for r in last4 if r.get("alignment_gap", 0) > 0.03)
    phi       = latest.get("phi", ai_frac)
    phi_star  = net._phi_star
    mean_hav  = latest.get("mean_hav", None)
    nudge     = latest.get("track2_nudge_active", False)
    ithr      = latest.get("ithr_ratio")
    n_auto    = entry.get("n_autonomous", 0)

    alerts = []
    stage  = 0

    if gap_streak >= 2 and gap > 0.03:
        stage = 1
        alerts.append(f"Alignment gap {gap:.3f} persisted {gap_streak}/{len(last4)} epochs.")
    if ai_frac > 0.6:
        stage = max(stage, 1)
        alerts.append(f"AI fraction {ai_frac:.0%} — T_replace_h2a may accelerate pathology.")
    if s2 or drift > 0.3:
        stage = 2
        alerts.append(f"Stage 2 active. Template drift {drift:.3f}. Suspend replacements, inject diverse probes.")
    if fitness < 0.4 and s2:
        alerts.append(f"Macro fitness {fitness:.3f} — decision quality visibly degraded. Escalate.")
    if ithr is not None and ithr > 3.0:
        stage = max(stage, 1)
        alerts.append(f"IT:HR ratio {ithr:.2f} >3.0 — LLM self-defeat risk. Deploy SEQ_GATEWAY.")
    if phi > phi_star:
        stage = max(stage, 1)
        alerts.append(
            f"φ={phi:.2f} exceeds φ*={phi_star:.2f}. HAV crossover: man-hours governance accelerates Track 2. "
            f"{'Switch to HAV regime immediately.' if nudge else 'HAV regime active — aligned.'}"
        )
    if nudge:
        stage = max(stage, 1)
        alerts.append("Track 2 nudge active: MAN_HOURS regime shifting beliefs toward AI centroid.")
    if mean_hav is not None and mean_hav < 0.20:
        stage = max(stage, 1)
        alerts.append(f"Mean HAV={mean_hav:.3f} critically low — humans mostly in procedural mode.")
    if n_auto > 0 and mean_hav is not None and mean_hav < 0.35:
        stage = max(stage, 1)
        alerts.append(
            f"{n_auto} autonomous unit(s) deployed + mean HAV={mean_hav:.3f} declining. "
            "Physical capital may be absorbing novel-problem work from humans. Rotate roles."
        )

    return {
        "sim_id": sim_id, "stage": stage, "ok": stage == 0, "alerts": alerts,
        "evidence": {
            "alignment_gap": round(gap, 4), "template_drift": round(drift, 4),
            "stage2_active": s2, "macro_fitness": round(fitness, 4),
            "ai_fraction": round(ai_frac, 4), "gap_streak": gap_streak,
            "phi": round(phi, 4), "phi_star": round(phi_star, 4),
            "crossover": phi > phi_star, "mean_hav": round(mean_hav, 4) if mean_hav else None,
            "track2_nudge": nudge, "ithr_ratio": round(ithr, 4) if ithr else None,
            "topology": net.topology.value, "measurement_regime": net.regime.value,
            "n_autonomous": n_auto,
        },
    }


# ── HAV Dashboard ─────────────────────────────────────────────────────────────
@app.get("/sim/{sim_id}/hav")
def hav_dashboard(sim_id: str):
    entry = _sims.get(sim_id)
    if not entry:
        raise HTTPException(404, "Simulation not found")
    return {"sim_id": sim_id, **_safe(entry["net"].hav_dashboard())}


# ── V_net / IT:HR ─────────────────────────────────────────────────────────────
@app.get("/sim/{sim_id}/vnet")
def vnet_history(sim_id: str, last_n: int = 0):
    entry = _sims.get(sim_id)
    if not entry:
        raise HTTPException(404, "Simulation not found")
    net  = entry["net"]
    recs = net._vnet_records
    if last_n > 0:
        recs = recs[-last_n:]
    return {
        "sim_id": sim_id,
        "summary": _safe(net.vnet_summary()),
        "history": [
            {"epoch": r.epoch, "fitness_t": round(r.fitness_t, 4),
             "n_ai": r.n_ai, "n_human": r.n_human,
             "value_epoch": round(r.value_epoch, 2), "cost_epoch": round(r.cost_epoch, 2),
             "vnet_epoch": round(r.vnet_epoch, 2), "vnet_cumulative": round(r.vnet_cumulative, 2),
             "ithr_ratio": round(r.ithr_ratio, 4) if r.ithr_ratio != float('inf') else None,
             "gateway_fired": r.gateway_fired, "gateway_caught": r.gateway_caught}
            for r in recs
        ],
    }


# ── φ status ──────────────────────────────────────────────────────────────────
@app.get("/sim/{sim_id}/phi")
def phi_status(sim_id: str):
    entry = _sims.get(sim_id)
    if not entry:
        raise HTTPException(404, "Simulation not found")
    net  = entry["net"]
    pool = entry["pool"]
    phi  = pool.n_ai / max(1, pool.n_human + pool.n_ai)
    K    = entry["params"].get("K", 4)
    phi_series = [r.get("phi", r.get("ai_fraction", 0.0)) for r in net.results]
    return {
        "sim_id": sim_id,
        "phi_current": round(phi, 4), "phi_star": round(net._phi_star, 4),
        "K": K, "crossover": phi > net._phi_star,
        "measurement_regime": net.regime.value,
        "track2_nudge_active": net._track2_nudge_active,
        "org_type": net.org_type, "n_autonomous": entry.get("n_autonomous", 0),
        "phi_series": [round(p, 4) for p in phi_series],
        "phi_star_thresholds": {"K_ge_6": 0.25, "K_4": 0.32, "K_le_2": 0.44},
        "crossover_advice": (
            "HAV crossover reached. Switch measurement_regime to HAV."
            if phi > net._phi_star
            else f"φ={phi:.2f} below φ*={net._phi_star:.2f}. Both regimes equivalent."
        ),
    }


# ── What-if scenario ──────────────────────────────────────────────────────────
@app.post("/sim/{sim_id}/scenario")
def run_scenario(sim_id: str, body: ScenarioRequest):
    """
    Fork the simulation with modified capital composition, run N epochs.
    Use n_autonomous to model physical robot deployment impact.
    Does NOT modify the original simulation.
    """
    entry = _sims.get(sim_id)
    if not entry:
        raise HTTPException(404, "Simulation not found")

    fork_params = {**entry["params"]}
    for f in ("n_human", "K", "alignment_coupling_prob", "alignment_weight",
              "tau_probe", "theta_replace", "hcm_rate", "topology",
              "role_mode", "lambda_mis", "measurement_regime"):
        val = getattr(body, f, None)
        if val is not None:
            fork_params[f] = val

    # Autonomous units: weighted at 0.8 autonomy → added to effective n_ai
    base_n_ai  = getattr(body, "n_ai", None) or entry["params"].get("n_ai", 10)
    n_auto     = getattr(body, "n_autonomous", None) or entry.get("n_autonomous", 0)
    fork_params["n_ai"] = base_n_ai + round(n_auto * 0.8)

    fork_seed = body.seed if body.seed is not None else entry["seed"] + 1000
    fork      = _build_sim(fork_params, fork_seed)
    tau       = fork_params.get("tau_epoch", 50)
    fork["net"].run(max_time=body.epochs * tau)
    fork_results  = [_safe(r) for r in fork["net"].results]
    base_results  = [_safe(r) for r in entry["net"].results[-body.epochs:]]

    def _summary(rows):
        if not rows:
            return {}
        ms = ["macro_fitness", "alignment_gap", "template_drift", "stage2_active",
              "ai_fraction", "vnet_cumulative", "ithr_ratio", "mean_hav",
              "phi", "track2_nudge_active"]
        out = {}
        for m in ms:
            vals = [r[m] for r in rows if m in r and r[m] is not None]
            if vals:
                out[f"{m}_mean"]  = round(float(np.mean(vals)), 4)
                out[f"{m}_final"] = round(float(vals[-1]), 4)
        return out

    param_keys = ("n_human", "n_ai", "K", "topology", "role_mode", "lambda_mis", "measurement_regime")
    return {
        "sim_id": sim_id, "scenario": body.label, "epochs": body.epochs,
        "n_autonomous_added": n_auto,
        "baseline":         {"params": {k: entry["params"].get(k) for k in param_keys}, "summary": _summary(base_results)},
        "scenario_result":  {"params": {k: fork_params.get(k) for k in param_keys}, "summary": _summary(fork_results), "results": fork_results},
    }


# ── Org memory ────────────────────────────────────────────────────────────────
@app.get("/sim/{sim_id}/memory")
def sim_memory(sim_id: str, top_n: int = 20):
    entry = _sims.get(sim_id)
    if not entry:
        raise HTTPException(404, "Simulation not found")
    entries = sorted(entry["org_memory"].entries, key=lambda e: -e.fitness_outcome)[:top_n]
    return {
        "sim_id": sim_id, "total_entries": len(entry["org_memory"].entries),
        "top_entries": [
            {"id": e.id, "mode": e.decision_mode.name, "fitness": round(e.fitness_outcome, 4),
             "framing": [p.name for p in e.opportunity_framing],
             "agent_mix": {"humans": e.agent_mix[0], "ais": e.agent_mix[1]},
             "timestamp": e.timestamp}
            for e in entries
        ],
    }


# ── List / Delete ─────────────────────────────────────────────────────────────
@app.get("/sim")
def list_sims():
    return {
        "active_sims": [
            {"sim_id": sid, "org_id": e["org_id"], "label": e["label"],
             "epochs_run": e["epochs_run"], "created_at": e["created_at"],
             "n_human": e["pool"].n_human, "n_ai": e["pool"].n_ai,
             "n_autonomous": e.get("n_autonomous", 0),
             "topology": e["net"].topology.value,
             "measurement_regime": e["net"].regime.value}
            for sid, e in _sims.items()
        ]
    }


@app.get("/orgs/{org_id}/role-predictions")
def role_predictions(org_id: str):
    """
    Predict emerging and at-risk roles based on current φ trajectory.
    Grounded in HAVCPN φ* crossover theorem (Paper 5):
      - Sub-crossover:  build AI capability
      - Approaching:    prepare φ-guardian and HAV infrastructure
      - Near crossover: lock in Values Custodians and OC capacity
      - Above crossover: HAV regime — VCs and autonomous governance are critical
    """
    matches = [(sid, e) for sid, e in _sims.items() if e["org_id"] == org_id]
    if not matches:
        raise HTTPException(404, f"No simulation found for org '{org_id}'")
    sim_id, entry = sorted(matches, key=lambda x: x[1].get("created_at", ""), reverse=True)[0]

    net  = entry["net"]
    pool = entry["pool"]
    phi  = pool.n_ai / max(1, pool.n_human + pool.n_ai)
    phi_star = net._phi_star
    K    = entry["params"].get("K", 4)

    last = _safe(net.results[-1]) if net.results else {}
    mean_hav      = float(last.get("mean_hav") or 0.5)
    alignment_gap = float(last.get("alignment_gap") or 0.0)
    drift         = float(last.get("template_drift") or 0.0)

    ratio = phi / max(0.001, phi_star)   # how close to crossover

    if ratio < 0.5:
        emerging = [
            {"title": "AI/ML Engineer",               "urgency": "medium",
             "reason": f"phi={phi:.3f} — org needs AI capability to reach phi*={phi_star:.3f}",
             "target_hav_min": 0.55, "npf_required": 0.60},
            {"title": "Data Infrastructure Engineer", "urgency": "medium",
             "reason": "Foundation for AI deployment at scale",
             "target_hav_min": 0.50, "npf_required": 0.55},
        ]
        at_risk = [
            {"title": "Manual Data Entry", "risk": "high",
             "reason": "High automation probability at current AI fraction", "hav_threshold": 0.25},
        ]
    elif ratio < 0.85:
        emerging = [
            {"title": "phi-Guardian Engineer",       "urgency": "high",
             "reason": f"phi={phi:.3f} approaching phi*={phi_star:.3f}. SLA guardian coverage needed before crossover.",
             "target_hav_min": 0.65, "npf_required": 0.70},
            {"title": "HAV Coach / People Scientist","urgency": "medium",
             "reason": "Org needs HAV measurement capability before crossover forces the switch",
             "target_hav_min": 0.60, "npf_required": 0.65},
            {"title": "Human-AI Liaison",            "urgency": "medium",
             "reason": "Bridge between AI agent outputs and human governance at crossover",
             "target_hav_min": 0.55, "npf_required": 0.60},
        ]
        at_risk = [
            {"title": "Routine Analyst",          "risk": "high",
             "reason": "NPF < 0.30 roles will be substituted as AI fraction rises", "hav_threshold": 0.30},
            {"title": "Standard Ticket Handler",  "risk": "high",
             "reason": "SRQ value only — at risk once AI SLO breach rates drop", "hav_threshold": 0.25},
        ]
    elif ratio <= 1.0:
        emerging = [
            {"title": "phi-Guardian (SRQ Specialist)",    "urgency": "critical",
             "reason": f"phi={phi:.3f} approx phi*={phi_star:.3f}. HAV crossover imminent. SRQ guardians required.",
             "target_hav_min": 0.70, "npf_required": 0.75},
            {"title": "Values Custodian (OC Specialist)", "urgency": "critical",
             "reason": "Org memory and novel framing capacity must be locked in before crossover erodes it",
             "target_hav_min": 0.75, "npf_required": 0.80},
            {"title": "Origination Capacity Scout",       "urgency": "high",
             "reason": "Novel problem surface expands at crossover — need humans who see what AI cannot",
             "target_hav_min": 0.65, "npf_required": 0.70},
        ]
        at_risk = [
            {"title": "Procedure-Bound Analyst", "risk": "critical",
             "reason": f"alignment_gap={alignment_gap:.3f} — MAN_HOURS regime at this phi creates substitution pressure",
             "hav_threshold": 0.30},
        ]
    else:
        emerging = [
            {"title": "Values Custodian",               "urgency": "critical",
             "reason": f"phi={phi:.3f} > phi*={phi_star:.3f}. HAV regime active. Values Custodians are the irreplaceable capital.",
             "target_hav_min": 0.75, "npf_required": 0.80},
            {"title": "Autonomous Unit Operator",        "urgency": "high",
             "reason": "Physical autonomous capital requires human governance — new role category",
             "target_hav_min": 0.60, "npf_required": 0.65},
            {"title": "Alignment Auditor",              "urgency": "high",
             "reason": f"template_drift={drift:.3f} — belief drift above crossover requires human audit capability",
             "target_hav_min": 0.65, "npf_required": 0.70},
            {"title": "Origination Capacity Specialist","urgency": "medium",
             "reason": "Novel framing is now the scarce resource. Hire for OC > 0.70.",
             "target_hav_min": 0.70, "npf_required": 0.75},
        ]
        at_risk = [
            {"title": "All roles with NPF < 0.30", "risk": "critical",
             "reason": f"Above phi*. MAN_HOURS regime accelerates elimination of low-NPF roles.",
             "hav_threshold": 0.30},
            {"title": "Standard IT Support",       "risk": "high",
             "reason": "ITSM resolution now AI-dominant above crossover", "hav_threshold": 0.25},
        ]

    trajectory = "above_crossover" if phi > phi_star else ("approaching" if ratio > 0.75 else "sub_crossover")
    return {
        "org_id":          org_id,
        "sim_id":          sim_id,
        "phi":             round(phi, 4),
        "phi_star":        round(phi_star, 4),
        "K":               K,
        "crossover":       phi > phi_star,
        "mean_hav":        round(mean_hav, 4),
        "trajectory":      trajectory,
        "emerging_roles":  emerging,
        "at_risk_roles":   at_risk,
        "prediction_basis": f"HAVCPN phi*={phi_star:.3f} (K={K}) · alignment_gap={alignment_gap:.3f} · template_drift={drift:.3f}",
    }


@app.delete("/sim/{sim_id}")
async def delete_sim(sim_id: str):
    if sim_id not in _sims:
        raise HTTPException(404, "Simulation not found")
    _sims.pop(sim_id)
    if db:
        async with db.acquire() as conn:
            await conn.execute("DELETE FROM sim_registry WHERE id=$1", sim_id)
    return {"deleted": sim_id}
