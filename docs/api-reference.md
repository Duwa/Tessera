# Tessera API Reference

Base URL: `https://your-tessera.com/api/v1`  
All endpoints return JSON. Authentication: coming in v2 (JWT org claims).

---

## Customer Onboarding

### Bootstrap HAV from historical data
```
POST /import/hav-bootstrap
```
The single most important endpoint. Accepts Workday/BambooHR/survey export, registers employees, creates T&A sessions, runs HAV pipeline, calibrates twin.

**Body:**
```json
{
  "org_id": "your-org",
  "phi_star": 0.32,
  "source": "workday | bamboohr | survey | manual",
  "employees": [
    {
      "employee_id": "string",
      "employee_name": "string",
      "role": "string",
      "department": "string",
      "salary": 120000,
      "periods": [
        { "label": "2025-Q4", "npf": 0.72, "srq": 0.68, "oc": 0.55, "hours": 480 }
      ]
    }
  ]
}
```

**Alternative — overall_score instead of npf/srq/oc:**
```json
{ "label": "2025-Q4", "overall_score": 0.74, "hours": 480 }
```

**Response:**
```json
{
  "status": "ok",
  "employees_registered": 47,
  "imported_sessions": 141,
  "aggregate_result": { "employees_updated": 47, "mean_hav": 0.634, "values_custodians": 8 },
  "twin_recalibrated": true,
  "twin_phi": 0.087,
  "twin_phi_star": 0.32
}
```

---

## Signal Feed

### Get organisational intelligence signals
```
GET /signals?org_id=your-org
```

Returns prioritised alerts across 8 signal sources. Critical → warning → info.

**Response:**
```json
{
  "signals": [
    {
      "id": "vc_absence_gap_emp-001",
      "severity": "critical | warning | info",
      "title": "string",
      "body": "string",
      "source": "twin | performance | benefits | time-attendance | recruiting | absence | governance | itsm",
      "action_label": "string",
      "action_nav": "string",
      "detail": "string",
      "ts": "2026-06-24T09:00:00Z"
    }
  ],
  "total": 12,
  "critical": 3,
  "warning": 6,
  "info": 3
}
```

---

## HAV Aggregation Pipeline

### Run HAV aggregation
```
POST /aggregate-hav
{"org_id": "your-org"}
```
Reads all completed T&A sessions, computes mean HAV/NPF/SRQ/OC per employee, updates performance reviews, identifies Values Custodians, recalibrates digital twin.

Called automatically after `/import/hav-bootstrap`. Safe to call at any time.

**Response:**
```json
{
  "employees_updated": 10,
  "mean_hav": 0.634,
  "values_custodians": 3,
  "twin_recalibrated": true,
  "twin_phi": 0.087,
  "twin_phi_star": 0.32,
  "twin_crossover": false
}
```

---

## People Registry

### Register employee
```
POST /api/v1/people/units
```
```json
{
  "unit_id": "emp-001",
  "unit_type": "human",
  "name": "Jane Smith",
  "role": "Senior Engineer",
  "department": "Engineering",
  "salary": 145000,
  "hav_score": 0.72,
  "npf_score": 0.68,
  "org_id": "your-org"
}
```

### Get org composition
```
GET /api/v1/people/composition?org_id=your-org
```
Returns human vs AI unit counts, mean HAV, φ fraction.

---

## Time & Attendance (HAV Measurement)

### Import sessions in bulk
```
POST /api/v1/time-attendance/sessions/import
```
```json
{
  "sessions": [
    {
      "employee_id": "emp-001",
      "org_id": "your-org",
      "checkin_at": "2026-06-24T09:00:00Z",
      "checkout_at": "2026-06-24T17:00:00Z",
      "actual_npf": 0.72,
      "srq_score": 0.68,
      "oc_score": 0.55,
      "task_type": "project_work | srq_resolution | mixed",
      "source": "jira | servicenow | manual",
      "notes": "Sprint 42 delivery"
    }
  ],
  "phi_star_default": 0.32
}
```

**Response:** `{ "inserted": 10, "skipped": 0, "total": 10 }`

### Get org HAV summary
```
GET /api/v1/time-attendance/org-hav-summary?org_id=your-org
```

---

## Digital Twin

### Get twin state
```
GET /api/v1/twin/orgs/{org_id}/role-predictions
```
Returns current φ, φ*, stage, mean HAV, Values Custodian count.

### Get φ history
```
GET /api/v1/twin/orgs/{org_id}/phi-history?last_n=20
```

### Calibrate twin
```
POST /api/v1/twin/orgs/{org_id}/calibrate?epochs=5
```
Re-runs NK landscape simulation with current employee HAV data.

### Early warning
```
GET /api/v1/twin/sim/{sim_id}/early-warning
```
Returns stage (0/1/2), phi, phi_star, evidence dict, belief drift metrics.

---

## Performance

### Run HAV aggregation → performance reviews
```
POST /api/v1/performance/aggregate-from-ta?org_id=your-org
```
Reads T&A sessions, creates/updates review records. Called automatically by `/aggregate-hav`.

### List reviews
```
GET /api/v1/performance/reviews?org_id=your-org&limit=50
```

---

## ITSM

### Create ticket
```
POST /api/v1/itsm/tickets
```
```json
{
  "org_id": "00000000-0000-0000-0000-000000000001",
  "ticket_type": "incident | request | problem | change",
  "title": "string",
  "description": "string",
  "priority": "P1 | P2 | P3 | P4",
  "category": "string",
  "reporter_email": "string",
  "assignee_email": "string",
  "team": "string"
}
```

Note: `org_id` for ITSM must be a valid UUID.

### Resolve ticket → SRQ bridge
```
POST /api/v1/itsm/tickets/{ticket_id}/resolve
{ "resolution": "string", "resolved_by": "email@example.com" }
```
After resolving, compute SRQ and import a T&A session:
```
SRQ = max(0.10, 1 − elapsed_hours / sla_hours)
POST /api/v1/time-attendance/sessions/import with srq_score=SRQ
```

### SLA monitoring
```
GET /api/v1/itsm/sla/at-risk?org_id=UUID&hours_ahead=72
GET /api/v1/itsm/sla/breaches?org_id=UUID
```

### CMDB — register CI / AI agent
```
POST /api/v1/itsm/cmdb
```
```json
{
  "org_id": "UUID",
  "name": "claude-agent-03",
  "ci_type": "service | server | network | software | other",
  "description": "string",
  "owner_email": "string",
  "status": "active | maintenance | retired"
}
```

---

## Workforce Planning

### Create headcount plan
```
POST /api/v1/workforce-planning/plans
{ "org_id": "your-org", "name": "FY2026 H2", "period": "2026-H2", "current_phi": 0.087, "org_k": 4 }
```

### Evaluate hire-human vs deploy-AI
```
POST /api/v1/workforce-planning/role-decisions
```
```json
{
  "plan_id": "uuid",
  "role_title": "Senior Data Scientist",
  "hav_required": 0.68,
  "npf_required": 0.60,
  "human_fitness": 0.88,
  "human_value_delivery": 145000,
  "human_comp_cost": 120000,
  "human_gov_cost": 6000,
  "ai_deployment_value": 90000,
  "ai_deployment_cost": 18000,
  "ai_oversight_cost": 12000,
  "headcount": 1
}
```
**Response includes:** `recommendation: "hire_human | deploy_ai | hybrid"`, `v_net_human`, `v_net_ai`, `rationale`

### φ scenario modelling
```
POST /api/v1/workforce-planning/phi-scenarios
{ "org_id": "your-org", "scenario_name": "Deploy 2 AI agents", "base_phi": 0.087, "delta_phi": 0.04, "org_k": 4, "mean_npf": 0.62 }
```
**Response:** `projected_phi`, `stays_above_crossover`, `nudge_acceleration`, `interpretation`

---

## Absence Management

### Submit leave request
```
POST /api/v1/absence/requests
```
```json
{
  "employee_id": "emp-001",
  "leave_type": "pto | sick | fmla | bereavement | unpaid",
  "start_date": "2026-07-14",
  "end_date": "2026-07-25",
  "days_requested": 10,
  "mean_hav": 0.82,
  "mean_npf": 0.75,
  "org_k": 4
}
```
If `mean_hav≥0.70 AND mean_npf≥0.65`, the employee is a Values Custodian and the response includes a coverage assignment requirement before approval.

---

## Recruiting

### Create requisition
```
POST /api/v1/recruiting/requisitions
{ "org_id": "your-org", "role_title": "string", "department": "string", "min_hav": 0.65, "is_phi_guardian": true }
```

### Score candidate
```
POST /api/v1/recruiting/candidates
{ "requisition_id": "uuid", "candidate_name": "string", "npf_score": 0.72, "srq_score": 0.68, "oc_score": 0.60 }
```
Twin predictions applied: `phi_guardian_fit`, `hav_score`, `recommendation`.

---

## Knowledge Management

### Search knowledge base
```
GET /api/v1/knowledge/search?q=your+query&org_id=your-org
```
Results ranked by relevance × author HAV × deflection count. VC-authored articles surface first.

### Track ticket deflection
```
POST /api/v1/knowledge/deflection
{ "article_id": "uuid", "query": "string", "did_ticket_deflect": true, "org_id": "your-org" }
```
Each deflection = $22 saved (default). Tracked in the Knowledge view.

---

## HAV Formula Reference

```
HAV(h, session) = 0.50 × NPF + 0.30 × SRQ + 0.20 × OC

Values Custodian:  HAV ≥ 0.70  AND  NPF ≥ 0.65

φ* crossover:
  K ≥ 6  →  φ* = 0.25
  K = 4  →  φ* = 0.32
  K ≤ 2  →  φ* = 0.44

Alignment Premium Rate:
  φ < 0.25   →  r_AP = 0.05
  φ > 0.75   →  r_AP = 0.25
  otherwise  →  r_AP = 0.05 + (φ − 0.25) × 0.40

Monthly Alignment Premium = r_AP × HAV × (annual_salary / 12)

SRQ from incident resolution:
  SRQ = max(0.10, min(0.95, 1 − elapsed_hours / sla_hours))

V_net(human) = fitness × value_delivery − comp_cost − gov_cost − edge_count × probe_cost
V_net(AI)    = deployment_value − deployment_cost − oversight_cost − edge_count × probe_cost
```
