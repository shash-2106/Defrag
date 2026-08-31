"""
Defrag API — FastAPI backend
Serves the SPA frontend and exposes REST endpoints for the agent pipeline.
"""

import os
import json
import asyncio
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from orchestrator import LifecycleOrchestrator
from llm_client import llm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("defrag.api")

app = FastAPI(
    title="Defrag — Infrastructure Watchdog API",
    description="Multi-agent lifecycle orchestrator for developer cloud resources",
    version="1.0.0",
)

# ============================================================================
# State — in-memory for current scan result
# ============================================================================

_scan_result: Optional[dict] = None
_scan_in_progress: bool = False
_orchestrator: Optional[LifecycleOrchestrator] = None


def get_orchestrator() -> LifecycleOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        credentials = {
            "aws": {"account_id": os.environ.get("AWS_ACCOUNT_ID", "")},
            "github": {"token": os.environ.get("GITHUB_TOKEN", "")},
            "gcp": {"project_id": os.environ.get("GCP_PROJECT_ID", "")},
        }
        _orchestrator = LifecycleOrchestrator(credentials)
    return _orchestrator


# ============================================================================
# REQUEST MODELS
# ============================================================================

class ApprovalRequest(BaseModel):
    plan_id: str
    approved_by: str  # Must be a non-system identifier (e.g., email or username)
    confirm: bool = False  # Must be True to proceed


# ============================================================================
# API ROUTES
# ============================================================================

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "llm_provider": llm.provider or "none (rule-based fallback)",
        "llm_model": llm.model_name or None,
        "llm_available": llm.available,
        "scan_available": _scan_result is not None,
        "scan_in_progress": _scan_in_progress,
    }


@app.post("/api/scan")
async def trigger_scan(background_tasks: BackgroundTasks, user_id: str = "demo_user"):
    """
    Trigger a full lifecycle scan.
    Runs Discovery → Inference → Risk → Optimization → Dry-run in background.
    """
    global _scan_in_progress, _scan_result
    if _scan_in_progress:
        return {"status": "in_progress", "message": "Scan already running"}

    _scan_in_progress = True

    def run_scan():
        global _scan_result, _scan_in_progress
        try:
            orch = get_orchestrator()
            result = orch.run_full_cycle(user_id)
            _scan_result = result
            logger.info(f"Scan complete: {result.get('run_id')}")
        except Exception as e:
            logger.error(f"Scan failed: {e}", exc_info=True)
            _scan_result = {"error": str(e), "timestamp": datetime.utcnow().isoformat()}
        finally:
            _scan_in_progress = False

    background_tasks.add_task(run_scan)
    return {"status": "started", "message": "Scan started in background. Poll /api/status for progress."}


@app.get("/api/status")
def scan_status():
    """Poll this endpoint after triggering a scan."""
    if _scan_in_progress:
        return {"status": "in_progress"}
    if _scan_result is None:
        return {"status": "no_scan", "message": "No scan has been run yet. POST to /api/scan to start."}
    if "error" in _scan_result:
        return {"status": "error", "error": _scan_result["error"]}
    return {
        "status": "complete",
        "run_id": _scan_result.get("run_id"),
        "timestamp": _scan_result.get("timestamp"),
        "simulation_mode": _scan_result.get("simulation_mode", True),
        "llm_provider": _scan_result.get("llm_provider", "none"),
        "llm_model": _scan_result.get("llm_model"),
        "providers_status": _scan_result.get("providers_status", {}),
    }


@app.get("/api/results")
def get_results():
    """
    Full scan results — projects, risks, plans, trajectory, audit log.
    This is the main data feed for the dashboard.
    """
    if _scan_result is None:
        raise HTTPException(status_code=404, detail="No scan results yet. POST /api/scan first.")
    if "error" in _scan_result:
        raise HTTPException(status_code=500, detail=_scan_result["error"])
    return _scan_result


@app.get("/api/projects")
def get_projects():
    if _scan_result is None:
        raise HTTPException(status_code=404, detail="No scan results. Run /api/scan first.")
    return {
        "projects": _scan_result.get("projects", []),
        "risk_assessments": _scan_result.get("risk_assessments", []),
        "simulation_mode": _scan_result.get("simulation_mode", True),
    }


@app.get("/api/plans")
def get_plans():
    if _scan_result is None:
        raise HTTPException(status_code=404, detail="No scan results. Run /api/scan first.")
    return {
        "optimization_plans": _scan_result.get("optimization_plans", {}),
        "execution_records": _scan_result.get("execution_records", []),
    }


@app.get("/api/trajectory")
def get_trajectory():
    """Agent execution trajectory — observable actions, no hidden chain-of-thought."""
    if _scan_result is None:
        raise HTTPException(status_code=404, detail="No scan results. Run /api/scan first.")
    return {
        "trajectory": _scan_result.get("trajectory", []),
        "audit_log": _scan_result.get("audit_log", []),
    }


@app.post("/api/approve")
def approve_plan(request: ApprovalRequest):
    """
    Human approval gate for consequential actions.
    - plan_id: ID of the plan to execute
    - approved_by: Must be a human identifier (not 'system' or 'auto')
    - confirm: Must be True
    SAFETY: EC2 STOP is executed; DELETE/TERMINATE is NOT implemented.
    """
    if not request.confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm must be True to execute an approved plan."
        )
    if not request.approved_by or request.approved_by.lower() in ("system", "auto", ""):
        raise HTTPException(
            status_code=403,
            detail="approved_by must be a human identifier. Automated execution is not permitted."
        )

    orch = get_orchestrator()
    result = orch.approve_and_execute(request.plan_id, request.approved_by)

    if result.get("status") in ("NOT_FOUND",):
        raise HTTPException(status_code=404, detail=result.get("reason"))
    if result.get("status") in ("BLOCKED", "REJECTED"):
        raise HTTPException(status_code=403, detail=result.get("reason"))

    # Update scan result with latest execution data
    if _scan_result is not None:
        existing = _scan_result.get("execution_records", [])
        # Append new record (don't duplicate)
        existing.append({
            "execution_id": result.get("execution_id"),
            "plan_id": request.plan_id,
            "user_id": request.approved_by,
            "status": result.get("status"),
            "actions_executed": result.get("actions_executed", []),
            "verification_polls": result.get("verification_polls", []),
        })

    return result


@app.get("/api/resources")
def get_resources():
    if _scan_result is None:
        raise HTTPException(status_code=404, detail="No scan results. Run /api/scan first.")
    return {
        "resources": _scan_result.get("resources", []),
        "subscriptions": _scan_result.get("subscriptions", []),
        "billing_events": _scan_result.get("billing_events", []),
        "providers_status": _scan_result.get("providers_status", {}),
    }


@app.get("/api/summary")
def get_summary():
    if _scan_result is None:
        raise HTTPException(status_code=404, detail="No scan results. Run /api/scan first.")
    return {
        "summary": _scan_result.get("summary", {}),
        "simulation_mode": _scan_result.get("simulation_mode", True),
        "llm_provider": _scan_result.get("llm_provider", "none"),
        "providers_status": _scan_result.get("providers_status", {}),
    }


# ============================================================================
# STATIC FILES — serve the SPA
# ============================================================================

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def serve_index():
        index = static_dir / "index.html"
        if index.exists():
            return HTMLResponse(content=index.read_text())
        return HTMLResponse("<h1>Defrag API is running. Frontend not found in /static/</h1>")

    @app.get("/{path:path}", response_class=HTMLResponse)
    def serve_spa(path: str):
        # Serve index.html for all non-API routes (SPA routing)
        if path.startswith("api/"):
            raise HTTPException(status_code=404)
        index = static_dir / "index.html"
        if index.exists():
            return HTMLResponse(content=index.read_text())
        raise HTTPException(status_code=404)
else:
    @app.get("/")
    def root():
        return {"message": "Defrag API running. Create /static/index.html to serve frontend."}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=True)
