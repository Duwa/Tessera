# Tessera — Developer Onboarding

Welcome. Tessera is a HCAM platform (Human and AI Capital Management). Read README.md first — specifically the φ-crossover theorem and HAV formula — before writing a single line of code. The formulas aren't decoration; they drive real product decisions.

---

## Day 1: Get it running

```powershell
git clone https://github.com/automotonai/tessera
cd tessera
cp .env.example .env
docker compose up --build
```

Wait for all 27 services to report healthy (~2–3 min on first build), then:

- **Platform UI:** http://localhost
- **API Docs:** http://localhost:8000/docs
- **Seed demo data:** http://localhost/demo/seed (run this first so the dashboard isn't empty)

If a service fails to start, check its logs: `docker compose logs -f <service-name>`

---

## The architecture in 90 seconds

```
Browser
  → nginx:80
      → /api/v1/*   → gateway:8000   → 27 microservices
      → /           → frontend/platform.html (single-file SPA)
      → /demo/seed  → gateway:8000   (seeds demo data across all services)
      → /signals    → gateway:8000   (aggregates signals from all services)
```

The **gateway** (`gateway/main.py`) is the hub. It:
- Proxies `/api/v1/<service>/...` to the right microservice
- Runs the `/demo/seed` orchestration (calls every service in order)
- Aggregates signals from 8+ services for the signal feed
- Runs HAV aggregation via `/aggregate-hav`

The **frontend** (`frontend/platform.html`) is a single HTML file (~4k lines). All views are rendered in JS. No build step. Edit and refresh.

---

## Codebase map

```
tessera/
├── gateway/main.py           ← Start here. The nerve centre.
├── frontend/platform.html    ← The entire UI in one file.
├── services/
│   ├── twin/main.py          ← NK landscape digital twin, φ tracking
│   ├── people/main.py        ← Human + AI capital registry
│   ├── time_attendance/      ← HAV measurement from work sessions
│   ├── performance/          ← HAV-based review cycles
│   ├── itsm/main.py          ← Incidents, Changes, Problems, CMDB
│   ├── workforce_planning/   ← hire_human vs deploy_ai (V_net model)
│   ├── agent_factory/main.py ← AI agent lifecycle registry
│   └── ...24 more services
├── docs/
│   ├── onboarding.md         ← Customer onboarding guide
│   ├── api-reference.md      ← All API endpoints
│   └── developer-onboarding.md  ← This file
└── docker-compose.yml        ← All 27 services + postgres + redis + nginx
```

---

## Key concepts you must understand before committing code

### HAV (Human Alignment Value)
```
HAV(h, session) = 0.50 × NPF + 0.30 × SRQ + 0.20 × OC
```
Every HR action, incident resolution, and workforce planning decision flows through this. If you're touching performance, compensation, recruiting, absence, or payroll — understand HAV first.

### Values Custodians
`HAV ≥ 0.70 AND NPF ≥ 0.65` = Values Custodian. These are the humans whose judgment prevents AI belief drift. The platform gives them special treatment in every flow. Never break VC protection logic.

### φ (AI fraction)
`φ = AI_agents / (AI_agents + humans)`. When φ > φ* (crossover threshold), governance stage escalates. Every time you add or retire an AI agent, φ changes. Recalculate it.

### φ* crossover thresholds
```
K ≥ 6  →  φ* = 0.25
K = 4  →  φ* = 0.32   ← default for most orgs
K ≤ 2  →  φ* = 0.44
```

### The ITSM → SRQ → HAV bridge
When an engineer resolves a ticket: `SRQ = max(0.10, 1 − elapsed_hours / sla_hours)`. The ITSM service's SRQ feeds into HAV measurement via a T&A session import. This is Tessera's core differentiator vs ServiceNow. Don't break this chain.

---

## Adding a new service

1. Copy an existing service directory as a template: `cp -r services/absence services/my-service`
2. Change the port in `main.py` (next available: check docker-compose.yml)
3. Add to `docker-compose.yml` with a new DATABASE_URL:
```yaml
my-service:
  build: ./services/my-service
  ports: ["8028:8028"]
  environment:
    DATABASE_URL: postgresql://tessera:tessera@postgres:5432/tessera_myservice
  depends_on:
    postgres:
      condition: service_healthy
  networks: [tessera-net]
  restart: unless-stopped
```
4. Register in gateway's SERVICES dict (`gateway/main.py` ~line 44)
5. Add gateway proxy route: `@app.api_route("/api/v1/my-service/{path:path}", ...)`
6. Add a nav link + `loadMyService()` function in `frontend/platform.html`

---

## Working with the frontend

The frontend is a single HTML file with no build step. Open `frontend/platform.html`, find the `loadView()` switch, and add your case. Each view function follows the pattern:

```javascript
async function loadMyView() {
  const main = document.getElementById('main-content');
  main.innerHTML = '<div>Loading…</div>';
  const data = await GET('/my-service/endpoint');
  main.innerHTML = `<div class="view active" ...>
    <!-- build your HTML here -->
  </div>`;
}
```

Helper functions available in every view:
- `GET(path)` — prepends `/api/v1`, returns parsed JSON
- `api(path, opts)` — raw fetch with error handling
- `toast(msg)` — bottom-right notification
- `fmtHAV(v)` — formats a HAV score with colour coding
- `PRIO_COLOR`, `PRIO_BG` — ITSM priority colours

---

## Running tests

```powershell
cd tests
npm install
npx playwright test
```

Or run a specific test file: `npx playwright test e2e/demo-rehearsal.spec.js`

---

## Common gotchas

| Problem | Cause | Fix |
|---------|-------|-----|
| ITSM 422 on org_id | ITSM uses `uuid.UUID()` validation | Use `"00000000-0000-0000-0000-000000000001"` not a string slug |
| `GET /plans` returns 405 | Service only had POST | Add the GET handler to the service |
| Signal feed empty | Demo data not seeded | Hit `/demo/seed` first |
| φ not updating | Twin not recalibrated | `POST /api/v1/twin/orgs/{id}/calibrate?epochs=5` |
| HAV reviews missing | T&A sessions not aggregated | `POST /aggregate-hav` with org_id |

---

## Your first contribution

1. Read `gateway/main.py` top-to-bottom. You'll understand the entire platform.
2. Seed the demo data: `GET /demo/seed`
3. Open the platform at http://localhost, explore every view
4. Pick one signal from the signal feed and trace its path: which service generates it, what triggers it, how it surfaces in the UI
5. Read `docs/api-reference.md` for the full endpoint catalogue

Questions: rajendraduwarahan@gmail.com
