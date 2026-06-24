"""
HACM API Gateway
================
Single entry point for the frontend.
Routes every /api/v1/{service}/... request to the right microservice.

Frontend calls:  GET/POST http://localhost:8000/api/v1/cost-benefit/analyze
Gateway routes:  → http://cost-benefit:8001/analyze

This means the frontend never needs to know about individual services.
Add a new service? Add a route here. Frontend doesn't change.
"""

import os
import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hacm.gateway")

app = FastAPI(
    title="HACM API Gateway",
    description="Routes all frontend requests to the correct HACM microservice",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── SERVICE REGISTRY ──────────────────────────────────────────────────
# To add a new service: add one line here + a new docker-compose service.
# Nothing else changes.

SERVICES = {
    "cost-benefit": os.getenv("COST_BENEFIT_URL", "http://localhost:8001"),
    "payroll":       os.getenv("PAYROLL_URL",       "http://localhost:8002"),
    "learning":      os.getenv("LEARNING_URL",       "http://localhost:8003"),
    "twin":          os.getenv("TWIN_URL",            "http://localhost:8004"),
    "people":        os.getenv("PEOPLE_URL",          "http://localhost:8005"),
    "expenses":      os.getenv("EXPENSES_URL",        "http://localhost:8006"),
    "onboarding":    os.getenv("ONBOARDING_URL",      "http://localhost:8007"),
    "governance":    os.getenv("GOVERNANCE_URL",      "http://localhost:8008"),
    "intake":         os.getenv("INTAKE_URL",          "http://localhost:8009"),
    "trace":          os.getenv("TRACE_URL",           "http://localhost:8010"),
    "agent-factory":  os.getenv("AGENT_FACTORY_URL",   "http://localhost:8011"),
    "identity":       os.getenv("IDENTITY_URL",         "http://localhost:8012"),
    "itsm":           os.getenv("ITSM_URL",             "http://localhost:8013"),
    "workos":         os.getenv("WORKOS_URL",           "http://localhost:8014"),
    "notifications":   os.getenv("NOTIFICATIONS_URL",    "http://localhost:8015"),
    "audit":           os.getenv("AUDIT_URL",             "http://localhost:8016"),
    "time-attendance":   os.getenv("TIME_ATTENDANCE_URL",     "http://localhost:8017"),
    "benefits":          os.getenv("BENEFITS_URL",           "http://localhost:8018"),
    "recruiting":        os.getenv("RECRUITING_URL",         "http://localhost:8019"),
    "performance":       os.getenv("PERFORMANCE_URL",        "http://localhost:8020"),
    "succession":        os.getenv("SUCCESSION_URL",         "http://localhost:8021"),
    "compensation":      os.getenv("COMPENSATION_URL",       "http://localhost:8022"),
    "absence":           os.getenv("ABSENCE_URL",            "http://localhost:8023"),
    "workforce-planning": os.getenv("WORKFORCE_PLANNING_URL","http://localhost:8024"),
    "deployment":         os.getenv("DEPLOYMENT_URL",          "http://localhost:8028"),
    "finance":           os.getenv("FINANCE_URL",            "http://localhost:8025"),
    "knowledge":         os.getenv("KNOWLEDGE_URL",          "http://localhost:8026"),
    "service-catalog":   os.getenv("SERVICE_CATALOG_URL",    "http://localhost:8027"),
}


@app.get("/")
def root():
    return {
        "service": "HACM API Gateway",
        "version": "1.0.0",
        "routes": {
            f"/api/v1/{name}/...": url
            for name, url in SERVICES.items()
        },
        "health": "/health",
        "docs": "/docs",
    }


@app.get("/health")
async def health():
    """Check all services are up."""
    results = {}
    async with httpx.AsyncClient(timeout=3.0) as client:
        for name, url in SERVICES.items():
            try:
                r = await client.get(f"{url}/health")
                results[name] = "up" if r.status_code == 200 else "degraded"
            except Exception:
                results[name] = "down"
    overall = "healthy" if all(v == "up" for v in results.values()) else "degraded"
    return {"status": overall, "services": results}


@app.get("/demo/seed")
async def seed_demo(org_id: str = "demo-org"):
    """
    Seed demo data across all services. GET endpoint so it works from any browser/tool.
    Creates employees, HAV reviews, compensation records, recruiting pipeline, and retention alerts.
    """
    import json as _json
    results = {}
    DEMO = [
        {"id":"emp-001","name":"Maya Chen",    "dept":"Engineering","title":"VP Engineering",   "salary":180000,"npf":0.89,"srq":0.85,"oc":0.82,"trend":"improving"},
        {"id":"emp-002","name":"James Okonkwo","dept":"Engineering","title":"Senior Engineer",  "salary":145000,"npf":0.78,"srq":0.72,"oc":0.75,"trend":"stable"},
        {"id":"emp-003","name":"Sofia Reyes",  "dept":"Product",    "title":"Product Manager",  "salary":130000,"npf":0.71,"srq":0.68,"oc":0.65,"trend":"improving"},
        {"id":"emp-004","name":"Alex Mercer",  "dept":"Engineering","title":"Software Engineer","salary":115000,"npf":0.62,"srq":0.58,"oc":0.61,"trend":"stable"},
        {"id":"emp-005","name":"Jordan Park",  "dept":"Design",     "title":"Product Designer", "salary":105000,"npf":0.55,"srq":0.52,"oc":0.54,"trend":"stable"},
        {"id":"emp-006","name":"Sam Williams", "dept":"Platform",   "title":"DevOps Engineer",  "salary":108000,"npf":0.48,"srq":0.45,"oc":0.46,"trend":"declining"},
        {"id":"emp-007","name":"Priya Sharma", "dept":"Data",       "title":"Data Scientist",   "salary":135000,"npf":0.76,"srq":0.73,"oc":0.71,"trend":"improving"},
        {"id":"emp-008","name":"Marcus Lee",   "dept":"Engineering","title":"Frontend Engineer","salary":95000, "npf":0.38,"srq":0.35,"oc":0.40,"trend":"stable"},
        {"id":"emp-009","name":"Emma Wilson",  "dept":"People",     "title":"HR Manager",       "salary":110000,"npf":0.68,"srq":0.64,"oc":0.66,"trend":"stable"},
        {"id":"emp-010","name":"David Kim",    "dept":"Sales",      "title":"Sales Lead",       "salary":120000,"npf":0.31,"srq":0.28,"oc":0.35,"trend":"declining"},
    ]
    for e in DEMO:
        e["hav"] = round(0.50*e["npf"] + 0.30*e["srq"] + 0.20*e["oc"], 4)

    phi = sum(e["hav"] for e in DEMO) / len(DEMO)
    r_ap = 0.05 if phi < 0.25 else (0.25 if phi > 0.75 else round(0.05 + (phi-0.25)*0.40, 4))
    perf_url  = SERVICES["performance"]
    comp_url  = SERVICES["compensation"]
    rec_url   = SERVICES["recruiting"]
    ben_url   = SERVICES["benefits"]

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Performance cycle
        r = await client.post(f"{perf_url}/cycles", json={
            "org_id": org_id, "name": "FY2026 Q1", "cycle_type": "quarterly",
            "start_date": "2026-01-01", "end_date": "2026-03-31"
        })
        if r.status_code != 201:
            return {"error": "performance cycle failed", "detail": r.text}
        cycle_id = r.json()["cycle_id"]
        results["cycle_id"] = cycle_id

        # 2. Performance reviews
        review_ids = []
        for e in DEMO:
            rv = await client.post(f"{perf_url}/reviews", json={
                "cycle_id": cycle_id, "employee_id": e["id"],
                "mean_hav": e["hav"], "mean_npf": e["npf"],
                "mean_srq": e["srq"], "mean_oc":  e["oc"],
                "hav_trend": e["trend"], "npf_trend": e["trend"],
                "above_crossover_pct": 0.85 if e["hav"] > 0.25 else 0.40,
                "phi_guardian_sessions": int(e["npf"] * 12) if e["hav"] >= 0.65 else 0,
                "narrative": f"Continuous HAV tracking for {e['name']}."
            })
            if rv.status_code == 201:
                review_ids.append(rv.json().get("review_id"))
        results["reviews_created"] = len(review_ids)

        # 3. Merit cycle
        mc = await client.post(f"{comp_url}/merit-cycles", json={
            "org_id": org_id, "name": "FY2026 Annual Merit",
            "effective_date": "2026-04-01", "budget_pct": 0.055
        })
        merit_cycle_id = mc.json().get("cycle_id") if mc.status_code == 201 else None
        results["merit_cycle_id"] = merit_cycle_id

        # 4. Compensation records + merit recommendations
        comp_count = 0
        for e in DEMO:
            await client.post(f"{comp_url}/records", json={
                "employee_id": e["id"], "org_id": org_id, "effective_date": "2026-01-01",
                "base_salary": e["salary"], "token_budget": 12000 if e["hav"] >= 0.65 else 6000,
                "mean_hav": e["hav"], "phi": phi, "phi_star": 0.25, "reason": "Annual review"
            })
            if merit_cycle_id:
                await client.post(f"{comp_url}/merit-recommendations", json={
                    "cycle_id": merit_cycle_id, "employee_id": e["id"],
                    "current_salary": e["salary"], "mean_hav": e["hav"],
                    "hav_trend": e["trend"],
                    "token_budget": 12000 if e["hav"] >= 0.65 else 6000, "phi": phi
                })
            comp_count += 1
        results["comp_records"] = comp_count

        # 5. Recruiting requisitions
        req_ids = []
        for req in [
            {"org_id":org_id,"title":"Senior Platform Engineer","department":"Engineering","phi_role":"phi_guardian","target_hav_min":0.65,"target_npf_min":0.60,"headcount":2},
            {"org_id":org_id,"title":"ML Engineer",            "department":"Data",       "phi_role":"standard",    "target_hav_min":0.50,"target_npf_min":0.45,"headcount":1},
            {"org_id":org_id,"title":"Product Designer",       "department":"Design",     "phi_role":"standard",    "target_hav_min":0.45,"target_npf_min":0.40,"headcount":1},
        ]:
            rr = await client.post(f"{rec_url}/requisitions", json=req)
            if rr.status_code == 201:
                req_ids.append(rr.json()["requisition_id"])
        results["requisitions"] = len(req_ids)

        # 6. Candidates
        cand_count = 0
        candidates = [
            {"name":"Asha Patel",    "email":"asha@demo.com", "req_idx":0,"npf":0.74,"srq":0.69,"oc":0.71},
            {"name":"Chris Nakamura","email":"chris@demo.com","req_idx":0,"npf":0.61,"srq":0.57,"oc":0.58},
            {"name":"Lena Braun",    "email":"lena@demo.com", "req_idx":1,"npf":0.79,"srq":0.74,"oc":0.68},
            {"name":"Omar Hassan",   "email":"omar@demo.com", "req_idx":2,"npf":0.52,"srq":0.48,"oc":0.51},
        ]
        for c in candidates:
            if c["req_idx"] < len(req_ids):
                cr = await client.post(f"{rec_url}/candidates", json={
                    "requisition_id": req_ids[c["req_idx"]], "name": c["name"],
                    "email": c["email"], "source": "referral", "status": "applied"
                })
                if cr.status_code == 201:
                    cid = cr.json()["candidate_id"]
                    await client.post(f"{rec_url}/candidates/{cid}/score", json={
                        "npf_potential": c["npf"], "srq_potential": c["srq"],
                        "oc_potential": c["oc"], "ai_human_collab_score": 0.70
                    })
                    cand_count += 1
        results["candidates"] = cand_count

        # 7. Retention alerts for Values Custodians
        vc_count = 0
        for e in DEMO:
            if e["hav"] >= 0.70 and e["npf"] >= 0.65:
                await client.post(f"{ben_url}/retention-alert", json={
                    "employee_id": e["id"], "org_id": org_id,
                    "mean_hav": e["hav"], "mean_npf": e["npf"],
                    "trigger": "values_custodian_threshold",
                    "severity": "critical" if e["hav"] >= 0.80 else "high"
                })
                vc_count += 1
        results["vc_alerts"] = vc_count

        # 8. Register demo employees as human capital units in people service
        people_url  = SERVICES["people"]
        people_count = 0
        for e in DEMO:
            pr = await client.post(f"{people_url}/units", json={
                "employee_id": e["id"],
                "name": e["name"],
                "unit_type": "human",
                "department": e["dept"],
                "role_title": e["title"],
                "annual_salary": e["salary"],
                "monthly_token_allocation": 12000 if e["hav"] >= 0.65 else 6000,
            })
            if pr.status_code == 201:
                people_count += 1
        results["people_units"] = people_count

        # 9. Calibrate digital twin from real capital composition
        twin_url = SERVICES["twin"]
        tc = await client.post(f"{twin_url}/orgs/{org_id}/calibrate?epochs=5")
        if tc.status_code == 201:
            td = tc.json()
            results["twin_sim_id"]  = td.get("sim_id")
            results["twin_phi"]     = td.get("phi")
            results["twin_phi_star"] = td.get("phi_star")
            results["twin_crossover"] = td.get("crossover")
        else:
            results["twin_calibration"] = "skipped"

        # 10. Seed onboarding journeys (first 3 employees as recent new hires)
        onboarding_url = SERVICES["onboarding"]
        onboarding_count = 0
        for e in DEMO[:3]:
            oj = await client.post(f"{onboarding_url}/journeys", json={
                "unit_id": e["id"], "unit_name": e["name"],
                "unit_type": "human", "journey_type": "onboarding",
                "start_date": "2026-01-15", "manager_id": "emp-001",
                "role": e["title"], "department": e["dept"],
                "belief_alignment_at_entry": e["hav"],
            })
            if oj.status_code in (200, 201):
                onboarding_count += 1
        results["onboarding_journeys"] = onboarding_count

        # 11. Seed absence balances for all employees
        absence_url = SERVICES["absence"]
        absence_count = 0
        for e in DEMO:
            ab = await client.post(f"{absence_url}/balances", json={
                "employee_id": e["id"], "pto_days": 15.0,
                "sick_days": 10.0, "personal_days": 3.0,
            })
            if ab.status_code == 201:
                absence_count += 1
        results["absence_balances"] = absence_count

        # 12b. Seed ITSM incidents + CMDB entries
        ITSM_ORG = "00000000-0000-0000-0000-000000000001"
        itsm_url = SERVICES["itsm"]
        itsm_count = 0
        demo_incidents = [
            {"ticket_type":"incident","title":"AI agent latency spike — P2","description":"claude-agent-01 response time >8s on engineering queries. SLA breach risk.","priority":"P2","category":"AI Services","reporter_email":"maya.chen@demo.com","assignee_email":"priya.sharma@demo.com","team":"Platform"},
            {"ticket_type":"incident","title":"Payroll token budget miscalculation — P3","description":"Alignment premium not applied to 2 employees in June run. HAV data feed error.","priority":"P3","category":"Payroll","reporter_email":"emma.wilson@demo.com","assignee_email":"emma.wilson@demo.com","team":"People Ops"},
            {"ticket_type":"incident","title":"Onboarding journey stuck at step 3 — P3","description":"Sofia Reyes onboarding blocked on belief-alignment task. Manager review required.","priority":"P3","category":"Onboarding","reporter_email":"james.okonkwo@demo.com","assignee_email":"maya.chen@demo.com","team":"Engineering"},
            {"ticket_type":"request","title":"New AI agent provisioning — Data team","description":"Request to provision a new Haiku 4.5 agent for data pipeline automation. HAV governance review needed.","priority":"P3","category":"AI Provisioning","reporter_email":"priya.sharma@demo.com","team":"Data"},
            {"ticket_type":"change","title":"Upgrade twin calibration to 10 epochs","description":"CAB approval requested for twin recalibration parameter change from 5 to 10 epochs.","priority":"P4","category":"Platform Config","reporter_email":"maya.chen@demo.com","team":"Platform"},
        ]
        for inc in demo_incidents:
            ir = await client.post(f"{itsm_url}/tickets", json={"org_id": ITSM_ORG, **inc})
            if ir.status_code == 201:
                itsm_count += 1
        results["itsm_incidents"] = itsm_count

        # 12c. Seed ITSM CMDB — register AI agents as capital assets
        cmdb_items = [
            {"name":"claude-agent-01","ci_type":"service","status":"active","description":"Primary Haiku 4.5 agent for data pipeline automation","owner_email":"priya.sharma@demo.com","metadata":{"model":"claude-haiku-4-5","deployment":"cloud","monthly_cost_usd":480}},
            {"name":"claude-agent-02","ci_type":"service","status":"active","description":"Engineering query assistant (Sonnet 4.6) — SLA target <2s","owner_email":"maya.chen@demo.com","metadata":{"model":"claude-sonnet-4-6","deployment":"cloud","monthly_cost_usd":1200}},
            {"name":"tessera-gateway","ci_type":"service","status":"active","description":"Tessera API gateway — routes all microservice calls","owner_email":"maya.chen@demo.com","metadata":{"port":8000,"replicas":2}},
            {"name":"twin-compute-01","ci_type":"server","status":"active","description":"Primary compute node running digital twin CPN simulations","owner_email":"priya.sharma@demo.com","metadata":{"cpu_cores":8,"ram_gb":32}},
        ]
        cmdb_count = 0
        for ci in cmdb_items:
            cr = await client.post(f"{itsm_url}/cmdb", json={"org_id": ITSM_ORG, **ci})
            if cr.status_code == 201:
                cmdb_count += 1
        results["itsm_cmdb"] = cmdb_count

        # 12d. Seed Workforce Planning — plan + role decisions + φ scenarios
        wp_url = SERVICES["workforce-planning"]
        wp_seeded = 0
        twin_r = await client.get(f"{SERVICES['twin']}/orgs/demo-org/role-predictions")
        current_phi = twin_r.json().get("phi", 0.087) if twin_r.status_code == 200 else 0.087
        plan_r = await client.post(f"{wp_url}/plans", json={
            "org_id":"demo-org","name":"FY2026 H2 Headcount Plan",
            "period":"2026-H2","current_phi":current_phi,"org_k":4
        })
        if plan_r.status_code == 201:
            plan_id = plan_r.json().get("plan_id","")
            demo_roles = [
                {"role_title":"Senior Data Scientist","department":"Engineering","hav_required":0.68,"npf_required":0.60,"human_fitness":0.88,"human_value_delivery":145000,"human_comp_cost":120000,"human_gov_cost":6000,"ai_deployment_value":90000,"ai_deployment_cost":18000,"ai_oversight_cost":12000,"headcount":1},
                {"role_title":"AI Agent Oversight Lead","department":"Platform","hav_required":0.72,"npf_required":0.65,"human_fitness":0.91,"human_value_delivery":160000,"human_comp_cost":130000,"human_gov_cost":7000,"ai_deployment_value":0,"ai_deployment_cost":0,"ai_oversight_cost":0,"headcount":1},
                {"role_title":"Data Pipeline Automation","department":"Data","hav_required":0.10,"npf_required":0.05,"human_fitness":0.40,"human_value_delivery":90000,"human_comp_cost":85000,"human_gov_cost":4000,"ai_deployment_value":95000,"ai_deployment_cost":22000,"ai_oversight_cost":14000,"headcount":2},
                {"role_title":"Customer Support L1","department":"Operations","hav_required":0.15,"npf_required":0.10,"human_fitness":0.50,"human_value_delivery":70000,"human_comp_cost":60000,"human_gov_cost":3000,"ai_deployment_value":75000,"ai_deployment_cost":15000,"ai_oversight_cost":10000,"headcount":3},
                {"role_title":"Org Belief Analyst","department":"People Ops","hav_required":0.70,"npf_required":0.65,"human_fitness":0.85,"human_value_delivery":140000,"human_comp_cost":115000,"human_gov_cost":6500,"ai_deployment_value":50000,"ai_deployment_cost":10000,"ai_oversight_cost":8000,"headcount":1},
            ]
            for rd in demo_roles:
                rr = await client.post(f"{wp_url}/role-decisions", json={"plan_id":plan_id, **rd})
                if rr.status_code == 201:
                    wp_seeded += 1
            # Phi scenarios
            for sc in [
                {"scenario_name":"Deploy 2 more AI agents","base_phi":current_phi,"delta_phi":0.04,"mean_npf":0.60},
                {"scenario_name":"Hire 3 high-HAV humans","base_phi":current_phi,"delta_phi":-0.03,"mean_npf":0.68},
                {"scenario_name":"No change — organic drift","base_phi":current_phi,"delta_phi":0.01,"mean_npf":0.62},
            ]:
                await client.post(f"{wp_url}/phi-scenarios", json={"org_id":"demo-org","org_k":4, **sc})
        results["workforce_roles"] = wp_seeded

        # 12. Seed T&A sessions (6 per employee, spread over last 90 days)
        # This gives aggregate-hav real data to crunch immediately after seed.
        import math as _math
        ta_url     = SERVICES["time-attendance"]
        ta_sessions_all = []
        # Reference timestamp: 2026-06-23T09:00:00Z (today)
        base_ts = "2026-06-23T09:00:00+00:00"
        base_epoch = 1750676400  # unix seconds for 2026-06-23 09:00 UTC
        for e in DEMO:
            for i in range(6):
                days_ago  = 90 - i * 15          # 90, 75, 60, 45, 30, 15 days ago
                cin_epoch = base_epoch - days_ago * 86400
                cout_epoch = cin_epoch + 7 * 3600  # 7-hour session
                # Trend: improving employees get a small positive slope over time
                slope = 0.025 if e["trend"] == "improving" else (-0.015 if e["trend"] == "declining" else 0.0)
                t = i / 5.0  # 0..1
                jitter = (((e["id"][-3:].__hash__() + i * 17) % 100) - 50) / 1000.0  # deterministic ±0.05
                npf = max(0.05, min(0.99, e["npf"] + slope * t + jitter))
                srq = max(0.05, min(0.99, e["srq"] + slope * t * 0.8 + jitter * 0.6))
                oc  = max(0.05, min(0.99, e["oc"]  + slope * t * 0.6 + jitter * 0.4))
                from datetime import datetime as _dt, timezone as _tz
                cin_dt  = _dt.fromtimestamp(cin_epoch,  tz=_tz.utc).isoformat()
                cout_dt = _dt.fromtimestamp(cout_epoch, tz=_tz.utc).isoformat()
                ta_sessions_all.append({
                    "employee_id": e["id"], "org_id": org_id,
                    "checkin_at": cin_dt, "checkout_at": cout_dt,
                    "actual_npf": round(npf, 4), "srq_score": round(srq, 4), "oc_score": round(oc, 4),
                    "phi_at_checkin": round(phi, 4), "phi_star": 0.32,
                    "task_type": "mixed", "source": "demo_seed",
                })
        ta_import = await client.post(
            f"{ta_url}/sessions/import",
            json={"sessions": ta_sessions_all, "phi_star_default": 0.32},
        )
        results["ta_sessions_seeded"] = (
            ta_import.json().get("inserted", 0) if ta_import.status_code == 201 else 0
        )

        # 13. Seed Market360 AI agent lifecycle registry
        af_url = SERVICES["agent-factory"]
        m360_agents = [
            # Customers department
            {
                "org_id": "market360", "name": "CustomerIQ-01",
                "description": "Classifies inbound customer requests and routes to the right team",
                "department": "customers", "pipeline": "request-classification",
                "framework": "langgraph", "phi_contribution": 0.015,
                "tasks_automated": ["classify inbound request", "extract intent", "route to team"],
                "daily_runs": 420, "value_per_run": 4.50, "oversight_human": "maya.chen@demo.com",
                "onboarded_by": "priya.sharma@demo.com",
            },
            {
                "org_id": "market360", "name": "ChurnGuard-01",
                "description": "Predicts churn risk from CRM signals and triggers retention playbook",
                "department": "customers", "pipeline": "churn-prediction",
                "framework": "custom", "phi_contribution": 0.012,
                "tasks_automated": ["score churn risk", "generate retention offer", "log to CRM"],
                "daily_runs": 180, "value_per_run": 12.00, "oversight_human": "james.okonkwo@demo.com",
                "onboarded_by": "maya.chen@demo.com",
            },
            # Sales department
            {
                "org_id": "market360", "name": "LeadScorer-01",
                "description": "Scores inbound leads from web forms and assigns to sales reps",
                "department": "sales", "pipeline": "lead-scoring",
                "framework": "custom", "phi_contribution": 0.018,
                "tasks_automated": ["score lead", "enrich from LinkedIn", "assign to rep", "log activity"],
                "daily_runs": 300, "value_per_run": 8.00, "oversight_human": "sofia.reyes@demo.com",
                "onboarded_by": "priya.sharma@demo.com",
            },
            {
                "org_id": "market360", "name": "ProposalBot-01",
                "description": "Generates first-draft sales proposals from deal context",
                "department": "sales", "pipeline": "proposal-generation",
                "framework": "crewai", "phi_contribution": 0.014,
                "tasks_automated": ["extract deal context", "generate proposal draft", "attach pricing"],
                "daily_runs": 45, "value_per_run": 35.00, "oversight_human": "alex.mercer@demo.com",
                "onboarded_by": "sofia.reyes@demo.com",
            },
            # Planning department
            {
                "org_id": "market360", "name": "DemandPlanner-01",
                "description": "Forecasts demand signals from sales data + external feeds",
                "department": "planning", "pipeline": "demand-forecasting",
                "framework": "autogen", "phi_contribution": 0.020,
                "tasks_automated": ["aggregate sales signals", "run forecast model", "generate report"],
                "daily_runs": 12, "value_per_run": 85.00, "oversight_human": "priya.sharma@demo.com",
                "onboarded_by": "james.okonkwo@demo.com",
            },
            {
                "org_id": "market360", "name": "BudgetAlert-01",
                "description": "Monitors spend vs plan and raises alerts when thresholds breach",
                "department": "planning", "pipeline": "budget-monitoring",
                "framework": "custom", "phi_contribution": 0.008,
                "tasks_automated": ["check spend vs plan", "compute variance", "trigger alert if >5%"],
                "daily_runs": 96, "value_per_run": 3.00, "oversight_human": "maya.chen@demo.com",
                "onboarded_by": "priya.sharma@demo.com",
            },
            # Fulfillment department
            {
                "org_id": "market360", "name": "OrderRouter-01",
                "description": "Routes orders to the optimal warehouse based on stock and geo",
                "department": "fulfillment", "pipeline": "order-routing",
                "framework": "langgraph", "phi_contribution": 0.022,
                "tasks_automated": ["check stock levels", "select warehouse", "create pick ticket", "update ERP"],
                "daily_runs": 650, "value_per_run": 2.20, "oversight_human": "alex.mercer@demo.com",
                "onboarded_by": "sofia.reyes@demo.com",
            },
            {
                "org_id": "market360", "name": "DeliveryChaser-01",
                "description": "Monitors delivery status and proactively notifies customers of delays",
                "department": "fulfillment", "pipeline": "delivery-monitoring",
                "framework": "custom", "phi_contribution": 0.010,
                "tasks_automated": ["poll carrier API", "detect delay", "draft customer notification", "log event"],
                "daily_runs": 240, "value_per_run": 1.80, "oversight_human": "james.okonkwo@demo.com",
                "onboarded_by": "alex.mercer@demo.com",
            },
        ]
        m360_seeded = 0
        for ag in m360_agents:
            ar = await client.post(f"{af_url}/agents/onboard", json=ag)
            if ar.status_code == 201:
                m360_seeded += 1
        results["market360_agents"] = m360_seeded

    results.update({"org_phi": round(phi, 4), "r_ap": round(r_ap, 4), "org_id": org_id})
    return {"status": "seeded", **results}


@app.post("/aggregate-hav")
async def aggregate_hav(org_id: str = "demo-org"):
    """
    HAV Aggregation Pipeline: reads measured HAV from T&A sessions → writes real performance reviews.
    This closes the loop: T&A measured HAV → performance → twin calibration.
    """
    from datetime import date as _date
    results = {
        "org_id": org_id, "employees_updated": 0,
        "employees_no_sessions": 0, "cycle_id": None, "errors": [],
    }

    perf_url   = SERVICES["performance"]
    ta_url     = SERVICES["time-attendance"]
    people_url = SERVICES["people"]

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. Get or create a "Live HAV" performance cycle
        cycles_r = await client.get(f"{perf_url}/cycles?org_id={org_id}")
        cycle_id = None
        if cycles_r.status_code == 200:
            for c in cycles_r.json().get("cycles", []):
                if "Live HAV" in c.get("name", ""):
                    cycle_id = c["id"]; break

        if not cycle_id:
            today = _date.today().isoformat()
            cr = await client.post(f"{perf_url}/cycles", json={
                "org_id": org_id, "name": "Live HAV (T&A Measured)",
                "cycle_type": "continuous", "start_date": today, "end_date": today,
            })
            if cr.status_code == 201:
                cycle_id = cr.json()["cycle_id"]

        if not cycle_id:
            return {"error": "Could not create performance cycle", **results}
        results["cycle_id"] = cycle_id

        # 2. Get all human capital units
        people_r = await client.get(f"{people_url}/units?unit_type=human")
        if people_r.status_code != 200:
            return {"error": "Could not reach people service", **results}
        employees = people_r.json().get("units", [])
        results["total_employees"] = len(employees)

        # 3. For each employee: fetch T&A sessions → compute real HAV → write review
        for emp in employees:
            emp_id = emp["employee_id"]
            try:
                sess_r = await client.get(
                    f"{ta_url}/sessions?employee_id={emp_id}&org_id={org_id}&limit=50"
                )
                if sess_r.status_code != 200:
                    continue

                completed = [
                    s for s in sess_r.json().get("sessions", [])
                    if s.get("state") == "completed" and s.get("hav_score") is not None
                ]
                if not completed:
                    results["employees_no_sessions"] += 1
                    continue

                mean_hav = sum(s["hav_score"] for s in completed) / len(completed)
                mean_npf = sum(s.get("actual_npf") or s.get("declared_npf") or mean_hav for s in completed) / len(completed)
                mean_srq = sum(s.get("srq_score") or mean_hav * 0.9 for s in completed) / len(completed)
                mean_oc  = sum(s.get("oc_score")  or mean_hav * 0.8 for s in completed) / len(completed)
                phi_guard = sum(1 for s in completed if s.get("shift_type") == "phi_guardian")
                above_xover = sum(1 for s in completed if (s.get("hav_score") or 0) > 0.25) / len(completed)

                # HAV trend: compare first half vs second half
                mid = max(1, len(completed) // 2)
                fh  = sum(s["hav_score"] for s in completed[:mid]) / mid
                sh  = sum(s["hav_score"] for s in completed[mid:]) / max(1, len(completed) - mid)
                trend = "improving" if sh > fh + 0.02 else ("declining" if sh < fh - 0.02 else "stable")

                rev_r = await client.post(f"{perf_url}/reviews", json={
                    "cycle_id": cycle_id, "employee_id": emp_id,
                    "mean_hav": round(mean_hav, 4), "mean_npf": round(mean_npf, 4),
                    "mean_srq": round(mean_srq, 4), "mean_oc":  round(mean_oc, 4),
                    "hav_trend": trend, "npf_trend": trend,
                    "above_crossover_pct": round(above_xover, 4),
                    "phi_guardian_sessions": phi_guard,
                    "narrative": f"Auto-aggregated from {len(completed)} T&A sessions. HAV={mean_hav:.4f} (measured, not seeded).",
                })
                if rev_r.status_code == 201:
                    results["employees_updated"] += 1
            except Exception as e:
                results["errors"].append(str(e))

        # 4. Recalibrate twin with the freshly-measured HAV data
        if results["employees_updated"] > 0:
            try:
                twin_url = SERVICES["twin"]
                tc = await client.post(f"{twin_url}/orgs/{org_id}/calibrate?epochs=3")
                if tc.status_code == 201:
                    td = tc.json()
                    results["twin_recalibrated"] = True
                    results["twin_phi"]          = td.get("phi")
                    results["twin_phi_star"]     = td.get("phi_star")
                    results["twin_crossover"]    = td.get("crossover")
                else:
                    results["twin_recalibrated"] = False
            except Exception:
                results["twin_recalibrated"] = False

    return {"status": "aggregated", **results}


@app.post("/import/hav-bootstrap")
async def hav_bootstrap(body: dict, org_id: str = "demo-org"):
    """
    Customer onboarding import endpoint.

    Accepts historical HR data in a normalised format (Workday, BambooHR,
    pulse surveys, manager assessments, or manual) and bootstraps the
    Tessera HAV engine without needing live T&A check-in/checkout events.

    Payload schema:
    {
      "org_id": "acme-corp",          // optional — overrides query param
      "phi_star": 0.32,               // optional — org φ* (default 0.32)
      "source": "workday",            // "workday" | "bamboohr" | "survey" | "manual"
      "employees": [
        {
          "employee_id": "e-123",
          "employee_name": "Alice Smith",
          "role": "Senior Engineer",
          "department": "Engineering",
          "salary": 140000,           // optional — used for people service
          "periods": [
            {
              "label": "2025-Q4",
              "npf": 0.78,            // Non-Procedure Fraction (0–1)
              "srq": 0.72,            // SLA Recovery Quality (0–1)
              "oc":  0.68,            // Origination Capacity (0–1)
              "hours": 480            // total hours in period (default 160)
            }
          ]
        }
      ]
    }

    For simpler sources (survey, manual) that only provide an overall
    engagement/performance score, supply:
      "overall_score": 0.75
    instead of npf/srq/oc — the endpoint will distribute it across the
    three components using the HAVCPN default weighting.

    Returns: { imported_sessions, employees_registered, aggregate_result }
    """
    from datetime import date as _date, datetime as _dt, timezone as _tz, timedelta as _td
    import math as _math

    eff_org_id  = body.get("org_id", org_id)
    phi_star    = float(body.get("phi_star", 0.32))
    source      = body.get("source", "import")
    employees   = body.get("employees", [])

    if not employees:
        return {"error": "No employees provided in payload."}

    ta_url     = SERVICES["time-attendance"]
    people_url = SERVICES["people"]

    sessions_payload = []
    registered = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Register employees in people service (idempotent via upsert)
        for emp in employees:
            eid = emp["employee_id"]
            await client.post(f"{people_url}/units", json={
                "employee_id": eid,
                "name": emp.get("employee_name", eid),
                "unit_type": "human",
                "department": emp.get("department", "Unknown"),
                "role_title": emp.get("role", "Unknown"),
                "annual_salary": emp.get("salary", 0),
                "monthly_token_allocation": 8000,
            })
            registered += 1

            # Convert each period to a T&A session
            periods = emp.get("periods", [])
            # If no periods, create one synthetic session from overall_score
            if not periods and emp.get("overall_score") is not None:
                s = float(emp["overall_score"])
                periods = [{"label": "imported", "npf": s, "srq": s * 0.9, "oc": s * 0.8, "hours": 160}]

            # Sort periods chronologically (labels like "2025-Q1", "2025-Q4", "Jan-2025")
            now_ts = _dt.now(tz=_tz.utc)
            for idx, period in enumerate(periods):
                npf = float(period.get("npf") or period.get("overall_score") or 0.5)
                srq = float(period.get("srq") or npf * 0.9)
                oc  = float(period.get("oc")  or npf * 0.8)
                hours = float(period.get("hours", 160))
                # Place session: most recent period = 7 days ago; older = 7 + idx*30 days ago
                days_ago = 7 + (len(periods) - 1 - idx) * 30
                cin_dt   = now_ts - _td(days=days_ago)
                cout_dt  = cin_dt + _td(hours=hours / 20)  # spread over ~20 work-days
                sessions_payload.append({
                    "employee_id":  eid,
                    "org_id":       eff_org_id,
                    "checkin_at":   cin_dt.isoformat(),
                    "checkout_at":  cout_dt.isoformat(),
                    "actual_npf":   round(max(0.01, min(0.99, npf)), 4),
                    "srq_score":    round(max(0.01, min(0.99, srq)), 4),
                    "oc_score":     round(max(0.01, min(0.99, oc)),  4),
                    "phi_at_checkin": None,
                    "phi_star":     phi_star,
                    "task_type":    "mixed",
                    "source":       source,
                    "notes":        period.get("label", ""),
                })

        # Bulk-import all sessions in one call
        import_r = await client.post(
            f"{ta_url}/sessions/import",
            json={"sessions": sessions_payload, "phi_star_default": phi_star},
        )
        import_result = import_r.json() if import_r.status_code == 201 else {"error": import_r.text}

    # Auto-run aggregate-hav so performance reviews are immediately available
    agg = await aggregate_hav(org_id=eff_org_id)

    return {
        "status": "bootstrapped",
        "org_id": eff_org_id,
        "source": source,
        "employees_registered": registered,
        "imported_sessions": import_result,
        "aggregate_result": {
            "employees_updated": agg.get("employees_updated", 0),
            "employees_no_sessions": agg.get("employees_no_sessions", 0),
            "cycle_id": agg.get("cycle_id"),
        },
    }


@app.get("/signals")
async def get_signals(org_id: str = "demo-org"):
    """
    Aggregate live signals from all services into a ranked feed.
    Severity: critical > warning > info
    Model: SIGNAL (what) → RECORD (acknowledge) → ACTION (do)
    """
    signals = []
    now_iso = __import__("datetime").datetime.utcnow().isoformat() + "Z"

    def sig(type_, severity, title, body, source, action_label=None, action_nav=None, detail=None):
        signals.append({
            "type": type_, "severity": severity,
            "title": title, "body": body, "source": source,
            "action_label": action_label, "action_nav": action_nav,
            "detail": detail, "timestamp": now_iso,
        })

    async with httpx.AsyncClient(timeout=5.0) as client:
        # ── Twin signals ──────────────────────────────────────────────────
        twin_url = SERVICES["twin"]
        twin_sim_id = None
        try:
            r = await client.get(f"{twin_url}/orgs/{org_id}/sim")
            if r.status_code == 200:
                twin_sim_id = r.json().get("sim_id")
        except Exception:
            pass

        if twin_sim_id:
            try:
                r = await client.get(f"{twin_url}/sim/{twin_sim_id}/early-warning")
                if r.status_code == 200:
                    ew = r.json()
                    ev = ew.get("evidence", {})
                    phi      = ev.get("phi", 0)
                    phi_star = ev.get("phi_star", 0.32)
                    gap      = ev.get("alignment_gap", 0)
                    drift    = ev.get("template_drift", 0)
                    mhav     = ev.get("mean_hav")
                    nudge    = ev.get("track2_nudge", False)
                    stage    = ew.get("stage", 0)
                    n_auto   = ev.get("n_autonomous", 0)
                    regime   = ev.get("measurement_regime", "HAV")

                    if stage == 2:
                        sig("stage2", "critical",
                            "Stage 2 governance failure",
                            f"Template drift {drift:.3f}. Decision quality degraded. Suspend AI replacements and inject diverse probes.",
                            "twin", "View Twin →", "twin",
                            detail=f"alignment_gap={gap:.3f} · drift={drift:.3f}")
                    if nudge:
                        sig("track2_nudge", "critical",
                            "Track 2 nudge active",
                            f"MAN_HOURS regime is shifting org beliefs toward AI centroid. Switch measurement regime to HAV immediately.",
                            "twin", "View Twin →", "twin")
                    if phi > phi_star:
                        sig("phi_crossover", "warning",
                            f"φ crossover reached (φ={phi:.3f} > φ*={phi_star:.3f})",
                            f"AI fraction exceeds critical threshold. HAV governance regime is now required — man-hours undercounts human contribution.",
                            "twin", "View Twin →", "twin",
                            detail=f"K=4 · regime={regime}")
                    elif phi > phi_star * 0.85:
                        sig("phi_approaching", "warning",
                            f"φ approaching crossover (φ={phi:.3f}, φ*={phi_star:.3f})",
                            f"φ is within 15% of crossover. Begin HAV regime preparation.",
                            "twin", "View Twin →", "twin")
                    if gap > 0.15 and stage < 2:
                        sig("alignment_gap", "warning",
                            f"Persistent alignment gap ({gap:.3f})",
                            f"Belief misalignment between org template and agents has persisted. Decision quality at risk.",
                            "twin", "View Twin →", "twin")
                    if mhav is not None and mhav < 0.30:
                        sig("hav_critical", "critical",
                            f"Mean HAV critically low ({mhav:.3f})",
                            "Organisation operating in mostly procedural mode. Novel problem capacity near zero.",
                            "twin", "View Twin →", "twin")
                    elif mhav is not None and mhav < 0.45:
                        sig("hav_low", "warning",
                            f"Mean HAV below healthy threshold ({mhav:.3f})",
                            "Consider role rotation to restore novel problem exposure. Target HAV ≥ 0.50.",
                            "twin", "View Performance →", "performance")
                    if n_auto > 0 and mhav is not None and mhav < 0.40:
                        sig("autonomous_hav_risk", "warning",
                            f"{n_auto} autonomous unit(s) + declining human HAV",
                            "Physical autonomous capital may be absorbing novel work from humans. Rotate roles to restore NPF.",
                            "twin", "View People →", "people")
            except Exception:
                pass

        # ── Performance signals ───────────────────────────────────────────
        try:
            r = await client.get(f"{SERVICES['performance']}/reviews")
            if r.status_code == 200:
                revs = r.json().get("reviews", [])
                NAMES = {
                    "emp-001":"Maya Chen","emp-002":"James Okonkwo","emp-003":"Sofia Reyes",
                    "emp-004":"Alex Mercer","emp-005":"Jordan Park","emp-006":"Sam Williams",
                    "emp-007":"Priya Sharma","emp-008":"Marcus Lee","emp-009":"Emma Wilson","emp-010":"David Kim",
                }
                # Deduplicate: one signal per employee (take lowest HAV across dupes — most cautious)
                by_emp = {}
                for rv in revs:
                    eid = rv.get("employee_id","")
                    if eid not in by_emp or rv.get("mean_hav", 1) < by_emp[eid].get("mean_hav", 1):
                        by_emp[eid] = rv
                for rv in by_emp.values():
                    name  = NAMES.get(rv.get("employee_id"), rv.get("employee_id","?"))
                    hav   = rv.get("mean_hav", 0)
                    trend = rv.get("hav_trend","")
                    isVC  = hav >= 0.70 and rv.get("mean_npf", 0) >= 0.65
                    if trend == "declining" and isVC:
                        sig(f"vc_declining_{rv['employee_id']}", "critical",
                            f"Values Custodian declining — {name}",
                            f"HAV={hav:.3f} trending down. Retention risk: this employee is a φ-guardian. Intervene before crossover.",
                            "performance", "View People →", "people",
                            detail=f"HAV={hav:.3f} · VC status")
                    elif trend == "declining" and hav < 0.45:
                        sig(f"hav_declining_{rv['employee_id']}", "warning",
                            f"{name} HAV declining",
                            f"HAV={hav:.3f} and falling. Candidate for role rotation or additional novel-problem exposure.",
                            "performance", "View Performance →", "performance")
        except Exception:
            pass

        # ── Benefits / VC retention signals ───────────────────────────────
        try:
            r = await client.get(f"{SERVICES['benefits']}/retention-alerts?org_id={org_id}")
            if r.status_code == 200:
                alerts = r.json().get("alerts", [])
                # Deduplicate by employee_id — keep highest severity per employee
                by_emp_alert = {}
                for a in alerts:
                    eid = a.get("employee_id","")
                    sev = a.get("severity","")
                    if eid not in by_emp_alert or sev == "critical":
                        by_emp_alert[eid] = a
                uniq = list(by_emp_alert.values())
                vc_critical = [a for a in uniq if a.get("severity") == "critical"]
                vc_high     = [a for a in uniq if a.get("severity") == "high"]
                if vc_critical:
                    sig("vc_retention", "critical",
                        f"{len(vc_critical)} Values Custodian(s) flagged critical",
                        "Critical-severity retention risk. These employees hold org memory and novel-problem capacity. Departure = permanent knowledge loss.",
                        "benefits", "View People →", "people",
                        detail=f"{len(vc_critical)} critical · {len(vc_high)} high")
                elif vc_high:
                    sig("vc_retention_high", "warning",
                        f"{len(vc_high)} Values Custodian(s) at risk",
                        "High-severity retention risk. Engage before these employees enter declining HAV trajectory.",
                        "benefits", "View People →", "people")
        except Exception:
            pass

        # ── Time & Attendance signals ─────────────────────────────────────
        try:
            r = await client.get(f"{SERVICES['time-attendance']}/org-hav-summary?org_id={org_id}&last_days=30")
            if r.status_code == 200:
                ta = r.json()
                n_sess = ta.get("n_sessions", 0)
                ta_hav = ta.get("mean_hav")
                if n_sess == 0:
                    sig("no_ta_sessions", "info",
                        "No time-attendance sessions recorded",
                        "HAV is currently seeded from performance reviews, not measured. Check employees in via Time & Attendance to get real HAV data.",
                        "time-attendance", "Go to T&A →", "timeattendance")
                elif ta_hav is not None and n_sess > 0:
                    sig("ta_hav_measured", "info",
                        f"HAV measured from {n_sess} real session(s) — mean {ta_hav:.3f}",
                        "Twin is calibrated from measured HAV, not seeded data. This is the live org mirror.",
                        "time-attendance", "View T&A →", "timeattendance")
        except Exception:
            pass

        # ── Recruiting signals ────────────────────────────────────────────
        try:
            r = await client.get(f"{SERVICES['recruiting']}/requisitions?org_id={org_id}")
            if r.status_code == 200:
                reqs = r.json().get("requisitions", [])
                # Deduplicate by title — additive seeding creates duplicate reqs
                seen_titles = set()
                uniq_reqs = []
                for q in reqs:
                    t = q.get("title","")
                    if t not in seen_titles:
                        seen_titles.add(t)
                        uniq_reqs.append(q)
                phi_reqs = [q for q in uniq_reqs if q.get("phi_role") == "phi_guardian"]
                if phi_reqs:
                    sig("open_phi_roles", "warning",
                        f"{len(phi_reqs)} open φ-guardian role(s)",
                        f"φ-guardian positions unfilled. Each open seat reduces AI SLA coverage. Prioritise HAV-scored candidates.",
                        "recruiting", "View Recruiting →", "recruiting",
                        detail=", ".join(q.get("title","?") for q in phi_reqs[:3]))
        except Exception:
            pass

        # ── Governance signals ────────────────────────────────────────────
        try:
            gov_url = SERVICES["governance"]
            r = await client.get(f"{gov_url}/state/{org_id}")
            if r.status_code == 200:
                gs = r.json()
                stage    = gs.get("stage", 0)
                drift    = gs.get("template_drift", 0) or 0
                gap      = gs.get("alignment_gap", 0) or 0
                regime   = gs.get("measurement_regime", "HAV")
                nudge    = gs.get("track2_nudge", False)
                if regime != "HAV" and regime:
                    sig("wrong_measurement_regime", "critical",
                        f"Wrong measurement regime: {regime}",
                        "Organisation is measuring human contribution in MAN_HOURS mode above φ*. "
                        "This accelerates Track 2 pathology. Switch to HAV immediately.",
                        "governance", "View Twin →", "twin",
                        detail=f"regime={regime} · drift={drift:.3f}")
                if drift > 0.20 and stage < 2:
                    sig("template_drift_high", "warning",
                        f"Template drift elevated ({drift:.3f})",
                        "Agent belief vectors diverging from org template faster than HAV can reabsorb. "
                        "Probe injection recommended before Stage 2 threshold.",
                        "governance", "View Twin →", "twin",
                        detail=f"drift={drift:.3f} · gap={gap:.3f}")
        except Exception:
            pass

        # ── Onboarding signals ────────────────────────────────────────────
        try:
            onb_url = SERVICES["onboarding"]
            r = await client.get(f"{onb_url}/summary")
            if r.status_code == 200:
                ob = r.json()
                offboarding   = ob.get("offboarding_active", 0)
                ai_onboarding = ob.get("ai_agent_onboarding_active", 0)
                mutations     = ob.get("mutations_triggered", [])
                h2a = sum(1 for m in mutations if m == "T_replace_h2a")
                a2h = sum(1 for m in mutations if m == "T_replace_a2h")
                probe = sum(1 for m in mutations if m == "T_probe_entry")
                if offboarding > 0:
                    sig("offboarding_active", "warning",
                        f"{offboarding} offboarding journey(s) in progress",
                        "Tacit knowledge capture must complete before departure. "
                        "Incomplete offboarding = permanent structural knowledge loss.",
                        "onboarding", "View Onboarding →", "onboarding",
                        detail=f"{offboarding} offboarding · {h2a} T_replace_h2a · {a2h} T_replace_a2h")
                if h2a > 0:
                    sig("mutation_h2a", "warning",
                        f"{h2a} human→AI replacement mutation(s) triggered",
                        f"T_replace_h2a active: {h2a} role(s) transitioning to AI agents. "
                        "φ will rise — verify org is not approaching crossover without HAV governance.",
                        "onboarding", "View Twin →", "twin",
                        detail=f"T_replace_h2a × {h2a}")
                if ai_onboarding > 0:
                    sig("ai_agent_onboarding", "info",
                        f"{ai_onboarding} AI agent(s) being onboarded",
                        "New AI capital units entering the org. Mandate coherence and RAG depth "
                        "will be tracked from activation.",
                        "onboarding", "View Onboarding →", "onboarding")
                if probe > 0:
                    sig("probe_entry", "info",
                        f"{probe} outsider probe injection(s) active",
                        "T_probe_entry journeys in progress. Belief diversity injection underway — "
                        "monitor template drift for absorption signal.",
                        "onboarding", "View Twin →", "twin")
        except Exception:
            pass

        # ── Absence / VC coverage gap signals ────────────────────────────
        try:
            absence_url = SERVICES["absence"]
            r = await client.get(f"{absence_url}/requests?is_vc=true&status=pending")
            if r.status_code == 200:
                vc_reqs = r.json().get("requests", [])
                NAMES = {
                    "emp-001":"Maya Chen","emp-002":"James Okonkwo","emp-003":"Sofia Reyes",
                    "emp-004":"Alex Mercer","emp-005":"Jordan Park","emp-006":"Sam Williams",
                    "emp-007":"Priya Sharma","emp-008":"Marcus Lee","emp-009":"Emma Wilson","emp-010":"David Kim",
                }
                if vc_reqs:
                    for req in vc_reqs:
                        eid      = req.get("employee_id","?")
                        name     = NAMES.get(eid, eid)
                        days     = req.get("days_requested", 0)
                        hav_imp  = req.get("hav_impact") or 0
                        phi_imp  = req.get("phi_coverage_impact") or 0
                        start    = req.get("start_date","?")
                        sig(
                            f"vc_absence_gap_{eid}",
                            "critical",
                            f"VC absence unresolved — {name}",
                            f"{days}d leave from {start} · HAV impact={hav_imp:.2f} · φ-coverage gap={phi_imp:.3f}. "
                            f"Assign coverage before approving — VC absence removes {name}'s novel-problem capacity from active org coverage.",
                            "absence",
                            "View Absence →", "absence",
                            detail=f"HAV impact={hav_imp:.2f} · φ gap={phi_imp:.3f}",
                        )
        except Exception:
            pass

        # ── ITSM / SLA breach signals ────────────────────────────────────
        try:
            itsm_url = SERVICES["itsm"]
            ITSM_ORG = "00000000-0000-0000-0000-000000000001"
            # at-risk tickets
            ar = await client.get(f"{itsm_url}/sla/at-risk?org_id={ITSM_ORG}&hours_ahead=72")
            if ar.status_code == 200:
                at_risk = ar.json().get("at_risk", [])
                for t in at_risk[:5]:
                    tid   = t.get("id", t.get("ticket_id", "?"))
                    prio  = t.get("priority", "P?")
                    title = t.get("title", "Incident")
                    sla_at = t.get("sla_resolve_at")
                    if sla_at:
                        from datetime import datetime as _dt2, timezone as _tz2
                        sla_dt = _dt2.fromisoformat(sla_at.replace("Z","+00:00"))
                        rem_m  = int((sla_dt - _dt2.now(_tz2.utc)).total_seconds() / 60)
                        hrs    = f"{rem_m//60}h {rem_m%60}m" if rem_m > 0 else "OVERDUE"
                    else:
                        hrs = "?"
                    sev   = "critical" if prio in ("P1", "P2") else "warning"
                    sig(
                        f"sla_at_risk_{tid}",
                        sev,
                        f"SLA breach imminent — [{prio}] {title[:50]}",
                        f"{hrs} until SLA breach. Incident resolution quality (SRQ) will "
                        f"be measured and fed into the assigned agent's HAV score.",
                        "itsm",
                        "Resolve in ITSM →", "itsm",
                        detail=f"SLA window: {hrs} remaining",
                    )
            # breached tickets
            br = await client.get(f"{itsm_url}/sla/breaches?org_id={ITSM_ORG}")
            if br.status_code == 200:
                breaches = br.json().get("breaches", [])
                for t in breaches[:3]:
                    tid   = t.get("id", t.get("ticket_id", "?"))
                    prio  = t.get("priority", "P?")
                    title = t.get("title", "Incident")
                    sig(
                        f"sla_breached_{tid}",
                        "critical",
                        f"SLA breached — [{prio}] {title[:50]}",
                        f"Ticket past SLA deadline. Resolution SRQ will be penalised. "
                        f"Tessera will log SRQ=0.10 if ticket remains unresolved for 48h.",
                        "itsm",
                        "Resolve Now →", "itsm",
                        detail="SLA breached — SRQ penalty active",
                    )
        except Exception:
            pass

    # Rank: critical first, then warning, then info; stable sort
    order = {"critical": 0, "warning": 1, "info": 2}
    signals.sort(key=lambda s: order.get(s["severity"], 9))

    return {
        "org_id": org_id,
        "signals": signals,
        "total": len(signals),
        "critical": sum(1 for s in signals if s["severity"] == "critical"),
        "warning":  sum(1 for s in signals if s["severity"] == "warning"),
        "info":     sum(1 for s in signals if s["severity"] == "info"),
        "twin_sim_id": twin_sim_id,
    }


@app.api_route(
    "/api/v1/{service}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
)
async def proxy(service: str, path: str, request: Request):
    """
    Proxy all /api/v1/{service}/... requests to the correct microservice.
    Forwards headers, body, and query params transparently.
    """
    if service not in SERVICES:
        raise HTTPException(
            status_code=404,
            detail=f"Service '{service}' not found. Available: {list(SERVICES.keys())}"
        )

    target_url = f"{SERVICES[service]}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    # Forward request
    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }

    logger.info(f"→ {request.method} {service}/{path}")

    try:
        async with httpx.AsyncClient(timeout=65.0) as client:
            response = await client.request(
                method=request.method,
                url=target_url,
                content=body,
                headers=headers,
            )
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.headers.get("content-type", "application/json"),
        )
    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail=f"Service '{service}' is unavailable. Is it running?"
        )
    except Exception as e:
        logger.error(f"Gateway error: {e}")
        raise HTTPException(status_code=502, detail=str(e))
