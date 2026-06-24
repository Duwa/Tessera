"""
Tessera Deployment Service — port 8028
=======================================
Provisions Azure infrastructure and deploys AI agent containers.

Routing logic (based on agent daily_runs):
  daily_runs < 100  →  ACI (Azure Container Instances) — one-shot job
  daily_runs >= 100 →  AKS (Azure Kubernetes Service) — Deployment or CronJob

Provisioning order (idempotent — safe to call repeatedly):
  1. Resource Group
  2. Azure Container Registry (ACR)
  3. AKS cluster (only when agent needs AKS)
  4. Build Docker image from generated code
  5. Push image to ACR
  6. Deploy container to ACI or AKS

All long-running operations run as asyncio background tasks.
Poll GET /deployments/{id} for live status.
"""
from __future__ import annotations
import os, uuid, asyncio, json, asyncpg, textwrap
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

DATABASE_URL     = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@localhost:5432/tessera_deployment")
AGENT_FACTORY_URL = os.getenv("AGENT_FACTORY_URL", "http://agent-factory:8011")

# Azure config — set in .env
AZURE_SUBSCRIPTION_ID   = os.getenv("AZURE_SUBSCRIPTION_ID", "")
AZURE_TENANT_ID         = os.getenv("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID         = os.getenv("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET     = os.getenv("AZURE_CLIENT_SECRET", "")
AZURE_RESOURCE_GROUP    = os.getenv("AZURE_RESOURCE_GROUP", "tessera-rg")
AZURE_LOCATION          = os.getenv("AZURE_LOCATION", "eastus")
AZURE_ACR_NAME          = os.getenv("AZURE_ACR_NAME", "tesseraacr")
AZURE_AKS_CLUSTER       = os.getenv("AZURE_AKS_CLUSTER", "tessera-aks")

# Threshold: agents with >= this daily_runs go to AKS; below goes to ACI
AKS_THRESHOLD = 100

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS deployments (
    id                   TEXT PRIMARY KEY,
    agent_id             TEXT NOT NULL,
    org_id               TEXT NOT NULL,
    agent_name           TEXT,
    department           TEXT,
    pipeline             TEXT,
    framework            TEXT,
    compute_target       TEXT,   -- aci | aks
    status               TEXT DEFAULT 'queued',
    -- queued | provisioning | building | pushing | deploying | running | failed | stopped
    status_detail        TEXT,
    image_tag            TEXT,
    azure_resource_group TEXT,
    azure_acr_login      TEXT,
    azure_resource_name  TEXT,   -- ACI container group or AKS deployment name
    k8s_manifest         TEXT,
    generated_code       TEXT,
    dockerfile           TEXT,
    logs                 TEXT DEFAULT '',
    error                TEXT,
    daily_runs           INT DEFAULT 0,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    updated_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dep_agent ON deployments(agent_id);
CREATE INDEX IF NOT EXISTS idx_dep_org   ON deployments(org_id);
CREATE INDEX IF NOT EXISTS idx_dep_status ON deployments(status);

CREATE TABLE IF NOT EXISTS azure_infra (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL UNIQUE,
    resource_group  TEXT,
    acr_name        TEXT,
    acr_login_server TEXT,
    aks_cluster     TEXT,
    provisioned_at  TIMESTAMPTZ DEFAULT NOW(),
    status          TEXT DEFAULT 'pending'
);
"""

db: asyncpg.Pool | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(CREATE_TABLES)
    yield
    await db.close()

app = FastAPI(title="Tessera Deployment Service", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ─── Models ───────────────────────────────────────────────────────────────────

class DeployRequest(BaseModel):
    agent_id: str
    org_id: str
    simulate: bool = False   # if True, runs mock provisioning without real Azure calls


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ser(row) -> dict:
    d = dict(row)
    for k, v in d.items():
        if hasattr(v, 'isoformat'):
            d[k] = v.isoformat()
    return d

def _azure_configured() -> bool:
    return all([AZURE_SUBSCRIPTION_ID, AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET])

async def _log(dep_id: str, msg: str):
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE deployments SET logs = logs || $2, updated_at=NOW() WHERE id=$1",
            dep_id, f"\n[{__import__('datetime').datetime.utcnow().strftime('%H:%M:%S')}] {msg}"
        )

async def _set_status(dep_id: str, status: str, detail: str = "", error: str = ""):
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE deployments SET status=$2, status_detail=$3, error=$4, updated_at=NOW() WHERE id=$1",
            dep_id, status, detail, error or None
        )


# ─── Azure provisioning ───────────────────────────────────────────────────────

async def _provision_azure(dep_id: str, org_id: str, simulate: bool):
    """Idempotently provision: Resource Group → ACR → (optionally) AKS."""
    try:
        from azure.identity import ClientSecretCredential
        from azure.mgmt.resource import ResourceManagementClient
        from azure.mgmt.containerregistry import ContainerRegistryManagementClient
        from azure.mgmt.containerregistry.models import Registry, Sku
        from azure.mgmt.containerservice import ContainerServiceClient
        from azure.mgmt.containerservice.models import (
            ManagedCluster, ManagedClusterAgentPoolProfile, ManagedClusterServicePrincipalProfile
        )
        cred = ClientSecretCredential(AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET)
    except ImportError:
        await _log(dep_id, "azure-mgmt packages not installed — running in simulate mode")
        simulate = True

    if simulate:
        await _log(dep_id, "SIMULATE: Resource Group 'tessera-rg' OK")
        await asyncio.sleep(0.5)
        await _log(dep_id, "SIMULATE: ACR 'tesseraacr.azurecr.io' OK")
        await asyncio.sleep(0.5)
        async with db.acquire() as conn:
            await conn.execute("""
                INSERT INTO azure_infra (id, org_id, resource_group, acr_name, acr_login_server, aks_cluster, status)
                VALUES ($1,$2,$3,$4,$5,$6,'ready')
                ON CONFLICT (org_id) DO UPDATE SET status='ready', acr_login_server=$5
            """, str(uuid.uuid4()), org_id, AZURE_RESOURCE_GROUP,
                 AZURE_ACR_NAME, f"{AZURE_ACR_NAME}.azurecr.io", AZURE_AKS_CLUSTER)
        return f"{AZURE_ACR_NAME}.azurecr.io"

    # 1. Resource Group
    await _log(dep_id, f"Ensuring Resource Group '{AZURE_RESOURCE_GROUP}' in {AZURE_LOCATION}…")
    rmc = ResourceManagementClient(cred, AZURE_SUBSCRIPTION_ID)
    rmc.resource_groups.create_or_update(AZURE_RESOURCE_GROUP, {"location": AZURE_LOCATION})
    await _log(dep_id, "Resource Group ready.")

    # 2. ACR
    await _log(dep_id, f"Ensuring ACR '{AZURE_ACR_NAME}'…")
    acr_client = ContainerRegistryManagementClient(cred, AZURE_SUBSCRIPTION_ID)
    acr = acr_client.registries.begin_create(
        AZURE_RESOURCE_GROUP, AZURE_ACR_NAME,
        Registry(location=AZURE_LOCATION, sku=Sku(name="Basic"), admin_user_enabled=True)
    ).result()
    login_server = acr.login_server
    await _log(dep_id, f"ACR ready: {login_server}")

    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO azure_infra (id, org_id, resource_group, acr_name, acr_login_server, aks_cluster, status)
            VALUES ($1,$2,$3,$4,$5,$6,'ready')
            ON CONFLICT (org_id) DO UPDATE SET status='ready', acr_login_server=$5
        """, str(uuid.uuid4()), org_id, AZURE_RESOURCE_GROUP,
             AZURE_ACR_NAME, login_server, AZURE_AKS_CLUSTER)
    return login_server


async def _provision_aks(dep_id: str, login_server: str, simulate: bool):
    """Provision AKS cluster (only when needed for high-frequency agents)."""
    if simulate:
        await _log(dep_id, f"SIMULATE: AKS cluster '{AZURE_AKS_CLUSTER}' ready (2 nodes, Standard_D2s_v3)")
        await asyncio.sleep(1.0)
        return True

    try:
        from azure.identity import ClientSecretCredential
        from azure.mgmt.containerservice import ContainerServiceClient
        from azure.mgmt.containerservice.models import (
            ManagedCluster, ManagedClusterAgentPoolProfile, ManagedClusterServicePrincipalProfile
        )
        cred = ClientSecretCredential(AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET)
    except ImportError:
        await _log(dep_id, "Skipping AKS provision (azure-mgmt-containerservice not installed)")
        return False

    await _log(dep_id, f"Provisioning AKS cluster '{AZURE_AKS_CLUSTER}' (this takes ~5 min)…")
    aks_client = ContainerServiceClient(cred, AZURE_SUBSCRIPTION_ID)
    aks_client.managed_clusters.begin_create_or_update(
        AZURE_RESOURCE_GROUP, AZURE_AKS_CLUSTER,
        ManagedCluster(
            location=AZURE_LOCATION,
            dns_prefix=f"{AZURE_AKS_CLUSTER}-dns",
            agent_pool_profiles=[ManagedClusterAgentPoolProfile(
                name="agentpool", count=2, vm_size="Standard_D2s_v3",
                os_disk_size_gb=30, mode="System"
            )],
            service_principal_profile=ManagedClusterServicePrincipalProfile(
                client_id=AZURE_CLIENT_ID, secret=AZURE_CLIENT_SECRET
            )
        )
    ).result()
    await _log(dep_id, "AKS cluster ready.")
    return True


async def _build_and_push(dep_id: str, image_tag: str, code: str, dockerfile: str,
                          login_server: str, simulate: bool):
    """Build Docker image from generated code and push to ACR."""
    if simulate:
        await _log(dep_id, f"SIMULATE: Building Docker image {image_tag}…")
        await asyncio.sleep(1.0)
        await _log(dep_id, f"SIMULATE: Pushing {image_tag} to {login_server}…")
        await asyncio.sleep(0.8)
        await _log(dep_id, "Image push complete (simulated).")
        return True

    import tempfile, pathlib
    try:
        import docker as docker_sdk
    except ImportError:
        await _log(dep_id, "docker-py not installed — cannot build image. Install: pip install docker")
        return False

    with tempfile.TemporaryDirectory() as tmpdir:
        p = pathlib.Path(tmpdir)
        (p / "agent.py").write_text(code)
        (p / "Dockerfile").write_text(dockerfile)
        (p / "tessera_sdk.py").write_text(_tessera_sdk_stub())

        await _log(dep_id, f"Building Docker image {image_tag}…")
        client = docker_sdk.from_env()
        client.images.build(path=tmpdir, tag=image_tag, rm=True)
        await _log(dep_id, "Build complete. Authenticating with ACR…")

        try:
            from azure.identity import ClientSecretCredential
            from azure.mgmt.containerregistry import ContainerRegistryManagementClient
            cred = ClientSecretCredential(AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET)
            acr_client = ContainerRegistryManagementClient(cred, AZURE_SUBSCRIPTION_ID)
            creds = acr_client.registries.list_credentials(AZURE_RESOURCE_GROUP, AZURE_ACR_NAME)
            acr_user = creds.username
            acr_pass = creds.passwords[0].value
            client.login(registry=login_server, username=acr_user, password=acr_pass)
        except Exception as e:
            await _log(dep_id, f"ACR auth failed: {e}")
            return False

        await _log(dep_id, f"Pushing {image_tag}…")
        full_tag = f"{login_server}/{image_tag}"
        client.images.get(image_tag).tag(full_tag)
        client.images.push(full_tag)
        await _log(dep_id, "Image pushed to ACR.")
        return True


async def _deploy_aci(dep_id: str, agent_name: str, image_tag: str, login_server: str,
                      department: str, pipeline: str, simulate: bool):
    """Deploy agent as an ACI container group (one-shot job pattern)."""
    container_name = f"tessera-{agent_name.lower()}-{dep_id[:8]}"
    if simulate:
        await _log(dep_id, f"SIMULATE: Creating ACI container group '{container_name}'…")
        await asyncio.sleep(1.0)
        await _log(dep_id, f"SIMULATE: Container running at ACI. Pipeline: {department}/{pipeline}")
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE deployments SET azure_resource_name=$2, compute_target='aci', updated_at=NOW() WHERE id=$1",
                dep_id, container_name
            )
        return container_name

    try:
        from azure.identity import ClientSecretCredential
        from azure.mgmt.containerinstance import ContainerInstanceManagementClient
        from azure.mgmt.containerinstance.models import (
            ContainerGroup, Container, ContainerPort, ResourceRequests,
            ResourceRequirements, ImageRegistryCredential, OperatingSystemTypes,
            ContainerGroupRestartPolicy
        )
        cred = ClientSecretCredential(AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET)
    except ImportError:
        await _log(dep_id, "azure-mgmt-containerinstance not installed")
        return None

    await _log(dep_id, f"Deploying to ACI as '{container_name}'…")
    aci_client = ContainerInstanceManagementClient(cred, AZURE_SUBSCRIPTION_ID)

    try:
        from azure.mgmt.containerregistry import ContainerRegistryManagementClient
        acr_client = ContainerRegistryManagementClient(cred, AZURE_SUBSCRIPTION_ID)
        creds = acr_client.registries.list_credentials(AZURE_RESOURCE_GROUP, AZURE_ACR_NAME)
        acr_user = creds.username
        acr_pass = creds.passwords[0].value
    except Exception:
        acr_user, acr_pass = "", ""

    aci_client.container_groups.begin_create_or_update(
        AZURE_RESOURCE_GROUP, container_name,
        ContainerGroup(
            location=AZURE_LOCATION,
            os_type=OperatingSystemTypes.LINUX,
            restart_policy=ContainerGroupRestartPolicy.NEVER,
            image_registry_credentials=[ImageRegistryCredential(
                server=login_server, username=acr_user, password=acr_pass
            )],
            containers=[Container(
                name=container_name,
                image=f"{login_server}/{image_tag}",
                resources=ResourceRequirements(
                    requests=ResourceRequests(cpu=1.0, memory_in_gb=1.5)
                ),
                environment_variables=[
                    {"name": "TESSERA_AGENT_NAME", "value": agent_name},
                    {"name": "TESSERA_DEPARTMENT", "value": department},
                    {"name": "TESSERA_PIPELINE",   "value": pipeline},
                ]
            )]
        )
    ).result()
    await _log(dep_id, f"ACI container group '{container_name}' running.")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE deployments SET azure_resource_name=$2, compute_target='aci', updated_at=NOW() WHERE id=$1",
            dep_id, container_name
        )
    return container_name


async def _deploy_aks(dep_id: str, agent_name: str, image_tag: str, login_server: str,
                      manifest: str, simulate: bool):
    """Apply K8s manifest to AKS cluster."""
    if simulate:
        await _log(dep_id, f"SIMULATE: Applying K8s manifest to AKS '{AZURE_AKS_CLUSTER}'…")
        await asyncio.sleep(1.2)
        await _log(dep_id, "SIMULATE: Deployment/CronJob applied. Pods starting.")
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE deployments SET azure_resource_name=$2, compute_target='aks', updated_at=NOW() WHERE id=$1",
                dep_id, f"tessera-{agent_name.lower()}"
            )
        return True

    try:
        from kubernetes import client as k8s_client, config as k8s_config
        import yaml, tempfile, pathlib

        # Pull kubeconfig from AKS
        from azure.identity import ClientSecretCredential
        from azure.mgmt.containerservice import ContainerServiceClient
        cred = ClientSecretCredential(AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET)
        aks_client = ContainerServiceClient(cred, AZURE_SUBSCRIPTION_ID)
        kube_config = aks_client.managed_clusters.list_cluster_user_credentials(
            AZURE_RESOURCE_GROUP, AZURE_AKS_CLUSTER
        )
        kube_yaml = kube_config.kubeconfigs[0].value.decode("utf-8")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(kube_yaml)
            kubeconfig_path = f.name

        k8s_config.load_kube_config(kubeconfig_path)
        k8s_yaml = yaml.safe_load(manifest)
        kind = k8s_yaml.get("kind", "")

        if kind == "Deployment":
            k8s_client.AppsV1Api().create_namespaced_deployment(
                namespace="default", body=k8s_yaml
            )
        elif kind == "CronJob":
            k8s_client.BatchV1Api().create_namespaced_cron_job(
                namespace="default", body=k8s_yaml
            )
        await _log(dep_id, f"AKS {kind} applied.")
    except ImportError:
        await _log(dep_id, "kubernetes or azure packages not installed — AKS deploy skipped")
        return False
    return True


# ─── Code generation helpers ──────────────────────────────────────────────────

def _tessera_sdk_stub() -> str:
    return textwrap.dedent("""\
        \"\"\"Tessera SDK stub for agent tracing.\"\"\"
        import os, httpx, uuid, contextlib
        from datetime import datetime

        TESSERA_URL = os.getenv("TESSERA_URL", "http://gateway:8000")

        class _Run:
            def __init__(self, name, branch): self.name=name; self.branch=branch; self.run_id=str(uuid.uuid4())
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def span(self, **kw): pass

        class TesseraTracer:
            def run(self, name, branch="value"): return _Run(name, branch)
    """)

def _dockerfile(framework: str) -> str:
    base_packages = {
        "langgraph": "langgraph langchain-anthropic",
        "crewai":    "crewai langchain-anthropic",
        "autogen":   "pyautogen anthropic",
        "custom":    "anthropic httpx",
    }.get(framework, "anthropic httpx")
    return textwrap.dedent(f"""\
        FROM python:3.11-slim
        WORKDIR /app
        RUN pip install --no-cache-dir fastapi uvicorn httpx {base_packages}
        COPY tessera_sdk.py .
        COPY agent.py .
        ENV PYTHONUNBUFFERED=1
        CMD ["python", "agent.py"]
    """)

def _k8s_manifest(agent_name: str, image: str, daily_runs: int,
                  department: str, pipeline: str) -> str:
    safe_name = agent_name.lower().replace("_", "-")
    if daily_runs >= AKS_THRESHOLD:
        # High-frequency: always-on Deployment
        return textwrap.dedent(f"""\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: tessera-{safe_name}
              labels:
                app: tessera-agent
                agent: {safe_name}
                department: {department}
            spec:
              replicas: 1
              selector:
                matchLabels:
                  app: tessera-{safe_name}
              template:
                metadata:
                  labels:
                    app: tessera-{safe_name}
                spec:
                  containers:
                  - name: {safe_name}
                    image: {image}
                    env:
                    - name: TESSERA_AGENT_NAME
                      value: "{agent_name}"
                    - name: TESSERA_DEPARTMENT
                      value: "{department}"
                    - name: TESSERA_PIPELINE
                      value: "{pipeline}"
                    resources:
                      requests:
                        cpu: "250m"
                        memory: "512Mi"
                      limits:
                        cpu: "500m"
                        memory: "1Gi"
                  restartPolicy: Always
        """)
    else:
        # Lower frequency: CronJob
        schedule = "*/15 * * * *" if daily_runs >= 20 else "0 */2 * * *"
        return textwrap.dedent(f"""\
            apiVersion: batch/v1
            kind: CronJob
            metadata:
              name: tessera-{safe_name}
              labels:
                app: tessera-agent
                agent: {safe_name}
                department: {department}
            spec:
              schedule: "{schedule}"
              concurrencyPolicy: Forbid
              jobTemplate:
                spec:
                  template:
                    spec:
                      containers:
                      - name: {safe_name}
                        image: {image}
                        env:
                        - name: TESSERA_AGENT_NAME
                          value: "{agent_name}"
                        - name: TESSERA_DEPARTMENT
                          value: "{department}"
                        - name: TESSERA_PIPELINE
                          value: "{pipeline}"
                      restartPolicy: Never
        """)

def _generate_code(framework: str, agent_name: str, description: str,
                   tasks: list[str], pipeline: str, department: str) -> str:
    safe_class = ''.join(w.capitalize() for w in agent_name.replace('-','_').split('_'))
    task_list  = '\n'.join(f'    # {t}' for t in tasks) if tasks else '    # implement pipeline logic here'

    if framework == "langgraph":
        return textwrap.dedent(f"""\
            \"\"\"
            {agent_name} — {description}
            Department: {department} | Pipeline: {pipeline}
            Framework:  LangGraph
            Generated by Tessera Agent Factory
            \"\"\"
            import os, asyncio
            from typing import TypedDict, Annotated
            from langgraph.graph import StateGraph, END
            from langchain_anthropic import ChatAnthropic
            from tessera_sdk import TesseraTracer

            ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
            MODEL = os.getenv("TESSERA_MODEL", "claude-sonnet-4-6")

            llm = ChatAnthropic(model=MODEL, api_key=ANTHROPIC_API_KEY)
            tracer = TesseraTracer()


            class {safe_class}State(TypedDict):
                input: str
                context: dict
                result: str
                error: str | None


            # ── Nodes ──────────────────────────────────────────────────────────

            def intake_node(state: {safe_class}State) -> {safe_class}State:
                \"\"\"Parse and validate the incoming mandate.\"\"\"
                return {{**state, "context": {{"mandate": state["input"]}}}}


            def process_node(state: {safe_class}State) -> {safe_class}State:
                \"\"\"Core pipeline logic for {pipeline}.\"\"\"
                tasks = [
            {task_list}
                ]
                # TODO: implement tasks using `llm` or direct business logic
                response = llm.invoke(f"Execute for pipeline '{pipeline}': {{state['input']}}")
                return {{**state, "result": response.content}}


            def output_node(state: {safe_class}State) -> {safe_class}State:
                \"\"\"Emit result and log to Tessera trace.\"\"\"
                print(f"[{agent_name}] Pipeline complete: {{state['result'][:200]}}")
                return state


            def route(state: {safe_class}State) -> str:
                return "output" if not state.get("error") else END


            # ── Graph ──────────────────────────────────────────────────────────

            builder = StateGraph({safe_class}State)
            builder.add_node("intake",  intake_node)
            builder.add_node("process", process_node)
            builder.add_node("output",  output_node)
            builder.set_entry_point("intake")
            builder.add_edge("intake", "process")
            builder.add_conditional_edges("process", route, {{"output": "output", END: END}})
            builder.add_edge("output", END)
            graph = builder.compile()


            async def run(mandate: str = "run pipeline"):
                async with tracer.run("{agent_name}", branch="{department}") as r:
                    result = await graph.ainvoke({{
                        "input": mandate, "context": {{}}, "result": "", "error": None
                    }})
                    return result


            if __name__ == "__main__":
                asyncio.run(run())
        """)

    elif framework == "crewai":
        return textwrap.dedent(f"""\
            \"\"\"
            {agent_name} — {description}
            Department: {department} | Pipeline: {pipeline}
            Framework:  CrewAI
            Generated by Tessera Agent Factory
            \"\"\"
            import os, asyncio
            from crewai import Agent, Task, Crew, Process
            from langchain_anthropic import ChatAnthropic
            from tessera_sdk import TesseraTracer

            ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
            MODEL = os.getenv("TESSERA_MODEL", "claude-sonnet-4-6")

            llm = ChatAnthropic(model=MODEL, api_key=ANTHROPIC_API_KEY)
            tracer = TesseraTracer()


            # ── Agents ─────────────────────────────────────────────────────────

            orchestrator = Agent(
                role="Pipeline Orchestrator",
                goal="Execute the {pipeline} pipeline for {department} efficiently and accurately",
                backstory=\"\"\"You are the orchestrator for {agent_name}.
            Your purpose: {description}
            You ensure every task is completed with precision.\"\"\",
                llm=llm,
                verbose=True,
                allow_delegation=True,
            )

            analyst = Agent(
                role="Data Analyst",
                goal="Analyse inputs and produce structured outputs for {pipeline}",
                backstory="You process data and extract insights for the {department} team.",
                llm=llm,
                verbose=True,
            )


            # ── Tasks ──────────────────────────────────────────────────────────

            def build_tasks(mandate: str) -> list[Task]:
                return [
                    Task(
                        description=f"Analyse the mandate and extract key requirements: {{mandate}}",
                        agent=analyst,
                        expected_output="Structured JSON with requirements and constraints",
                    ),
                    Task(
                        description="Execute the {pipeline} pipeline based on the analysis",
                        agent=orchestrator,
                        expected_output="Pipeline execution report with outcomes",
                    ),
                ]


            # ── Run ────────────────────────────────────────────────────────────

            async def run(mandate: str = "run pipeline"):
                async with tracer.run("{agent_name}", branch="{department}") as r:
                    crew = Crew(
                        agents=[analyst, orchestrator],
                        tasks=build_tasks(mandate),
                        process=Process.sequential,
                        verbose=True,
                    )
                    result = crew.kickoff(inputs={{"mandate": mandate}})
                    return {{"result": str(result)}}


            if __name__ == "__main__":
                asyncio.run(run())
        """)

    elif framework == "autogen":
        return textwrap.dedent(f"""\
            \"\"\"
            {agent_name} — {description}
            Department: {department} | Pipeline: {pipeline}
            Framework:  AutoGen
            Generated by Tessera Agent Factory
            \"\"\"
            import os, asyncio
            import autogen
            from tessera_sdk import TesseraTracer

            ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
            MODEL = os.getenv("TESSERA_MODEL", "claude-sonnet-4-6")

            tracer = TesseraTracer()

            config_list = [{{
                "model": MODEL,
                "api_key": ANTHROPIC_API_KEY,
                "api_type": "anthropic",
            }}]

            llm_config = {{"config_list": config_list, "timeout": 120}}


            # ── Agents ─────────────────────────────────────────────────────────

            user_proxy = autogen.UserProxyAgent(
                name="TesseraOrchestrator",
                human_input_mode="NEVER",
                max_consecutive_auto_reply=10,
                is_termination_msg=lambda x: x.get("content", "").rstrip().endswith("DONE"),
                code_execution_config={{
                    "work_dir": "/tmp/agent_workspace",
                    "use_docker": False,
                }},
            )

            pipeline_agent = autogen.AssistantAgent(
                name="{safe_class}",
                llm_config=llm_config,
                system_message=\"\"\"You execute the {pipeline} pipeline for the {department} department.
            {description}
            When you have completed the task, end your reply with 'DONE'.\"\"\",
            )


            async def run(mandate: str = "run pipeline"):
                async with tracer.run("{agent_name}", branch="{department}") as r:
                    await user_proxy.a_initiate_chat(
                        pipeline_agent,
                        message=f"Execute {pipeline} pipeline. Mandate: {{mandate}}",
                    )
                    return {{"result": "AutoGen conversation complete"}}


            if __name__ == "__main__":
                asyncio.run(run())
        """)

    else:  # custom
        return textwrap.dedent(f"""\
            \"\"\"
            {agent_name} — {description}
            Department: {department} | Pipeline: {pipeline}
            Framework:  Custom (direct Anthropic SDK)
            Generated by Tessera Agent Factory
            \"\"\"
            import os, asyncio
            import anthropic
            from tessera_sdk import TesseraTracer

            ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
            MODEL = os.getenv("TESSERA_MODEL", "claude-sonnet-4-6")

            client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
            tracer = TesseraTracer()

            SYSTEM_PROMPT = \"\"\"You are {agent_name}, an AI agent for the {department} department.
            Pipeline: {pipeline}
            Purpose:  {description}

            Tasks you execute:
            {task_list}

            Be concise and return structured output. Focus only on your pipeline scope.
            \"\"\"


            async def run(mandate: str = "run pipeline") -> dict:
                async with tracer.run("{agent_name}", branch="{department}") as r:
                    # Step 1: Analyse mandate
                    analysis = await client.messages.create(
                        model=MODEL,
                        max_tokens=1024,
                        system=SYSTEM_PROMPT,
                        messages=[{{"role": "user", "content": f"Analyse and plan: {{mandate}}"}}],
                    )
                    plan = analysis.content[0].text

                    # Step 2: Execute
                    execution = await client.messages.create(
                        model=MODEL,
                        max_tokens=2048,
                        system=SYSTEM_PROMPT,
                        messages=[
                            {{"role": "user",      "content": f"Analyse and plan: {{mandate}}"}},
                            {{"role": "assistant", "content": plan}},
                            {{"role": "user",      "content": "Now execute. Return structured JSON output."}},
                        ],
                    )
                    result = execution.content[0].text

                    return {{
                        "agent":  "{agent_name}",
                        "pipeline": "{pipeline}",
                        "department": "{department}",
                        "mandate": mandate,
                        "result": result,
                    }}


            if __name__ == "__main__":
                result = asyncio.run(run())
                print(result)
        """)


# ─── Background deploy task ───────────────────────────────────────────────────

async def _run_deploy(dep_id: str, agent: dict, simulate: bool):
    """Full pipeline: provision → build → push → deploy. Runs in background."""
    try:
        agent_name  = agent["name"]
        framework   = agent["framework"]
        department  = agent["department"]
        pipeline    = agent["pipeline"]
        daily_runs  = agent["daily_runs"] or 0
        tasks       = agent.get("tasks_automated") or []
        description = agent.get("description") or pipeline
        org_id      = agent["org_id"]

        # Decide compute target
        compute = "aks" if daily_runs >= AKS_THRESHOLD else "aci"
        await _set_status(dep_id, "provisioning", f"Target: {compute.upper()}")

        # 1. Provision Azure infra
        await _set_status(dep_id, "provisioning", "Ensuring Azure Resource Group + ACR…")
        login_server = await _provision_azure(dep_id, org_id, simulate)

        # 2. Provision AKS if needed
        if compute == "aks":
            await _set_status(dep_id, "provisioning", "Ensuring AKS cluster…")
            await _provision_aks(dep_id, login_server, simulate)

        # 3. Generate code
        await _set_status(dep_id, "building", "Generating agent code…")
        code       = _generate_code(framework, agent_name, description, tasks, pipeline, department)
        dockerfile = _dockerfile(framework)
        image_tag  = f"{agent_name.lower()}:{dep_id[:8]}"
        full_image = f"{login_server}/{image_tag}"

        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE deployments SET generated_code=$2, dockerfile=$3, image_tag=$4, azure_acr_login=$5, updated_at=NOW() WHERE id=$1",
                dep_id, code, dockerfile, image_tag, login_server
            )

        # 4. Build + push image
        await _set_status(dep_id, "pushing", "Building and pushing Docker image…")
        ok = await _build_and_push(dep_id, image_tag, code, dockerfile, login_server, simulate)
        if not ok:
            await _set_status(dep_id, "failed", "Image build/push failed", "See logs for details")
            return

        # 5. Deploy
        await _set_status(dep_id, "deploying", f"Deploying to {compute.upper()}…")
        if compute == "aci":
            await _deploy_aci(dep_id, agent_name, image_tag, login_server,
                              department, pipeline, simulate)
        else:
            manifest = _k8s_manifest(agent_name, full_image, daily_runs, department, pipeline)
            async with db.acquire() as conn:
                await conn.execute("UPDATE deployments SET k8s_manifest=$2 WHERE id=$1", dep_id, manifest)
            await _deploy_aks(dep_id, agent_name, image_tag, login_server, manifest, simulate)

        await _set_status(dep_id, "running",
                         f"Agent '{agent_name}' running on {compute.upper()} ({login_server})")
        await _log(dep_id, f"Deployment complete. Compute: {compute.upper()}, Image: {full_image}")

    except Exception as e:
        await _set_status(dep_id, "failed", str(e)[:300], str(e))
        await _log(dep_id, f"ERROR: {e}")


# ─── API endpoints ────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "service": "deployment", "version": "1.0.0", "port": 8028,
        "azure_configured": _azure_configured(),
        "aks_threshold": AKS_THRESHOLD,
    }

@app.get("/health")
async def health():
    async with db.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok", "service": "deployment", "azure_configured": _azure_configured()}


@app.post("/deploy", status_code=202)
async def deploy(body: DeployRequest, background_tasks: BackgroundTasks):
    """
    Trigger a full deploy pipeline for an agent.
    Returns immediately with deployment ID; poll GET /deployments/{id} for status.
    """
    # Fetch agent details from agent_factory
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{AGENT_FACTORY_URL}/agents/{body.agent_id}")
    if r.status_code != 200:
        raise HTTPException(404, f"Agent {body.agent_id} not found in registry")
    agent = r.json()

    # Use simulate mode if Azure not configured
    simulate = body.simulate or not _azure_configured()

    dep_id = str(uuid.uuid4())
    daily_runs = agent.get("daily_runs", 0) or 0
    compute = "aks" if daily_runs >= AKS_THRESHOLD else "aci"

    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO deployments
              (id, agent_id, org_id, agent_name, department, pipeline, framework,
               compute_target, status, status_detail, daily_runs)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'queued','Queued for deployment',$9)
        """, dep_id, body.agent_id, body.org_id,
             agent.get("name"), agent.get("department"), agent.get("pipeline"),
             agent.get("framework"), compute, daily_runs)

    background_tasks.add_task(_run_deploy, dep_id, agent, simulate)

    return {
        "deployment_id": dep_id,
        "agent_id": body.agent_id,
        "agent_name": agent.get("name"),
        "compute_target": compute,
        "simulate": simulate,
        "status": "queued",
        "poll": f"/api/v1/deployment/deployments/{dep_id}",
        "note": "Deployment running in background. Poll the above URL for live status." if not simulate
                else "Running in SIMULATE mode (no real Azure calls). Set AZURE_* env vars for real deployment.",
    }


@app.get("/deployments")
async def list_deployments(org_id: str = "market360", agent_id: str = ""):
    async with db.acquire() as conn:
        if agent_id:
            rows = await conn.fetch(
                "SELECT * FROM deployments WHERE agent_id=$1 ORDER BY created_at DESC LIMIT 20", agent_id
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM deployments WHERE org_id=$1 ORDER BY created_at DESC LIMIT 50", org_id
            )
    return {"deployments": [_ser(r) for r in rows], "total": len(rows)}


@app.get("/deployments/{dep_id}")
async def get_deployment(dep_id: str):
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM deployments WHERE id=$1", dep_id)
    if not row:
        raise HTTPException(404, "Deployment not found")
    return _ser(row)


@app.get("/deployments/{dep_id}/code")
async def get_code(dep_id: str):
    """Return the generated Python code + Dockerfile + K8s manifest for download."""
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT agent_name, framework, generated_code, dockerfile, k8s_manifest, compute_target FROM deployments WHERE id=$1",
            dep_id
        )
    if not row:
        raise HTTPException(404, "Deployment not found")
    return {
        "agent_name":     row["agent_name"],
        "framework":      row["framework"],
        "compute_target": row["compute_target"],
        "agent_py":       row["generated_code"] or "",
        "dockerfile":     row["dockerfile"] or "",
        "k8s_manifest":   row["k8s_manifest"] or "",
        "tessera_sdk_py": _tessera_sdk_stub(),
    }


@app.post("/generate-code")
async def generate_code_only(body: DeployRequest):
    """
    Generate code artifacts without deploying.
    Returns agent.py + Dockerfile + k8s manifest + tessera_sdk.py.
    """
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{AGENT_FACTORY_URL}/agents/{body.agent_id}")
    if r.status_code != 200:
        raise HTTPException(404, f"Agent {body.agent_id} not found")
    agent = r.json()

    daily_runs  = agent.get("daily_runs", 0) or 0
    compute     = "aks" if daily_runs >= AKS_THRESHOLD else "aci"
    login_server = f"{AZURE_ACR_NAME}.azurecr.io" if AZURE_ACR_NAME else "tesseraacr.azurecr.io"
    image_tag   = f"{agent['name'].lower()}:latest"
    full_image  = f"{login_server}/{image_tag}"

    code       = _generate_code(agent["framework"], agent["name"],
                                 agent.get("description") or agent["pipeline"],
                                 agent.get("tasks_automated") or [],
                                 agent["pipeline"], agent["department"])
    dockerfile  = _dockerfile(agent["framework"])
    manifest    = _k8s_manifest(agent["name"], full_image, daily_runs,
                                 agent["department"], agent["pipeline"])

    return {
        "agent_name":     agent["name"],
        "framework":      agent["framework"],
        "compute_target": compute,
        "agent_py":       code,
        "dockerfile":     dockerfile,
        "k8s_manifest":   manifest if compute in ("aks",) else None,
        "tessera_sdk_py": _tessera_sdk_stub(),
        "image_tag":      image_tag,
        "acr_login":      login_server,
        "deploy_commands": [
            f"# Build and push to ACR",
            f"docker build -t {image_tag} .",
            f"docker tag {image_tag} {full_image}",
            f"az acr login --name {AZURE_ACR_NAME or 'tesseraacr'}",
            f"docker push {full_image}",
            f"# Deploy",
            f"kubectl apply -f k8s.yaml" if compute == "aks" else
            f"az container create --resource-group {AZURE_RESOURCE_GROUP} --name {agent['name'].lower()} --image {full_image} --cpu 1 --memory 1.5",
        ],
    }


@app.get("/azure/infra")
async def get_infra(org_id: str = "market360"):
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM azure_infra WHERE org_id=$1", org_id)
    if not row:
        return {
            "org_id": org_id, "status": "not_provisioned",
            "azure_configured": _azure_configured(),
            "message": "Run a deployment to provision Azure infrastructure.",
        }
    return _ser(row)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SERVICE_PORT", 8028)))
