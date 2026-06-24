# Tessera — Human and AI Capital Management (HCAM)

> The operating system for hybrid human + AI organisations.  
> One platform to replace Workday, ServiceNow, and every metric that got the AI era wrong.

---

## The problem with every existing platform

Workday measures **man-hours**. ServiceNow measures **tickets closed**. Jira measures **story points**.

These are the wrong metrics. None of them answer the question that actually matters in 2026:

> *As AI takes over procedural work, which humans are generating irreplaceable value — and how do you know when you've deployed too much AI?*

Tessera answers that question mathematically.

---

## The HCAM Model

Tessera introduces **HAV — Human Alignment Value**:

```
HAV(h, session) = 0.50 × NPF + 0.30 × SRQ + 0.20 × OC
```

| Component | What it measures |
|---|---|
| **NPF** — Non-Procedural Fraction | % of work that requires novel problem-solving |
| **SRQ** — Service Resolution Quality | How well the human resolved issues vs SLA |
| **OC** — Organisational Coherence | Belief alignment contribution to org direction |

Every HR decision, every IT service management action, every hiring call — all flow through HAV.

### The φ-crossover theorem

φ is the AI fraction of your organisation. When φ exceeds φ* (the crossover threshold), standard management models break down:

```
φ*(K≥6) = 0.25    φ*(K=4) = 0.32    φ*(K≤2) = 0.44
```

Above φ*, belief drift accelerates, Values Custodians become critical, and the Alignment Premium must rise to retain human judgment. **Tessera is the only platform that models this mathematically.**

---

## What Tessera replaces

| Platform | Their metric | Why it's wrong | Tessera's answer |
|---|---|---|---|
| Workday | Man-hours, headcount | Counts bodies, not contribution | HAV score per employee, per session |
| ServiceNow | Tickets closed | Counts volume, not quality | SRQ — resolution quality feeds HAV |
| Jira / Atlassian | Story points | Counts output, not value created | NPF — novel vs procedural work ratio |

**One hood. All three.**

---

## Quick Start

```bash
git clone https://github.com/automotonai/tessera
cd tessera
cp .env.example .env
docker compose up --build
```

- **Platform UI:** http://localhost
- **API Gateway + Swagger:** http://localhost:8000/docs
- **Seed demo data:** http://localhost/demo/seed

---

## Architecture

```
Browser
  └── nginx (port 80)
        ├── /            → frontend/platform.html (single-file SPA)
        ├── /api/v1/*    → gateway:8000 (proxy + aggregation)
        ├── /demo/seed   → gateway:8000 (demo data seed)
        ├── /signals     → gateway:8000 (signal feed)
        ├── /aggregate-hav → gateway:8000 (HAV pipeline)
        └── /import/*    → gateway:8000 (customer onboarding)

gateway:8000
  └── Proxies to 27 microservices on ports 8001–8027
        └── All backed by PostgreSQL (one DB per service)
```

---

## Services

### Workday replacement — Human Capital

| Port | Service | Module |
|------|---------|--------|
| 8002 | payroll | Salary + Alignment Premium (r_AP × HAV × salary) |
| 8005 | people | Unified capital registry — humans + AI agents |
| 8007 | onboarding | Journey-based onboarding; T_probe / T_replace mutations |
| 8018 | benefits | Retention risk; Values Custodian protection |
| 8019 | recruiting | Twin-predicted role fit; φ-guardian requisitions |
| 8020 | performance | HAV-based review cycles; T&A session aggregation |
| 8022 | compensation | Merit cycles with HAV weighting |
| 8023 | absence | HAV-impact leave; VC coverage gap alerts |
| 8003 | learning | HAV-gap learning plans; Huang learning ratio |
| 8024 | workforce-planning | Hire human vs deploy AI — V_net cost model + φ scenarios |

### ServiceNow replacement — IT Service Management

| Port | Service | Module |
|------|---------|--------|
| 8013 | itsm | Incidents, Changes (CAB), Problems, CMDB, SLA |
| 8027 | service-catalog | Requestable items with φ-guardian gating |
| 8026 | knowledge | VC-authored KB; deflection tracking ($22/deflection) |

### Tessera-unique — No equivalent exists

| Port | Service | What it does |
|------|---------|-------------|
| 8004 | twin | NK-landscape digital twin; belief drift tracking; φ history |
| 8008 | governance | HAV regime enforcement; Stage 0/1/2 pathology detection |
| 8010 | trace | Live event stream; agent execution tracing |
| 8017 | time-attendance | HAV measurement from work sessions; SRQ import bridge |

---

## Customer Onboarding

### Option A — Import your Workday / BambooHR data (5 minutes)

```bash
curl -X POST http://your-tessera/import/hav-bootstrap \
  -H "Content-Type: application/json" \
  -d '{
    "org_id": "your-org",
    "phi_star": 0.32,
    "source": "workday",
    "employees": [
      {
        "employee_id": "EMP-001",
        "employee_name": "Jane Smith",
        "role": "Senior Engineer",
        "department": "Engineering",
        "salary": 145000,
        "periods": [
          { "label": "2025-Q4", "npf": 0.72, "srq": 0.68, "oc": 0.55, "hours": 480 },
          { "label": "2026-Q1", "npf": 0.75, "srq": 0.71, "oc": 0.58, "hours": 480 }
        ]
      }
    ]
  }'
```

This single call:
1. Registers employees in the people registry
2. Converts historical periods to T&A sessions
3. Runs the HAV aggregation pipeline
4. Recalibrates the digital twin
5. Returns φ trajectory and Values Custodian identification

→ **Full guide:** [docs/onboarding.md](docs/onboarding.md)

### Option B — Seed demo data

```
GET /demo/seed
```

Seeds 10 employees, 60 T&A sessions, 5 ITSM tickets, 4 CMDB items, 5 role decisions, and 3 φ scenarios. Live in 3 seconds.

---

## The Signal Feed

`GET /signals` returns a prioritised list of organisational intelligence — things Workday and ServiceNow cannot surface:

```json
{
  "signals": [
    {
      "type": "vc_absence_gap_emp-001",
      "severity": "critical",
      "title": "VC absence unresolved — Maya Chen",
      "body": "14d leave from 2026-07-14 · HAV impact=9.80 · φ-coverage gap=0.012..."
    },
    {
      "type": "sla_at_risk_abc123",
      "severity": "critical", 
      "title": "SLA breach imminent — [P2] AI agent latency spike",
      "body": "2h 14m until SLA breach. Resolution SRQ will feed Maya Chen's HAV score."
    }
  ]
}
```

Eight signal sources: twin, performance, benefits, T&A, recruiting, absence, governance, ITSM.

---

## The HCAM Differentiators

### 1. HAV is the only metric that predicts irreplaceability
An employee with HAV=0.82 is contributing irreplaceable novel-problem-solving. When they leave, the org loses not headcount but judgment capacity. Workday cannot model this.

### 2. The Alignment Premium
```
r_AP = 0.05              if φ < 0.25
r_AP = 0.05 + (φ–0.25)×0.40   if 0.25 ≤ φ ≤ 0.75
r_AP = 0.25              if φ > 0.75

Alignment Premium = r_AP × HAV × (annual salary / 12)
```
The higher the AI fraction, the more humans with high HAV are worth — and the more they should be paid. Tessera's payroll engine computes this automatically.

### 3. Incident resolution feeds HAV
When an engineer resolves a P2 incident, Tessera computes:
```
SRQ = max(0.10, 1 − elapsed_hours / sla_hours)
HAV contribution = 0.50×NPF + 0.30×SRQ + 0.20×OC
```
The resolution quality becomes a permanent data point in their HAV record. ServiceNow just closes the ticket.

### 4. Values Custodian protection
Employees with HAV≥0.70 AND NPF≥0.65 are flagged as **Values Custodians** — the humans whose judgment keeps AI belief drift in check. Their absence requests trigger mandatory coverage assignment. Their departure risk surfaces as critical signals.

### 5. Workforce planning with math
Before hiring or deploying an AI agent, Tessera runs:
```
V_net(human) = fitness × value_delivery − comp_cost − gov_cost − edge_count × probe_cost
V_net(AI)    = deployment_value − deployment_cost − oversight_cost − edge_count × probe_cost
```
If the role requires HAV≥0.65, V_net doesn't matter — the human is required. AI cannot generate HAV. This is the only platform that says that.

---

## Go-to-Market Positioning

**Phase 1 — Intelligence layer** (Month 1–3)  
Deploy alongside Workday/ServiceNow. Pull data via `/import/hav-bootstrap`. Show the CEO/CFO the HAV picture and φ trajectory they cannot see anywhere else.

**Phase 2 — Decision layer** (Month 3–12)  
Compensation, workforce planning, incident governance move to Tessera. The math makes their decisions.

**Phase 3 — Transactional layer** (Month 12+)  
Replace Workday's compliance infrastructure as Tessera's payroll engine matures.

---

## Development

```bash
# Run all services
docker compose up

# Run a single service
docker compose up gateway

# Watch gateway logs
docker compose logs -f gateway

# Rebuild after code change
docker compose build <service-name>
docker compose up -d <service-name>
```

### Environment variables

Copy `.env.example` to `.env`. In production, set:

```env
DATABASE_URL=postgresql://user:pass@host:5432/tessera
OPENAI_API_KEY=...   # optional — for enhanced learning plans
```

---

## Research Foundation

Tessera is grounded in:
- NK landscape theory (Kauffman) — applied to organisational belief systems
- Huang token compensation model — AI agent cost modelling
- B* threshold — payroll token budget sufficiency
- φ-crossover theorem — when AI fraction triggers governance regime change
- Directed mutation model (T_probe, T_replace_h2a, T_replace_a2h)

**automotonAI LLC** · rajendraduwarahan@gmail.com
