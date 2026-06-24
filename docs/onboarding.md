# Tessera Customer Onboarding Guide

Get your organisation's HAV picture live in under 10 minutes.

---

## Overview

Tessera has three onboarding paths depending on where your data lives today:

| Path | Source | Time to live HAV |
|------|--------|-----------------|
| **A — Workday / BambooHR export** | CSV or API pull | ~5 minutes |
| **B — Manual quick import** | UI form | ~3 minutes |
| **C — Bulk JSON paste** | Raw JSON | ~2 minutes |
| **D — Fresh start** | Demo seed only | ~30 seconds |

All paths end at the same place: a live HAV dashboard, a calibrated digital twin, and a signal feed surfacing risks Workday cannot see.

---

## Path A — Workday / BambooHR Import

### Step 1 — Export your data

**From Workday:**
1. Reports → Custom Reports → New Report
2. Fields: Worker ID, Full Name, Job Title, Department, Annual Salary
3. Add performance score columns if available (map to NPF below)
4. Export as JSON or CSV

**From BambooHR:**
1. Reports → New Report → Employees
2. Include: Employee ID, Name, Job Title, Department, Compensation
3. Export as CSV

### Step 2 — Map to Tessera format

Tessera needs HAV components (NPF, SRQ, OC) per employee per period. If your system doesn't have these directly, use these mappings:

| Your data | Tessera field | Notes |
|-----------|--------------|-------|
| Performance rating 1–5 | NPF | Divide by 5. Rating 4 → NPF=0.80 |
| Ticket resolution rate | SRQ | Already 0–1 if from ServiceNow |
| Engagement score | OC | Divide by 100 if percentage |
| No performance data | NPF=0.55, SRQ=0.50, OC=0.40 | Conservative defaults |

### Step 3 — POST to the bootstrap endpoint

```bash
curl -X POST https://your-tessera.com/import/hav-bootstrap \
  -H "Content-Type: application/json" \
  -d @your_employees.json
```

**Payload shape:**
```json
{
  "org_id": "acme-corp",
  "phi_star": 0.32,
  "source": "workday",
  "employees": [
    {
      "employee_id": "WD-0042",
      "employee_name": "Jane Smith",
      "role": "Senior Product Manager",
      "department": "Product",
      "salary": 145000,
      "periods": [
        {
          "label": "2025-Q3",
          "npf": 0.72,
          "srq": 0.65,
          "oc": 0.58,
          "hours": 480
        },
        {
          "label": "2025-Q4",
          "npf": 0.75,
          "srq": 0.70,
          "oc": 0.60,
          "hours": 480
        },
        {
          "label": "2026-Q1",
          "npf": 0.78,
          "srq": 0.72,
          "oc": 0.62,
          "hours": 480
        }
      ]
    }
  ]
}
```

**What the endpoint does:**
1. Registers each employee in the People registry
2. Converts each period → a completed T&A session with full HAV scoring
3. Runs the HAV aggregation pipeline across all imported sessions
4. Creates performance reviews with HAV trend data
5. Recalibrates the digital twin (updates φ and φ* for your org)
6. Returns a summary with Values Custodians identified

**Response:**
```json
{
  "status": "ok",
  "org_id": "acme-corp",
  "source": "workday",
  "employees_registered": 47,
  "imported_sessions": 141,
  "aggregate_result": {
    "employees_updated": 47,
    "mean_hav": 0.634,
    "values_custodians": 8
  },
  "twin_recalibrated": true,
  "twin_phi": 0.087,
  "twin_phi_star": 0.32,
  "twin_crossover": false
}
```

### Step 4 — Open the dashboard

Navigate to `http://your-tessera.com` and you'll see:

- **HAV Overview** — distribution across your org, Values Custodians highlighted
- **Signal Feed** — immediate alerts if any VCs are at risk, phi approaching crossover, or benefits gaps
- **Digital Twin** — your org's φ trajectory and governance stage
- **Performance** → Sync from T&A → reviews populated from your import

---

## Path B — Manual Quick Import (UI)

For small teams or first-look evaluation.

1. Open Tessera → click **Import Data** in the sidebar
2. Fill in the **Quick Import** form:
   - Org name and source label
   - One row per employee: name, role, department, salary
   - Add period rows: label, NPF, SRQ, OC, hours
3. Click **Import + Bootstrap** — the system runs the full pipeline automatically
4. Results appear inline: employees registered, sessions imported, twin calibrated

Best for: teams under 50 people, or trying Tessera before a full data migration.

---

## Path C — Bulk JSON Paste (UI)

1. Open Tessera → **Import Data** → **Bulk JSON** tab
2. Paste your full JSON payload directly (same format as Path A)
3. Click **Run Import**

Best for: developers evaluating the API before building an integration.

---

## Path D — Demo Seed

Seeds synthetic data for 10 employees across 4 departments. Includes 60 T&A sessions, 5 ITSM incidents, 4 CMDB items, and full performance history.

```
GET /demo/seed
```

Or click **Seed Demo Data** on the HAV Overview dashboard.

Best for: internal demos, POC evaluations, new team member onboarding to Tessera itself.

---

## What happens after import

### The HAV pipeline (automatic)

```
Imported sessions
       ↓
aggregate-hav (POST /aggregate-hav)
       ↓
Mean HAV / NPF / SRQ / OC per employee
       ↓
Performance reviews created / updated
       ↓
Values Custodians identified (HAV≥0.70 AND NPF≥0.65)
       ↓
Twin recalibrated → new φ, φ*, stage
       ↓
Signal feed updated → critical alerts surface immediately
```

This runs automatically after every import. You can also trigger it manually:

```bash
POST /aggregate-hav
{"org_id": "your-org"}
```

### Understanding your first signal feed

After import, check `GET /signals`. Typical first-day signals:

| Signal | What it means | Action |
|--------|--------------|--------|
| `vc_at_risk` | A Values Custodian's benefits are inadequate | Review benefits for this employee immediately |
| `phi_approaching_crossover` | AI fraction within 15% of φ* | Pause AI deployments; audit HAV of remaining humans |
| `hav_measured_from_sessions` | T&A data processed successfully | Informational — your baseline is established |
| `wrong_measurement_regime` | Org still reporting in man-hours | Switch to HAV regime in Governance settings |

---

## Connecting ongoing data sources

### Continuous T&A session import

For real-time HAV measurement, integrate session completion into your workflow tools:

```bash
POST /api/v1/time-attendance/sessions/import
{
  "sessions": [
    {
      "employee_id": "EMP-001",
      "org_id": "your-org",
      "checkin_at": "2026-06-24T09:00:00Z",
      "checkout_at": "2026-06-24T17:00:00Z",
      "actual_npf": 0.72,
      "srq_score": 0.68,
      "oc_score": 0.55,
      "task_type": "project_work",
      "source": "jira-integration"
    }
  ]
}
```

### ITSM incident resolution → SRQ

When a ticket resolves in your existing system, POST to:

```bash
POST /api/v1/time-attendance/sessions/import
{
  "sessions": [{
    "employee_id": "EMP-007",
    "actual_npf": 0.65,
    "srq_score": 0.88,        ← computed from: 1 - elapsed_hours/sla_hours
    "task_type": "srq_resolution",
    "source": "servicenow:INC0001234"
  }]
}
```

This is the bridge ServiceNow cannot build — every ticket resolution becomes a HAV data point.

### Workday webhook → Tessera

Set up a Workday Studio integration to POST performance events to Tessera:

```
Workday performance review completed
   → POST /api/v1/time-attendance/sessions/import
   → Tessera recomputes HAV
   → Signal feed updates within seconds
```

---

## Key concepts for your first week

### Values Custodians
Employees with HAV≥0.70 AND NPF≥0.65. These are the people whose judgment prevents AI belief drift. Tessera surfaces them, protects them, and alerts you when they're at risk. Never let one leave without understanding what knowledge they carry.

### φ (phi)
Your organisation's AI fraction — what percentage of your capital units are AI agents vs humans. As φ rises, standard management models break. When φ exceeds φ* (your crossover threshold), you enter the HAV governance regime where alignment premiums must rise.

### Alignment Premium
```
r_AP × HAV × (monthly salary)
```
The higher your φ, the higher r_AP, the more valuable high-HAV humans are — and the more they should earn. Tessera's payroll view computes this automatically once your twin is calibrated.

### The digital twin
A live NK-landscape simulation of your organisation's belief system. It tells you:
- Current φ and φ* (your crossover threshold)
- Stage 0/1/2 governance health
- Belief drift rate
- How many epochs until crossover at current trajectory

Check it weekly. If stage rises to 2, intervene immediately.

---

## Support

- **Documentation:** [docs/](./docs/)
- **API Reference:** [docs/api-reference.md](./api-reference.md)
- **Email:** rajendraduwarahan@gmail.com
- **GitHub Issues:** [github.com/automotonai/tessera/issues](https://github.com/automotonai/tessera/issues)
