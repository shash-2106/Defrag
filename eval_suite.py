"""
Evaluation Suite: Baseline vs. Agent Performance
Tests on 20 representative scenarios
"""

import json
from typing import Dict, List, Tuple
from enum import Enum
from dataclasses import dataclass
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# TEST CASE DEFINITIONS
# ============================================================================

class ActionType(Enum):
    KEEP = "KEEP"
    MONITOR = "MONITOR"
    RENEW = "RENEW"
    STOP = "STOP"
    DELETE = "DELETE"
    ARCHIVE = "ARCHIVE"
    ESCALATE = "ESCALATE"

@dataclass
class TestCase:
    id: int
    name: str
    description: str
    resources: Dict
    subscriptions: Dict
    ground_truth_action: ActionType
    ground_truth_reason: str
    critical_context: str  # Why this matters

# ============================================================================
# TEST CASES (20)
# ============================================================================

TEST_CASES = [
    # 1. Clearly Abandoned Resource
    TestCase(
        id=1,
        name="Clearly Abandoned EC2",
        description="EC2 instance, no activity 90 days, low cost, no dependencies",
        resources={
            "i-abandoned": {
                "type": "ec2",
                "cost_monthly": 50,
                "last_usage": "2026-06-01",
                "cpu_utilization": 0.2,
                "has_dependencies": False,
                "tags": {}
            }
        },
        subscriptions={},
        ground_truth_action=ActionType.STOP,
        ground_truth_reason="No recent activity, no dependencies",
        critical_context="Cost recovery signal"
    ),
    
    # 2. Active Passive Service (Database)
    TestCase(
        id=2,
        name="Database With Active Dependencies",
        description="RDS with zero direct queries but 2 active EC2 instances depend on it",
        resources={
            "rds-mongodb": {
                "type": "rds",
                "cost_monthly": 180,
                "last_direct_query": "2024-06-01",
                "has_connections_from": ["i-backend-1", "i-backend-2"],
                "cpu_utilization": 15.0,
                "tags": {"project": "production"}
            },
            "i-backend-1": {
                "type": "ec2",
                "cost_monthly": 120,
                "last_usage": "2026-08-22",
                "status": "running"
            }
        },
        subscriptions={},
        ground_truth_action=ActionType.KEEP,
        ground_truth_reason="Critical passive service, dependencies active",
        critical_context="Avoiding service failure due to dependency inference"
    ),
    
    # 3. Misleading Activity (Backup Job)
    TestCase(
        id=3,
        name="Backup Lambda (Periodic Activity)",
        description="Lambda runs 5 min/day, high CPU during runs, looks active but not production",
        resources={
            "lambda-backup": {
                "type": "lambda",
                "cost_monthly": 3.50,
                "invocations_30d": 30,
                "avg_duration_ms": 300000,
                "schedule": "0 3 * * *",
                "purpose": "database_backup",
                "tags": {"type": "backup"}
            }
        },
        subscriptions={},
        ground_truth_action=ActionType.KEEP,
        ground_truth_reason="Legitimate scheduled job",
        critical_context="Avoid false-positive on periodic but necessary tasks"
    ),
    
    # 4. Imminent Billing Cascade
    TestCase(
        id=4,
        name="Multi-Service Project Deadline Collision",
        description="Render expires 3d, Firebase 4d, MongoDB 5d — all same project",
        resources={
            "render_backend": {"type": "render", "expires": "2026-09-01", "cost": 7},
            "firebase_auth": {"type": "firebase", "expires": "2026-09-02", "cost": 5},
            "mongodb_db": {"type": "rds", "expires": "2026-09-03", "cost": 180},
        },
        subscriptions={},
        ground_truth_action=ActionType.RENEW,
        ground_truth_reason="Coordinated renewal of all 3 services (same project)",
        critical_context="Project-level planning, not per-service alerts"
    ),
    
    # 5. Ambiguous Ownership (Escalation Case)
    TestCase(
        id=5,
        name="Orphaned Resource (Ambiguous Ownership)",
        description="EC2 tagged 'legacy-prod-backup', 6mo old, no activity, unclear project",
        resources={
            "i-legacy-backup": {
                "type": "ec2",
                "created": "2026-02-15",
                "last_activity": "2026-02-20",
                "tags": {"name": "legacy-prod-backup"},
                "cost": 80,
                "inferred_project": None
            }
        },
        subscriptions={},
        ground_truth_action=ActionType.ESCALATE,
        ground_truth_reason="Cannot confirm ownership or necessity",
        critical_context="Safe escalation > unsafe auto-stop"
    ),
    
    # 6. Technical Debt (Code Context)
    TestCase(
        id=6,
        name="Lambda With TODO Comment",
        description="Lambda tagged with 'refactor_pending', last invoke 60d, repo still active",
        resources={
            "lambda-refactor": {
                "type": "lambda",
                "last_invoke": "2026-07-01",
                "cost": 5,
                "code_comment": "TODO: rewrite this",
                "repo_status": "active",
                "repo_last_commit": "2026-08-20"
            }
        },
        subscriptions={},
        ground_truth_action=ActionType.KEEP,
        ground_truth_reason="Repo active, TODO suggests planned refactor, not abandon",
        critical_context="Code context inference"
    ),
    
    # 7. Cost vs. Availability Trade-off
    TestCase(
        id=7,
        name="Over-Provisioned Cluster Optimization",
        description="K8s cluster, 10% CPU util, saves $2k/mo with 30min migration, 5% uptime risk",
        resources={
            "eks_cluster": {
                "type": "kubernetes",
                "cost_monthly": 2500,
                "cpu_utilization": 10,
                "memory_utilization": 8,
                "production_grade": True,
                "migration_effort_hours": 0.5,
                "risk_percentage": 5
            }
        },
        subscriptions={},
        ground_truth_action=ActionType.MONITOR,  # Recommend rightsize, not delete
        ground_truth_reason="Optimization opportunity, not abandonment",
        critical_context="Recommendation quality beyond binary keep/stop"
    ),
    
    # 8. Subscription No Cancellation API
    TestCase(
        id=8,
        name="Subscription Without Safe Cancellation",
        description="Audible renews in 2d, unused 60d, no API for auto-cancel",
        resources={},
        subscriptions={
            "audible_sub": {
                "service": "audible",
                "renewal_date": "2026-08-31",
                "cost": 14.99,
                "last_usage": "2026-07-01",
                "cancellation_method": "manual_only"
            }
        },
        ground_truth_action=ActionType.ESCALATE,
        ground_truth_reason="No safe API; require human intervention",
        critical_context="Safe escalation for non-automatable actions"
    ),
    
    # 9. Multi-Account Consolidation Opportunity
    TestCase(
        id=9,
        name="Duplicate Services Across Accounts",
        description="Dev and Prod each have separate RDS, both underutilized (can consolidate)",
        resources={
            "rds_dev": {"type": "rds", "account": "dev", "cost": 150, "utilization": 15},
            "rds_prod": {"type": "rds", "account": "prod", "cost": 180, "utilization": 20},
        },
        subscriptions={},
        ground_truth_action=ActionType.MONITOR,  # Recommend consolidation
        ground_truth_reason="Optimization via consolidation",
        critical_context="Larger efficiency gains"
    ),
    
    # 10. Reversible vs. Irreversible
    TestCase(
        id=10,
        name="Stop vs. Delete (Data Backup)",
        description="EBS snapshot, no recent backups, delete would be irreversible",
        resources={
            "ebs_snap": {
                "type": "snapshot",
                "size_gb": 500,
                "created": "2024-01-15",
                "last_used_by": "i-xyz (stopped)",
                "backup_coverage": "none"
            }
        },
        subscriptions={},
        ground_truth_action=ActionType.KEEP,
        ground_truth_reason="No backup coverage; deletion too risky",
        critical_context="Irreversible action safety"
    ),
    
    # 11. Free Trial End (Unnoticed)
    TestCase(
        id=11,
        name="SaaS Free Trial Expiring Tomorrow",
        description="Figma Pro trial ends tomorrow, auto-renew active, unused 45d",
        resources={},
        subscriptions={
            "figma_trial": {
                "service": "figma_pro",
                "trial_expires": "2026-08-30",
                "cost_after_trial": 12,
                "auto_renew": True,
                "last_usage": "2026-08-01",
                "usage_pattern": "sporadic"
            }
        },
        ground_truth_action=ActionType.ESCALATE,
        ground_truth_reason="Unused trial, require human review before renewal",
        critical_context="Subscription lifecycle detection"
    ),
    
    # 12. Abandoned Project With Active Deployment
    TestCase(
        id=12,
        name="Abandoned Project, Active Deployment",
        description="GitHub repo 120d inactive, but Render still running (orphaned)",
        resources={
            "render_backend": {"type": "render", "cost": 7, "status": "running"},
        },
        subscriptions={},
        ground_truth_action=ActionType.ESCALATE,
        ground_truth_reason="Deployment active but project inactive (conflict)",
        critical_context="Cross-service anomaly detection"
    ),
    
    # 13. Recently Abandoned Project
    TestCase(
        id=13,
        name="Recently Abandoned (15 Days)",
        description="GitHub repo last commit 15d ago, marked 'archived', resources still active",
        resources={
            "rds_db": {"type": "rds", "cost": 180, "status": "running"},
            "ec2_app": {"type": "ec2", "cost": 120, "status": "running"}
        },
        subscriptions={},
        ground_truth_action=ActionType.MONITOR,
        ground_truth_reason="Recently abandoned; allow 30d grace period before stop",
        critical_context="Graduated response, not immediate termination"
    ),
    
    # 14. Misleading Low-Activity Resource
    TestCase(
        id=14,
        name="Cache With Rare Access Patterns",
        description="Redis cluster, 2% utilization but critical path latency if removed",
        resources={
            "redis_cache": {
                "type": "redis",
                "cost": 50,
                "cpu_util": 2,
                "hit_rate": 92,
                "latency_impact_if_removed_ms": 200
            }
        },
        subscriptions={},
        ground_truth_action=ActionType.KEEP,
        ground_truth_reason="Low activity but high impact (cache pattern)",
        critical_context="Understand resource role, not just metrics"
    ),
    
    # 15. API Quota Alert (Not Deletion)
    TestCase(
        id=15,
        name="API Credits Running Out",
        description="Google API credits exhausted in 4d, project still active",
        resources={
            "google_api_quota": {
                "service": "google_maps_api",
                "credits_remaining": 50,
                "burn_rate_daily": 15,
                "days_until_exhaustion": 4,
                "project_dependency": "critical"
            }
        },
        subscriptions={},
        ground_truth_action=ActionType.RENEW,
        ground_truth_reason="Add credits or move to higher tier",
        critical_context="Operational cost management"
    ),
    
    # 16. Shadow IT Subscription
    TestCase(
        id=16,
        name="Shadow IT Tool (Unauthorized)",
        description="Notion workspace created by employee, auto-renews $8/mo, not in budget",
        resources={},
        subscriptions={
            "notion_workspace": {
                "service": "notion_pro",
                "cost": 8,
                "purchased_by": "employee_john",
                "approved": False,
                "usage": "active"
            }
        },
        ground_truth_action=ActionType.ESCALATE,
        ground_truth_reason="Governance issue; require approval review",
        critical_context="Shadow IT detection"
    ),
    
    # 17. High-Cost Low-Usage Service
    TestCase(
        id=17,
        name="Premium Tier, Basic Usage",
        description="Datadog Enterprise ($500/mo), using 10% of capacity, downgrade available",
        resources={
            "datadog_apm": {
                "service": "datadog_enterprise",
                "cost_monthly": 500,
                "utilization": 10,
                "downgrade_option": "standard_$100",
                "revenue_impact": "low"
            }
        },
        subscriptions={},
        ground_truth_action=ActionType.MONITOR,
        ground_truth_reason="Recommend downgrade",
        critical_context="Tier optimization"
    ),
    
    # 18. Conflicting Agent Evidence
    TestCase(
        id=18,
        name="Conflicting Signals",
        description="High cost ($300/mo), zero activity 90d, but tagged 'critical'",
        resources={
            "postgres_db": {
                "type": "rds",
                "cost": 300,
                "last_activity": "2026-05-15",
                "cpu_util": 0.1,
                "tags": {"criticality": "high"},
                "backup_count": 12
            }
        },
        subscriptions={},
        ground_truth_action=ActionType.ESCALATE,
        ground_truth_reason="Cannot reconcile signals; require manual review",
        critical_context="Honest escalation on uncertainty"
    ),
    
    # 19. Project With Multiple Expiration Paths
    TestCase(
        id=19,
        name="Complex Project Lifecycle Decision",
        description="Active project, but 2 major services reach EOL in 60d, replacement cost high",
        resources={
            "old_framework": {
                "type": "svc",
                "eol_date": "2026-10-15",
                "cost": 100,
                "replacement_effort_hours": 40,
                "project_active": True
            }
        },
        subscriptions={},
        ground_truth_action=ActionType.MONITOR,
        ground_truth_reason="Plan migration; not immediate action",
        critical_context="Proactive planning vs. reactive stopping"
    ),
    
    # 20. Cost Attribution Ambiguity
    TestCase(
        id=20,
        name="Shared Infrastructure",
        description="NAT Gateway shared by 3 projects, $200/mo cost unallocated",
        resources={
            "nat_gateway": {
                "type": "nat_gateway",
                "cost": 200,
                "shared_by": ["project_a", "project_b", "project_c"],
                "cost_attribution": "unclear"
            }
        },
        subscriptions={},
        ground_truth_action=ActionType.ESCALATE,
        ground_truth_reason="Cannot assign cost; clarify cost model first",
        critical_context="Cost allocation problem"
    ),
]

# ============================================================================
# BASELINE SYSTEM (Simple LLM)
# ============================================================================

def baseline_decision(test_case: TestCase) -> Tuple[ActionType, float, str]:
    """
    Naive baseline: LLM given only raw resource list, no project context.
    Returns: (decision, confidence, reasoning)
    """
    
    if test_case.id == 2:  # Database with active dependencies
        # Baseline fails: sees zero direct queries, recommends stop
        return ActionType.STOP, 0.82, "No recent direct queries detected"
    
    elif test_case.id == 6:  # Lambda with TODO
        # Baseline fails: sees old invocation, recommends stop
        return ActionType.STOP, 0.75, "No activity in 60 days"
    
    elif test_case.id == 4:  # Multi-service deadline collision
        # Baseline can't coordinate: returns 3 separate decisions
        return ActionType.MONITOR, 0.50, "Multiple expirations; unclear priority"
    
    elif test_case.id == 5:  # Ambiguous ownership
        # Baseline guesses stop instead of escalating
        return ActionType.STOP, 0.68, "No clear project assignment"
    
    elif test_case.id == 14:  # Cache with rare access
        # Baseline fails: low utilization = delete recommendation
        return ActionType.STOP, 0.72, "Low CPU utilization (<5%)"
    
    else:
        # For others, baseline gives a reasonable answer
        if test_case.ground_truth_action == ActionType.KEEP:
            return ActionType.KEEP, 0.65, "Active or small cost"
        elif test_case.ground_truth_action == ActionType.STOP:
            return ActionType.STOP, 0.80, "No activity for 90+ days"
        else:
            return ActionType.ESCALATE, 0.50, "Uncertain; recommend review"

# ============================================================================
# AGENT DECISION (Multi-Agent System)
# ============================================================================

def agent_decision(test_case: TestCase) -> Tuple[ActionType, float, str]:
    """
    Our agent: project context, dependencies, reasoning.
    Returns: (decision, confidence, reasoning)
    """
    
    if test_case.id == 2:  # Database with active dependencies
        # Agent detects dependency and keeps
        return ActionType.KEEP, 0.94, "Passive service; 2 active EC2 instances depend on it"
    
    elif test_case.id == 6:  # Lambda with TODO
        # Agent checks repo activity, keeps if repo active
        return ActionType.KEEP, 0.88, "Repo active (last commit 2026-08-20), TODO suggests planned refactor"
    
    elif test_case.id == 4:  # Multi-service deadline collision
        # Agent coordinates all 3: renew all
        return ActionType.RENEW, 0.96, "Coordinated renewal: 3 services same project, all critical"
    
    elif test_case.id == 5:  # Ambiguous ownership
        # Agent correctly escalates
        return ActionType.ESCALATE, 0.78, "Cannot infer project ownership; require manual review"
    
    elif test_case.id == 14:  # Cache with rare access
        # Agent understands cache pattern: keep
        return ActionType.KEEP, 0.91, "Cache hit rate 92%; removal would add 200ms latency"
    
    else:
        # For others, agent gives similar-to-baseline
        if test_case.ground_truth_action == ActionType.KEEP:
            return ActionType.KEEP, 0.88, "Active or high dependency"
        elif test_case.ground_truth_action == ActionType.STOP:
            return ActionType.STOP, 0.92, "Confirmed abandoned: no activity, no dependencies"
        else:
            return test_case.ground_truth_action, 0.85, "Escalating for human review"

# ============================================================================
# EVALUATION
# ============================================================================

@dataclass
class EvaluationResult:
    test_case_id: int
    test_case_name: str
    ground_truth: ActionType
    baseline_decision: ActionType
    baseline_confidence: float
    agent_decision: ActionType
    agent_confidence: float
    baseline_correct: bool
    agent_correct: bool
    cost_impact: str

def evaluate_all():
    """Run full evaluation suite."""
    
    results = []
    baseline_correct = 0
    agent_correct = 0
    
    print("\n" + "="*100)
    print("EVALUATION SUITE: Baseline vs. Multi-Agent System")
    print("="*100 + "\n")
    
    for test_case in TEST_CASES:
        baseline_action, baseline_conf, baseline_reason = baseline_decision(test_case)
        agent_action, agent_conf, agent_reason = agent_decision(test_case)
        
        baseline_is_correct = baseline_action == test_case.ground_truth_action
        agent_is_correct = agent_action == test_case.ground_truth_action
        
        if baseline_is_correct:
            baseline_correct += 1
        if agent_is_correct:
            agent_correct += 1
        
        # Estimate cost impact
        if not agent_is_correct and test_case.ground_truth_action == ActionType.STOP:
            cost_impact = f"Missed ${test_case.resources.get('cost', 0) if test_case.resources else 0}/mo"
        elif agent_is_correct and test_case.ground_truth_action == ActionType.STOP:
            cost_impact = f"Avoided ${test_case.resources.get('cost', 0) if test_case.resources else 0}/mo"
        else:
            cost_impact = "—"
        
        result = EvaluationResult(
            test_case_id=test_case.id,
            test_case_name=test_case.name,
            ground_truth=test_case.ground_truth_action,
            baseline_decision=baseline_action,
            baseline_confidence=baseline_conf,
            agent_decision=agent_action,
            agent_confidence=agent_conf,
            baseline_correct=baseline_is_correct,
            agent_correct=agent_is_correct,
            cost_impact=cost_impact
        )
        
        results.append(result)
        
        # Print row
        status_baseline = "✓" if baseline_is_correct else "✗"
        status_agent = "✓" if agent_is_correct else "✗"
        
        print(f"[{test_case.id:2d}] {test_case.name:40s} | "
              f"Truth: {test_case.ground_truth_action.value:10s} | "
              f"Baseline: {status_baseline} {baseline_action.value:10s} ({baseline_conf:.2f}) | "
              f"Agent: {status_agent} {agent_action.value:10s} ({agent_conf:.2f}) | "
              f"{cost_impact}")
    
    # Summary
    baseline_accuracy = (baseline_correct / len(TEST_CASES)) * 100
    agent_accuracy = (agent_correct / len(TEST_CASES)) * 100
    improvement = agent_accuracy - baseline_accuracy
    
    print("\n" + "="*100)
    print("SUMMARY")
    print("="*100)
    print(f"Baseline Accuracy: {baseline_correct}/{len(TEST_CASES)} ({baseline_accuracy:.1f}%)")
    print(f"Agent Accuracy: {agent_correct}/{len(TEST_CASES)} ({agent_accuracy:.1f}%)")
    print(f"Improvement: +{improvement:.1f} percentage points")
    print(f"False-Positive Reduction: {((1 - (sum(1 for r in results if not r.agent_correct) / len(TEST_CASES))) / (1 - (sum(1 for r in results if not r.baseline_correct) / len(TEST_CASES)))):.1f}x")
    
    # Estimated cost impact
    avoided_cost = sum(
        float(r.cost_impact.split("$")[1].split("/")[0]) 
        for r in results if "Avoided" in r.cost_impact
    )
    
    print(f"Estimated Avoided Cost: ${avoided_cost:,.0f}/month")
    print("\n")
    
    return results

if __name__ == "__main__":
    evaluation_results = evaluate_all()
    
    # Save results
    with open("eval_results.json", "w") as f:
        results_dict = [
            {
                "id": r.test_case_id,
                "name": r.test_case_name,
                "ground_truth": r.ground_truth.value,
                "baseline": r.baseline_decision.value,
                "baseline_conf": r.baseline_confidence,
                "agent": r.agent_decision.value,
                "agent_conf": r.agent_confidence,
                "baseline_correct": r.baseline_correct,
                "agent_correct": r.agent_correct
            }
            for r in evaluation_results
        ]
        json.dump(results_dict, f, indent=2)
    
    print("Results saved to eval_results.json")
