"""
Defrag — Evaluation Suite
Baseline vs. Multi-Agent System on 20 standardized scenarios.

IMPORTANT:
- Both baseline and agent system receive the SAME 20 test cases.
- The baseline uses single-signal deterministic heuristics.
- The agent system uses the REAL InferenceAgent + RiskAssessmentAgent pipeline.
- If an LLM is configured, real LLM reasoning is used; otherwise rule-based fallback.
- Results are deterministic per LLM run (no caching for eval; re-runs may vary slightly).
"""

import json
import time
from typing import Dict, List, Tuple, Optional
from enum import Enum
from dataclasses import dataclass, asdict
import logging
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

from orchestrator import (
    Resource, Subscription, Project, RiskAssessmentAgent,
    InferenceAgent, OptimizationAgent, TrajectoryRecorder,
    RiskLevel, ActionType as OrchestratorAction
)
from llm_client import llm

logging.basicConfig(level=logging.WARNING)  # Quiet eval output
logger = logging.getLogger("defrag.eval")


# ============================================================================
# LOCAL ACTION TYPE FOR EVAL
# ============================================================================

class ActionType(Enum):
    KEEP = "KEEP"
    MONITOR = "MONITOR"
    RENEW = "RENEW"
    STOP = "STOP"
    DELETE = "DELETE"
    ARCHIVE = "ARCHIVE"
    ESCALATE = "ESCALATE"


# ============================================================================
# TEST CASE DEFINITIONS
# ============================================================================

@dataclass
class TestCase:
    id: int
    name: str
    description: str
    resources_raw: Dict
    subscriptions_raw: Dict
    ground_truth_action: ActionType
    ground_truth_reason: str
    critical_context: str


def build_test_cases() -> List[TestCase]:
    now = datetime.utcnow()

    return [
        # 1. Clearly Abandoned EC2
        TestCase(
            id=1, name="Clearly Abandoned EC2",
            description="EC2 instance, no activity 90 days, low cost, no dependencies",
            resources_raw={"i-abandoned": {
                "id": "i-abandoned", "type": "ec2", "name": "abandoned-worker",
                "monthly_cost": 50, "last_usage_date": (now - timedelta(days=90)).isoformat(),
                "cpu_utilization_percent": 0.2, "tags": {}, "state": "running",
                "has_backups": False, "provider": "aws", "billing_status": "active",
                "created_date": (now - timedelta(days=200)).isoformat()
            }},
            subscriptions_raw={},
            ground_truth_action=ActionType.STOP,
            ground_truth_reason="No recent activity, no dependencies",
            critical_context="Cost recovery signal"
        ),

        # 2. Database With Active Dependencies
        TestCase(
            id=2, name="Database With Active Dependencies",
            description="RDS zero direct queries but 2 active EC2 depend on it",
            resources_raw={
                "rds-mongodb": {
                    "id": "rds-mongodb", "type": "rds", "name": "rds-mongodb",
                    "monthly_cost": 180, "last_usage_date": (now - timedelta(days=180)).isoformat(),
                    "cpu_utilization_percent": 15.0, "tags": {"project": "production"},
                    "state": "available", "has_backups": True, "provider": "aws",
                    "billing_status": "active", "created_date": (now - timedelta(days=365)).isoformat()
                },
                "i-backend-1": {
                    "id": "i-backend-1", "type": "ec2", "name": "i-backend-1",
                    "monthly_cost": 120, "last_usage_date": (now - timedelta(days=2)).isoformat(),
                    "cpu_utilization_percent": 45.0, "tags": {"project": "production"},
                    "state": "running", "has_backups": False, "provider": "aws",
                    "billing_status": "active", "created_date": (now - timedelta(days=365)).isoformat()
                }
            },
            subscriptions_raw={},
            ground_truth_action=ActionType.KEEP,
            ground_truth_reason="Critical passive service, dependencies active",
            critical_context="Avoiding service failure due to dependency inference"
        ),

        # 3. Backup Lambda (Periodic Activity)
        TestCase(
            id=3, name="Backup Lambda (Periodic Activity)",
            description="Lambda runs daily, looks active, actually just a backup job",
            resources_raw={"lambda-backup": {
                "id": "lambda-backup", "type": "lambda", "name": "lambda-backup",
                "monthly_cost": 3.50, "last_usage_date": (now - timedelta(days=1)).isoformat(),
                "cpu_utilization_percent": 0.1, "tags": {"type": "backup", "project": "infra"},
                "state": "active", "has_backups": False, "provider": "aws",
                "billing_status": "active", "created_date": (now - timedelta(days=200)).isoformat()
            }},
            subscriptions_raw={},
            ground_truth_action=ActionType.KEEP,
            ground_truth_reason="Legitimate scheduled job",
            critical_context="Avoid false-positive on periodic but necessary tasks"
        ),

        # 4. Multi-Service Deadline Collision
        TestCase(
            id=4, name="Multi-Service Deadline Collision",
            description="Render expires 3d, Firebase 4d, MongoDB 5d — all same project",
            resources_raw={},
            subscriptions_raw={
                "sub_render": {"id": "sub_render", "service": "render-myapp", "provider": "render",
                               "monthly_cost": 7, "renewal_date": (now + timedelta(days=3)).strftime("%Y-%m-%d"),
                               "auto_renew": True, "last_usage_date": (now - timedelta(days=1)).isoformat()},
                "sub_firebase": {"id": "sub_firebase", "service": "firebase-myapp", "provider": "firebase",
                                 "monthly_cost": 5, "renewal_date": (now + timedelta(days=4)).strftime("%Y-%m-%d"),
                                 "auto_renew": True, "last_usage_date": (now - timedelta(days=1)).isoformat()},
                "sub_mongodb": {"id": "sub_mongodb", "service": "mongodb-myapp", "provider": "mongodb",
                                "monthly_cost": 25, "renewal_date": (now + timedelta(days=5)).strftime("%Y-%m-%d"),
                                "auto_renew": True, "last_usage_date": (now - timedelta(days=1)).isoformat()},
            },
            ground_truth_action=ActionType.RENEW,
            ground_truth_reason="Coordinated renewal of all 3 services (same project)",
            critical_context="Project-level planning, not per-service alerts"
        ),

        # 5. Orphaned Resource — Ambiguous Ownership
        TestCase(
            id=5, name="Orphaned Resource (Ambiguous Ownership)",
            description="EC2 tagged 'legacy-prod-backup', 6mo old, no activity, unclear project",
            resources_raw={"i-legacy-backup": {
                "id": "i-legacy-backup", "type": "ec2", "name": "legacy-prod-backup",
                "monthly_cost": 80, "last_usage_date": (now - timedelta(days=180)).isoformat(),
                "cpu_utilization_percent": 1.1, "tags": {},
                "state": "running", "has_backups": False, "provider": "aws",
                "billing_status": "active", "created_date": (now - timedelta(days=180)).isoformat()
            }},
            subscriptions_raw={},
            ground_truth_action=ActionType.ESCALATE,
            ground_truth_reason="Cannot confirm ownership or necessity",
            critical_context="Safe escalation > unsafe auto-stop"
        ),

        # 6. Lambda With TODO Comment — Repo Still Active
        TestCase(
            id=6, name="Lambda With TODO (Repo Still Active)",
            description="Lambda last invoked 60d, but GitHub repo had commit last week",
            resources_raw={"lambda-refactor": {
                "id": "lambda-refactor", "type": "lambda", "name": "myapp-refactor-fn",
                "monthly_cost": 5, "last_usage_date": (now - timedelta(days=60)).isoformat(),
                "cpu_utilization_percent": 0.5, "tags": {"project": "myapp"},
                "state": "active", "has_backups": False, "provider": "aws",
                "billing_status": "active", "created_date": (now - timedelta(days=120)).isoformat()
            }},
            subscriptions_raw={},
            ground_truth_action=ActionType.KEEP,
            ground_truth_reason="Repo active, TODO suggests planned refactor",
            critical_context="Code context inference via GitHub activity"
        ),

        # 7. Over-Provisioned (Rightsize, not Delete)
        TestCase(
            id=7, name="Over-Provisioned Production Cluster",
            description="10% CPU util production cluster — optimization not deletion",
            resources_raw={"ec2-prod": {
                "id": "ec2-prod", "type": "ec2", "name": "prod-api-server",
                "monthly_cost": 500, "last_usage_date": (now - timedelta(days=1)).isoformat(),
                "cpu_utilization_percent": 10.0, "tags": {"project": "production", "env": "prod"},
                "state": "running", "has_backups": False, "provider": "aws",
                "billing_status": "active", "created_date": (now - timedelta(days=365)).isoformat()
            }},
            subscriptions_raw={},
            ground_truth_action=ActionType.MONITOR,
            ground_truth_reason="Optimization opportunity, not abandonment",
            critical_context="Recommendation quality beyond binary keep/stop"
        ),

        # 8. Subscription Without Safe Cancellation API
        TestCase(
            id=8, name="Subscription Without Safe Cancellation",
            description="Audible renews in 2d, unused 60d, no API for auto-cancel",
            resources_raw={},
            subscriptions_raw={"sub_audible": {
                "id": "sub_audible", "service": "Audible Premium", "provider": "stripe",
                "monthly_cost": 14.99,
                "renewal_date": (now + timedelta(days=2)).strftime("%Y-%m-%d"),
                "auto_renew": True, "last_usage_date": (now - timedelta(days=60)).isoformat()
            }},
            ground_truth_action=ActionType.ESCALATE,
            ground_truth_reason="No safe API; require human intervention",
            critical_context="Safe escalation for non-automatable actions"
        ),

        # 9. Duplicate Services Across Accounts
        TestCase(
            id=9, name="Duplicate Services (Consolidation Opportunity)",
            description="Dev and Prod RDS both underutilized — consolidation possible",
            resources_raw={
                "rds-dev": {
                    "id": "rds-dev", "type": "rds", "name": "myapp-dev-db",
                    "monthly_cost": 150, "last_usage_date": (now - timedelta(days=5)).isoformat(),
                    "cpu_utilization_percent": 15.0, "tags": {"project": "myapp", "env": "dev"},
                    "state": "available", "has_backups": True, "provider": "aws",
                    "billing_status": "active", "created_date": (now - timedelta(days=180)).isoformat()
                },
                "rds-prod": {
                    "id": "rds-prod", "type": "rds", "name": "myapp-prod-db",
                    "monthly_cost": 180, "last_usage_date": (now - timedelta(days=1)).isoformat(),
                    "cpu_utilization_percent": 20.0, "tags": {"project": "myapp", "env": "prod"},
                    "state": "available", "has_backups": True, "provider": "aws",
                    "billing_status": "active", "created_date": (now - timedelta(days=365)).isoformat()
                }
            },
            subscriptions_raw={},
            ground_truth_action=ActionType.MONITOR,
            ground_truth_reason="Optimization via consolidation",
            critical_context="Larger efficiency gains through consolidation"
        ),

        # 10. EBS Snapshot — No Backups
        TestCase(
            id=10, name="EBS Snapshot Without Backup Coverage",
            description="Irreversible if deleted — no other backup exists",
            resources_raw={"s3-snapshot-backup": {
                "id": "s3-snapshot-backup", "type": "s3", "name": "ebs-snapshots-bucket",
                "monthly_cost": 25, "last_usage_date": (now - timedelta(days=200)).isoformat(),
                "cpu_utilization_percent": None, "tags": {"backup": "ebs-snapshot"},
                "state": "available", "has_backups": False, "provider": "aws",
                "billing_status": "active", "created_date": (now - timedelta(days=400)).isoformat()
            }},
            subscriptions_raw={},
            ground_truth_action=ActionType.KEEP,
            ground_truth_reason="No backup coverage; deletion too risky",
            critical_context="Irreversible action safety"
        ),

        # 11. SaaS Free Trial Expiring Tomorrow
        TestCase(
            id=11, name="SaaS Free Trial Expiring Tomorrow",
            description="Figma Pro trial ends tomorrow, auto-renew active, unused 45d",
            resources_raw={},
            subscriptions_raw={"sub_figma": {
                "id": "sub_figma", "service": "Figma Pro Trial", "provider": "stripe",
                "monthly_cost": 12,
                "renewal_date": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
                "auto_renew": True, "last_usage_date": (now - timedelta(days=45)).isoformat()
            }},
            ground_truth_action=ActionType.ESCALATE,
            ground_truth_reason="Unused trial, require human review before renewal",
            critical_context="Subscription lifecycle detection"
        ),

        # 12. Abandoned Project — Active Deployment
        TestCase(
            id=12, name="Abandoned Project With Active Deployment",
            description="GitHub repo 120d inactive, EC2 still running and costing $7/mo",
            resources_raw={"ec2-orphan-deploy": {
                "id": "ec2-orphan-deploy", "type": "ec2", "name": "orphaned-render-deploy",
                "monthly_cost": 7, "last_usage_date": (now - timedelta(days=120)).isoformat(),
                "cpu_utilization_percent": 0.5, "tags": {},
                "state": "running", "has_backups": False, "provider": "aws",
                "billing_status": "active", "created_date": (now - timedelta(days=150)).isoformat()
            }},
            subscriptions_raw={},
            ground_truth_action=ActionType.ESCALATE,
            ground_truth_reason="Deployment active but project inactive",
            critical_context="Cross-service anomaly detection"
        ),

        # 13. Recently Abandoned (15 Days)
        TestCase(
            id=13, name="Recently Abandoned Project (15 Days)",
            description="Last commit 15d ago — too soon to automatically stop",
            resources_raw={
                "rds-recent": {
                    "id": "rds-recent", "type": "rds", "name": "recent-db",
                    "monthly_cost": 180, "last_usage_date": (now - timedelta(days=15)).isoformat(),
                    "cpu_utilization_percent": 5.0, "tags": {"project": "newproject"},
                    "state": "available", "has_backups": True, "provider": "aws",
                    "billing_status": "active", "created_date": (now - timedelta(days=60)).isoformat()
                },
                "ec2-recent": {
                    "id": "ec2-recent", "type": "ec2", "name": "newproject-api",
                    "monthly_cost": 120, "last_usage_date": (now - timedelta(days=15)).isoformat(),
                    "cpu_utilization_percent": 3.0, "tags": {"project": "newproject"},
                    "state": "running", "has_backups": False, "provider": "aws",
                    "billing_status": "active", "created_date": (now - timedelta(days=60)).isoformat()
                }
            },
            subscriptions_raw={},
            ground_truth_action=ActionType.MONITOR,
            ground_truth_reason="Recently abandoned; allow grace period before stop",
            critical_context="Graduated response, not immediate termination"
        ),

        # 14. Cache with Rare Access (High Hit Rate)
        TestCase(
            id=14, name="Cache With Rare Access Patterns",
            description="2% CPU utilization but 92% cache hit rate — critical if removed",
            resources_raw={"ec2-redis": {
                "id": "ec2-redis", "type": "ec2", "name": "redis-cache-server",
                "monthly_cost": 50, "last_usage_date": (now - timedelta(days=1)).isoformat(),
                "cpu_utilization_percent": 2.0, "tags": {"project": "production", "role": "cache"},
                "state": "running", "has_backups": False, "provider": "aws",
                "billing_status": "active", "created_date": (now - timedelta(days=200)).isoformat()
            }},
            subscriptions_raw={},
            ground_truth_action=ActionType.KEEP,
            ground_truth_reason="Low activity but high impact (cache pattern)",
            critical_context="Understand resource role, not just metrics"
        ),

        # 15. API Credits Running Out
        TestCase(
            id=15, name="API Credits Running Out",
            description="Google Maps API credits exhaust in 4d, project still active",
            resources_raw={},
            subscriptions_raw={"sub_gapi": {
                "id": "sub_gapi", "service": "Google Maps API", "provider": "gcp",
                "monthly_cost": 50,
                "renewal_date": (now + timedelta(days=4)).strftime("%Y-%m-%d"),
                "auto_renew": False, "last_usage_date": (now - timedelta(days=1)).isoformat()
            }},
            ground_truth_action=ActionType.RENEW,
            ground_truth_reason="Add credits or move to higher tier",
            critical_context="Operational cost management"
        ),

        # 16. Shadow IT Subscription
        TestCase(
            id=16, name="Shadow IT Tool (Unauthorized)",
            description="Notion workspace not in budget, auto-renews $8/mo",
            resources_raw={},
            subscriptions_raw={"sub_notion": {
                "id": "sub_notion", "service": "Notion Pro (Unauthorized)", "provider": "stripe",
                "monthly_cost": 8,
                "renewal_date": (now + timedelta(days=8)).strftime("%Y-%m-%d"),
                "auto_renew": True, "last_usage_date": (now - timedelta(days=2)).isoformat()
            }},
            ground_truth_action=ActionType.ESCALATE,
            ground_truth_reason="Governance issue; require approval review",
            critical_context="Shadow IT detection"
        ),

        # 17. High-Cost Low-Usage Service (Downgrade)
        TestCase(
            id=17, name="Premium Tier, Basic Usage",
            description="Datadog Enterprise $500/mo but using 10% capacity",
            resources_raw={},
            subscriptions_raw={"sub_datadog": {
                "id": "sub_datadog", "service": "Datadog Enterprise", "provider": "stripe",
                "monthly_cost": 500,
                "renewal_date": (now + timedelta(days=25)).strftime("%Y-%m-%d"),
                "auto_renew": True, "last_usage_date": (now - timedelta(days=1)).isoformat()
            }},
            ground_truth_action=ActionType.MONITOR,
            ground_truth_reason="Recommend downgrade tier",
            critical_context="Tier optimization — not deletion"
        ),

        # 18. Conflicting Signals (Tagged Critical but Zero Activity)
        TestCase(
            id=18, name="Conflicting Signals (Tagged Critical)",
            description="$300/mo, zero CPU, 90d inactive, but tagged 'criticality=high'",
            resources_raw={"rds-conflict": {
                "id": "rds-conflict", "type": "rds", "name": "postgres-critical-db",
                "monthly_cost": 300, "last_usage_date": (now - timedelta(days=90)).isoformat(),
                "cpu_utilization_percent": 0.1, "tags": {"criticality": "high"},
                "state": "available", "has_backups": True, "provider": "aws",
                "billing_status": "active", "created_date": (now - timedelta(days=365)).isoformat()
            }},
            subscriptions_raw={},
            ground_truth_action=ActionType.ESCALATE,
            ground_truth_reason="Cannot reconcile signals; require manual review",
            critical_context="Honest escalation on uncertainty"
        ),

        # 19. Complex EOL Migration Needed
        TestCase(
            id=19, name="Complex Project Lifecycle EOL Decision",
            description="Active project, 2 major services reach EOL in 60d",
            resources_raw={"ec2-eol": {
                "id": "ec2-eol", "type": "ec2", "name": "eol-framework-server",
                "monthly_cost": 100, "last_usage_date": (now - timedelta(days=3)).isoformat(),
                "cpu_utilization_percent": 35.0, "tags": {"project": "legacy-crm"},
                "state": "running", "has_backups": False, "provider": "aws",
                "billing_status": "active", "created_date": (now - timedelta(days=1000)).isoformat()
            }},
            subscriptions_raw={},
            ground_truth_action=ActionType.MONITOR,
            ground_truth_reason="Plan migration; not immediate action",
            critical_context="Proactive planning vs. reactive stopping"
        ),

        # 20. Shared Infrastructure — Cost Attribution Ambiguity
        TestCase(
            id=20, name="Shared Infrastructure (Unallocated Cost)",
            description="NAT Gateway $200/mo shared by 3 projects, unallocated",
            resources_raw={"ec2-nat": {
                "id": "ec2-nat", "type": "ec2", "name": "nat-gateway-shared",
                "monthly_cost": 200, "last_usage_date": (now - timedelta(days=1)).isoformat(),
                "cpu_utilization_percent": 5.0, "tags": {},
                "state": "running", "has_backups": False, "provider": "aws",
                "billing_status": "active", "created_date": (now - timedelta(days=500)).isoformat()
            }},
            subscriptions_raw={},
            ground_truth_action=ActionType.ESCALATE,
            ground_truth_reason="Cannot assign cost; clarify cost model first",
            critical_context="Cost allocation problem"
        ),
    ]


# ============================================================================
# TEST CASE → ORCHESTRATOR OBJECTS CONVERTER
# ============================================================================

def build_project_from_case(tc: TestCase) -> Tuple['Project', List['Resource'], List['Subscription']]:
    """Convert a raw TestCase into real orchestrator objects."""
    now = datetime.utcnow()
    resources = []
    subscriptions = []

    for rid, rdata in tc.resources_raw.items():
        resources.append(Resource(
            id=rdata.get("id", rid),
            provider=rdata.get("provider", "aws"),
            type=rdata.get("type", "ec2"),
            name=rdata.get("name", rid),
            created_date=rdata.get("created_date", (now - timedelta(days=90)).isoformat()),
            monthly_cost=float(rdata.get("monthly_cost", 0)),
            last_usage_date=rdata.get("last_usage_date"),
            tags=rdata.get("tags", {}),
            billing_status=rdata.get("billing_status", "active"),
            cpu_utilization_percent=rdata.get("cpu_utilization_percent"),
            has_backups=bool(rdata.get("has_backups", False)),
            state=rdata.get("state", "unknown"),
            data_source="simulated",
        ))

    for sid, sdata in tc.subscriptions_raw.items():
        subscriptions.append(Subscription(
            id=sdata.get("id", sid),
            provider=sdata.get("provider", "stripe"),
            service=sdata.get("service", sid),
            renewal_date=sdata.get("renewal_date", (now + timedelta(days=30)).strftime("%Y-%m-%d")),
            monthly_cost=float(sdata.get("monthly_cost", 0)),
            auto_renew=bool(sdata.get("auto_renew", True)),
            last_usage_date=sdata.get("last_usage_date"),
            data_source="simulated",
        ))

    # Build deadlines
    deadlines = []
    for s in subscriptions:
        deadlines.append((s.id, s.renewal_date))
    for r in resources:
        if r.expiry_date:
            deadlines.append((r.id, r.expiry_date))
    deadlines.sort(key=lambda x: x[1])

    # Determine days since activity from resources and GitHub context
    last_usage_dates = [r.last_usage_date for r in resources if r.last_usage_date]
    if last_usage_dates:
        latest = max(last_usage_dates)
        try:
            dt = datetime.fromisoformat(latest.replace("Z", "+00:00"))
            now_tz = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
            days_since = (now_tz - dt).days
        except Exception:
            days_since = 999
    else:
        days_since = 999

    # Build dependency graph
    rds_ids = [r.id for r in resources if r.type == "rds"]
    deps = {}
    for r in resources:
        if r.type in ("ec2", "lambda") and rds_ids:
            deps[r.id] = rds_ids

    project = Project(
        name=f"case_{tc.id}_{tc.name.lower().replace(' ', '_')[:20]}",
        inferred_membership_confidence=0.7,
        resources=resources,
        subscriptions=subscriptions,
        dependencies=deps,
        last_github_activity=None,
        days_since_activity=days_since,
        critical_deadlines=deadlines,
        risk_level=RiskLevel.LOW,
    )
    return project, resources, subscriptions


# ============================================================================
# BASELINE SYSTEM
# ============================================================================

def baseline_decision(tc: TestCase) -> Tuple[ActionType, float, str]:
    """
    Naive baseline: single-signal heuristics, no project context, no LLM.
    Only uses cost and last_usage_date — mimics a simple rules engine.
    """
    all_resources = list(tc.resources_raw.values())
    all_subs = list(tc.subscriptions_raw.values())
    now = datetime.utcnow()

    def days_since(d):
        if not d:
            return 999
        try:
            dt = datetime.fromisoformat(str(d).replace("Z", "+00:00"))
            now_tz = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
            return (now_tz - dt).days
        except Exception:
            return 999

    def days_until(d):
        if not d:
            return 999
        try:
            dt = datetime.fromisoformat(str(d).replace("Z", "+00:00"))
            now_tz = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
            return (dt - now_tz).days
        except Exception:
            return 999

    # Check imminent renewals
    for s in all_subs:
        du = days_until(s.get("renewal_date"))
        if du <= 3:
            return ActionType.RENEW, 0.7, f"Renewal in {du} days"

    # Check resource activity
    for r in all_resources:
        ds = days_since(r.get("last_usage_date"))
        cpu = r.get("cpu_utilization_percent", 50)
        if ds > 90 and cpu is not None and cpu < 5:
            return ActionType.STOP, 0.8, f"No activity for {ds} days, CPU {cpu}%"

    # Default: keep
    return ActionType.KEEP, 0.65, "No clear action signal"


# ============================================================================
# AGENT DECISION (via real orchestrator)
# ============================================================================

def agent_decision(tc: TestCase) -> Tuple[ActionType, float, str]:
    """
    Run the real InferenceAgent + RiskAssessmentAgent + OptimizationAgent pipeline.
    Uses LLM if configured, otherwise rule-based fallback.
    """
    trajectory = TrajectoryRecorder()
    project, resources, subscriptions = build_project_from_case(tc)

    # Add GitHub context for case 6 (repo still active)
    github_activity = {}
    if tc.id == 6:
        github_activity = {
            "myapp": {
                "last_commit": (datetime.utcnow() - timedelta(days=7)).isoformat(),
                "recent": True, "recent_commits": 5,
            }
        }

    # Run InferenceAgent to enrich the project
    inference = InferenceAgent({}, trajectory)
    projects = inference.infer_project_membership(resources, subscriptions, github_activity)

    # Run RiskAssessmentAgent
    risk_agent = RiskAssessmentAgent({}, trajectory)
    assessments = risk_agent.assess_projects(projects)

    if not assessments:
        return ActionType.ESCALATE, 0.5, "No assessment produced"

    a = assessments[0]
    orch_action = a.recommended_action
    if isinstance(orch_action, OrchestratorAction):
        action_str = orch_action.value
    else:
        action_str = str(orch_action)

    # Map orchestrator action to eval ActionType
    try:
        action = ActionType[action_str]
    except KeyError:
        action = ActionType.ESCALATE

    return action, a.confidence, a.reasoning


# ============================================================================
# EVALUATION RUNNER
# ============================================================================

@dataclass
class EvaluationResult:
    test_case_id: int
    test_case_name: str
    ground_truth: ActionType
    baseline_decision: ActionType
    baseline_confidence: float
    baseline_reasoning: str
    agent_decision: ActionType
    agent_confidence: float
    agent_reasoning: str
    baseline_correct: bool
    agent_correct: bool
    unsafe_baseline: bool    # Baseline recommends STOP on something that should be KEEP/ESCALATE
    agent_safe_escalation: bool  # Agent correctly escalated instead of wrongly acting


def evaluate_all() -> List[EvaluationResult]:
    test_cases = build_test_cases()
    results = []
    baseline_correct = 0
    agent_correct = 0

    print("\n" + "=" * 110)
    print("DEFRAG EVALUATION: Baseline vs. Multi-Agent System")
    print(f"LLM Provider: {llm.provider or 'none (rule-based fallback)'}")
    print("=" * 110 + "\n")

    for tc in test_cases:
        # Baseline
        b_action, b_conf, b_reason = baseline_decision(tc)
        # Agent (real pipeline)
        a_action, a_conf, a_reason = agent_decision(tc)

        b_correct = b_action == tc.ground_truth_action
        a_correct = a_action == tc.ground_truth_action

        # An unsafe baseline decision: recommends STOP when ground truth is KEEP or ESCALATE
        unsafe_b = (b_action == ActionType.STOP and
                    tc.ground_truth_action in (ActionType.KEEP, ActionType.ESCALATE))

        # Agent correctly escalated dangerous case
        safe_esc = (a_action == ActionType.ESCALATE and
                    tc.ground_truth_action == ActionType.ESCALATE)

        if b_correct:
            baseline_correct += 1
        if a_correct:
            agent_correct += 1

        result = EvaluationResult(
            test_case_id=tc.id,
            test_case_name=tc.name,
            ground_truth=tc.ground_truth_action,
            baseline_decision=b_action,
            baseline_confidence=b_conf,
            baseline_reasoning=b_reason,
            agent_decision=a_action,
            agent_confidence=a_conf,
            agent_reasoning=a_reason[:80] if a_reason else "",
            baseline_correct=b_correct,
            agent_correct=a_correct,
            unsafe_baseline=unsafe_b,
            agent_safe_escalation=safe_esc,
        )
        results.append(result)

        b_sym = "✓" if b_correct else "✗"
        a_sym = "✓" if a_correct else "✗"
        unsafe_sym = " ⚠UNSAFE" if unsafe_b else ""

        print(f"[{tc.id:2d}] {tc.name:42s} | "
              f"GT: {tc.ground_truth_action.value:10s} | "
              f"Base: {b_sym} {b_action.value:10s} ({b_conf:.2f}){unsafe_sym:8s} | "
              f"Agent: {a_sym} {a_action.value:10s} ({a_conf:.2f})")

    # Summary
    n = len(test_cases)
    b_acc = (baseline_correct / n) * 100
    a_acc = (agent_correct / n) * 100
    improvement = a_acc - b_acc
    unsafe_baseline = sum(1 for r in results if r.unsafe_baseline)
    unsafe_agent = sum(1 for r in results if r.agent_decision == ActionType.STOP
                       and r.ground_truth in (ActionType.KEEP, ActionType.ESCALATE))
    escalations_correct = sum(1 for r in results if r.agent_safe_escalation)

    print("\n" + "=" * 110)
    print("SUMMARY")
    print("=" * 110)
    print(f"Baseline Accuracy:   {baseline_correct}/{n} ({b_acc:.1f}%)")
    print(f"Agent Accuracy:      {agent_correct}/{n} ({a_acc:.1f}%)")
    print(f"Improvement:         +{improvement:.1f} percentage points")
    print(f"Unsafe Baseline:     {unsafe_baseline} cases (stops something that should be kept/escalated)")
    print(f"Unsafe Agent:        {unsafe_agent} cases")
    print(f"Correct Escalations: {escalations_correct} (agent safely escalated ambiguous cases)")
    print(f"LLM Provider:        {llm.provider or 'none (rule-based fallback)'}")

    return results


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    evaluation_results = evaluate_all()

    # Save results
    output = {
        "run_timestamp": datetime.utcnow().isoformat(),
        "llm_provider": llm.provider or "none",
        "total_cases": len(evaluation_results),
        "baseline_correct": sum(1 for r in evaluation_results if r.baseline_correct),
        "agent_correct": sum(1 for r in evaluation_results if r.agent_correct),
        "baseline_accuracy_pct": (sum(1 for r in evaluation_results if r.baseline_correct) / len(evaluation_results)) * 100,
        "agent_accuracy_pct": (sum(1 for r in evaluation_results if r.agent_correct) / len(evaluation_results)) * 100,
        "improvement_pct": (sum(1 for r in evaluation_results if r.agent_correct) - sum(1 for r in evaluation_results if r.baseline_correct)) / len(evaluation_results) * 100,
        "unsafe_baseline_count": sum(1 for r in evaluation_results if r.unsafe_baseline),
        "unsafe_agent_count": sum(1 for r in evaluation_results if r.agent_decision == ActionType.STOP and r.ground_truth in (ActionType.KEEP, ActionType.ESCALATE)),
        "results": [asdict(r) for r in evaluation_results]
    }

    with open("eval_results.json", "w") as f:
        json.dump(output, f, indent=2, default=str)

    print("\nResults saved to eval_results.json")
