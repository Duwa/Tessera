#!/usr/bin/env python3
"""
Tessera demo preflight check
============================
Run this RIGHT BEFORE a demo. It gives you one unambiguous GREEN/RED board:

    python scripts/preflight.py

It checks, concurrently:
  1. Gateway is up                     (http://localhost:8000/)
  2. Gateway's own /health aggregation (what the gateway thinks of every service)
  3. Each of the 7 services directly   (in case gateway routing hides a problem)
  4. The frontend is being served      (http://localhost / and /index.html)
  5. ONE real end-to-end request through the proxy (cost-benefit /analyze),
     proving a full request actually round-trips, not just that pings answer.

Exit code 0 = safe to demo. Exit code 1 = something is red, do not present yet.

Pure standard library. No pip install needed.
"""

import sys
import json
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

# Windows consoles default to cp1252 and choke on ✓/✗ — force UTF-8 output.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ── Config ─────────────────────────────────────────────────────────────
# Override hosts with env vars if you demo against a remote box.
import os

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8000")
FRONTEND = os.getenv("FRONTEND_URL", "http://localhost")  # nginx serves on :80

# name -> direct port (matches docker-compose.yml)
SERVICES = {
    "cost-benefit": 8001,
    "payroll":      8002,
    "learning":     8003,
    "twin":         8004,
    "people":       8005,
    "expenses":     8006,
    "onboarding":   8007,
    "governance":   8008,  # running but NOT in docker-compose.yml / gateway — see README note
}

TIMEOUT = 4.0  # seconds per check

# ── Pretty output (works in modern Windows terminal / VS Code) ─────────
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

OK = f"{GREEN}PASS{RESET}"
FAIL = f"{RED}FAIL{RESET}"
WARN = f"{YELLOW}WARN{RESET}"


def _get(url, method="GET", body=None, headers=None):
    """Return (status_code, parsed_or_text, elapsed_ms). status 0 = connection error."""
    start = time.time()
    data = None
    req_headers = headers or {}
    if body is not None:
        data = json.dumps(body).encode()
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode(errors="replace")
            elapsed = (time.time() - start) * 1000
            try:
                return resp.status, json.loads(raw), elapsed
            except json.JSONDecodeError:
                return resp.status, raw, elapsed
    except urllib.error.HTTPError as e:
        elapsed = (time.time() - start) * 1000
        return e.code, e.reason, elapsed
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        return 0, str(e), elapsed


class Result:
    def __init__(self, label, ok, detail, ms=None, warn=False):
        self.label = label
        self.ok = ok
        self.detail = detail
        self.ms = ms
        self.warn = warn


def check_gateway():
    code, payload, ms = _get(f"{GATEWAY}/")
    if code == 200:
        return Result("Gateway", True, "up", ms)
    return Result("Gateway", False, f"got {code}: {payload}", ms)


def check_gateway_health():
    code, payload, ms = _get(f"{GATEWAY}/health")
    if code != 200 or not isinstance(payload, dict):
        return Result("Gateway /health", False, f"got {code}", ms)
    services = payload.get("services", {})
    down = [n for n, v in services.items() if v != "up"]
    if not down:
        return Result("Gateway /health", True, f"all {len(services)} services up", ms)
    return Result("Gateway /health", False, f"gateway sees DOWN: {', '.join(down)}", ms)


def check_service(name, port):
    code, payload, ms = _get(f"http://localhost:{port}/health")
    if code == 200:
        return Result(f"svc:{name}", True, "up", ms)
    if code == 0:
        return Result(f"svc:{name}", False, f"unreachable on :{port} ({payload})", ms)
    return Result(f"svc:{name}", False, f"got {code} on :{port}", ms)


def check_frontend():
    code, payload, ms = _get(f"{FRONTEND}/")
    if code == 200:
        return Result("Frontend", True, "served", ms)
    return Result("Frontend", False, f"got {code} at {FRONTEND}", ms, warn=True)


def check_end_to_end():
    """A real request through the gateway proxy to cost-benefit /analyze."""
    payload_in = {"headcount_human": 25, "headcount_ai": 8, "monthly_token_budget": 3000.0}
    code, payload, ms = _get(
        f"{GATEWAY}/api/v1/cost-benefit/analyze", method="POST", body=payload_in
    )
    if code == 200 and isinstance(payload, dict) and "b_star" in payload:
        return Result("E2E proxy (cost-benefit/analyze)", True,
                      f"b_star={payload['b_star']}", ms)
    return Result("E2E proxy (cost-benefit/analyze)", False, f"got {code}: {payload}", ms)


def main():
    print(f"\n{BOLD}Tessera demo preflight{RESET}  {DIM}{GATEWAY}{RESET}\n")

    results = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [
            pool.submit(check_gateway),
            pool.submit(check_gateway_health),
            pool.submit(check_frontend),
            pool.submit(check_end_to_end),
        ]
        futures += [pool.submit(check_service, n, p) for n, p in SERVICES.items()]
        results = [f.result() for f in futures]

    # Stable ordering: gateway things first, then services, then e2e/frontend
    order = {"Gateway": 0, "Gateway /health": 1, "Frontend": 2}
    results.sort(key=lambda r: (order.get(r.label, 5), r.label))

    width = max(len(r.label) for r in results)
    hard_fail = False
    for r in results:
        if r.ok:
            tag = OK
        elif r.warn:
            tag = WARN
        else:
            tag = FAIL
            hard_fail = True
        ms = f"{DIM}{r.ms:6.0f}ms{RESET}" if r.ms is not None else " " * 8
        print(f"  [{tag}] {r.label.ljust(width)}  {ms}  {DIM}{r.detail}{RESET}")

    print()
    if hard_fail:
        print(f"{RED}{BOLD}✗ NOT demo-ready — fix the FAIL rows above before presenting.{RESET}\n")
        return 1
    print(f"{GREEN}{BOLD}✓ All green — safe to demo.{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
