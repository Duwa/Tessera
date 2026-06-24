# Tessera demo rehearsal (Playwright)

End-to-end "rehearsal" of the exact path you'll show in a demo. It fails loudly if
anything that would embarrass you on stage is broken: a page that won't render, a JS
error on load, a backend call that 4xx/5xx's, or a service round-trip that silently
falls back to "Backend not running".

## Prerequisites
The stack must already be running. Verify first with the fast preflight:

```bash
python ../scripts/preflight.py      # all green = safe to rehearse
```

## One-time setup
```bash
cd tests
npm install
npx playwright install chromium
```

## Run
```bash
npm run rehearse           # headless, ~35s
npm run rehearse:headed    # watch it drive the browser
npm run report             # open the HTML report (screenshots + traces of every step)
```

Override the target host (defaults to nginx on http://localhost):
```bash
BASE_URL=http://some-host npx playwright test
```

## What it checks (`e2e/demo-rehearsal.spec.js`)
1. **Home loads cleanly** — no uncaught JS errors on first paint.
2. **Every nav page renders** — walks all 19 pages, screenshots each, asserts no JS
   errors and no failed backend calls during the walk.
3. **Cost-benefit** — fills the "what" field on the `solo` page, clicks "Build my agent
   stack", asserts a recommendation renders. (Note: that field is required — an empty
   field makes the button do nothing.)
4. **Payroll** — clicks "Run org payroll", asserts it round-trips `POST /api/v1/payroll/run`
   through the gateway (status 200) and does NOT show "Backend not running".
5. **Governance** — opening "earlywarning" fetches the AI Governance dashboard from the
   governance service (:8008); asserts the fetch succeeds.

## Debugging a failure
Every run records a trace. After a failure:
```bash
npm run report
```
or open a specific trace: `npx playwright show-trace test-results/<...>/trace.zip`.
You get a DOM snapshot timeline of exactly what happened, step by step.

## Note on deploying frontend fixes
The frontend is baked into the nginx image at build time. During development you can push
a changed `frontend/index.html` into the running container without a rebuild:
```bash
docker cp ../frontend/index.html tessera_project-nginx-1:/usr/share/nginx/html/index.html
```
A real `docker compose build nginx` picks it up permanently from source.
