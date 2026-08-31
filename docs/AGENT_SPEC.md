# HANDOFF Lifecycle Agent — Executable Specification
## For Antigravity Framework / Multi-Agent LLM Runtime

---

## METADATA

```yaml
system_name: "HANDOFF Lifecycle Agent"
version: "1.0"
framework: "antigravity"
timestamp: "2026-08-29"
author_context: "Systems engineer, 10+ years agentic workflow, hackathon judge"

# Agent topology
num_agents: 5
agent_names:
  - discovery
  - inference
  - risk_assessment
  - optimization
  - execution

# Execution mode
execution_mode: "orchestrated"
human_in_loop: true
approval_required_for: ["STOP", "DELETE", "ARCHIVE"]
escalation_enabled: true
```

---

## SYSTEM PROMPT (Orchestrator)

```
You are the HANDOFF Lifecycle Orchestrator, a multi-agent system designed to help developers
manage infrastructure and subscription lifecycle across cloud providers, SaaS platforms, and
personal projects.

Your role:
1. Coordinate five specialized agents (Discovery, Inference, Risk, Optimizer, Executor)
2. Pass context and evidence between agents
3. Detect conflicts and escalate to human review
4. Format all outputs for dashboard display
5. Ensure all decisions are logged with reasoning

Core principle: Intent > Activity
- Project ownership and dependency context matter more than raw utilization metrics
- When uncertain, escalate rather than decide
- Optimize for safety first, cost second
- Always provide human-readable reasoning for each recommendation

Constraints:
- NEVER recommend STOP/DELETE without confidence > 0.85
- ALWAYS log decision reasoning with evidence
- ALWAYS verify post-execution for critical actions
- ESCALATE any ambiguous resource ownership
- ESCALATE any cross-project dependencies not explicitly confirmed
```

---

## AGENT 1: DISCOVERY AGENT

### Role
Enumerate all resources, subscriptions, and billing events across all connected providers.
Output raw inventory with minimal inference.

### System Prompt
```
You are the Discovery Agent. Your mission is to catalog everything the user has signed up for:
- Cloud infrastructure (AWS EC2, RDS, Lambda; GCP Compute, SQL, Storage)
- SaaS subscriptions (via email parsing and Stripe)
- Repositories (GitHub, GitLab)
- Deployed services (Render, Vercel, Firebase)
- Billing alerts and renewal notifications

You do NOT make decisions about what to keep/stop.
You DO extract structured metadata: ID, cost, last usage, tags, expiry dates, provider.

If a provider API fails, log the error and continue with others.
Return results in JSON format.
```

### Tools (To Bind)
```
[discovery:aws_list_resources]
  Description: List all EC2, RDS, S3, Lambda, ElastiCache instances in AWS account
  Input:
    account_id: str
    role_arn: str
    regions: Optional[List[str]] = ["us-east-1"]
    resource_types: List[str] = ["ec2", "rds", "s3", "lambda"]
  Output:
    resources: List[Dict] with fields:
      - id, provider, type, name, created_date, monthly_cost, last_usage_date, tags, billing_status

[discovery:gcp_list_resources]
  Description: List Compute instances, Cloud SQL, Cloud Storage
  Input:
    project_id: str
    credentials: Dict
  Output:
    resources: List[Dict] (same schema as AWS)

[discovery:github_list_repos]
  Description: List all repositories in user's GitHub account + recent commit activity
  Input:
    token: str
    org: Optional[str] = None
  Output:
    repos: List[Dict] with fields:
      - id, name, url, last_commit_date, is_archived, stars

[discovery:parse_billing_emails]
  Description: Extract billing alerts, renewal dates, charges from Gmail
  Input:
    email_ids: List[str]
    keywords: List[str] = ["billing", "renewal", "alert", "invoice"]
  Output:
    events: List[Dict] with fields:
      - service, event_type, amount, renewal_date, detected_date, raw_email_text

[discovery:stripe_list_subscriptions]
  Description: List all active Stripe subscriptions
  Input:
    api_key: str
  Output:
    subscriptions: List[Dict] with fields:
      - id, service_name, renewal_date, amount, auto_renew, last_usage_date

[discovery:render_get_projects]
  Description: List Render deployments + status
  Input:
    api_key: str
  Output:
    services: List[Dict]

[discovery:firebase_get_projects]
  Description: List Firebase projects + billing info
  Input:
    credentials: Dict
  Output:
    projects: List[Dict]
```

### Input Schema
```json
{
  "user_id": "string (required)",
  "credentials": {
    "aws": {"account_id": "string", "role_arn": "string"},
    "gcp": {"project_id": "string", "credentials_json": "path or dict"},
    "github": {"token": "string"},
    "gmail": {"user_email": "string"},
    "stripe": {"api_key": "string"},
    "render": {"api_key": "string"},
    "firebase": {"credentials_json": "dict"}
  },
  "discovery_options": {
    "include_stopped_resources": false,
    "include_archived_repos": true,
    "email_lookback_days": 90,
    "include_zero_cost": false
  }
}
```

### Output Schema
```json
{
  "timestamp": "ISO-8601",
  "resources": [
    {
      "id": "string (unique per provider)",
      "provider": "aws|gcp|render|firebase",
      "type": "ec2|rds|s3|lambda|compute|sql|storage|github_repo|render_service",
      "name": "string",
      "created_date": "ISO-8601",
      "monthly_cost": "float (estimated or actual)",
      "last_usage_date": "ISO-8601 or null",
      "last_usage_type": "query|connection|invocation|commit|deployment",
      "tags": {"key": "value"},
      "region": "string",
      "status": "active|stopped|archived",
      "billing_status": "active|paused|trialing",
      "metadata": {
        "cpu_utilization_percent": "float or null",
        "memory_utilization_percent": "float or null",
        "connections_from": ["resource_id"],
        "has_backups": boolean,
        "has_auto_scaling": boolean
      }
    }
  ],
  "subscriptions": [
    {
      "id": "string",
      "provider": "stripe|render|firebase|github",
      "service_name": "string",
      "renewal_date": "ISO-8601",
      "monthly_cost": "float",
      "auto_renew": boolean,
      "last_usage_date": "ISO-8601 or null",
      "billing_cycle": "monthly|annual",
      "status": "active|trial|suspended"
    }
  ],
  "billing_events": [
    {
      "service": "string",
      "event_type": "renewal|alert|charge|overages",
      "amount": "float",
      "detected_date": "ISO-8601",
      "renewal_date": "ISO-8601 or null",
      "source": "email|api|invoice"
    }
  ],
  "discovery_errors": [
    {
      "provider": "string",
      "error": "string",
      "timestamp": "ISO-8601"
    }
  ],
  "summary": {
    "total_resources": "int",
    "total_subscriptions": "int",
    "total_monthly_cost": "float",
    "providers_queried": ["list"],
    "providers_failed": ["list"]
  }
}
```

### Success Criteria
- [ ] Enumerate ≥90% of user's resources (cross-provider)
- [ ] Extract renewal dates from ≥80% of subscription emails
- [ ] Return structured JSON with no missing required fields
- [ ] Handle API errors gracefully (don't crash; log + continue)
- [ ] Latency < 30 seconds for full discovery

---

## AGENT 2: INFERENCE AGENT

### Role
Build a project graph by clustering resources, inferring project ownership, modeling dependencies,
and correlating activity across providers.

### System Prompt
```
You are the Inference Agent. Your job is to make sense of the raw resource inventory by:

1. CLUSTERING: Group resources by inferred project using:
   - Explicit tags (project=X)
   - Naming patterns (pet-tracker-backend, pet-tracker-db, etc.)
   - Cross-provider correlation (GitHub repo name matches EC2 tag prefix)

2. DEPENDENCY MODELING: For each project:
   - Scan environment variables (EC2, Lambda) for service references
   - Check security group rules for network dependencies
   - Examine container definitions for linked services
   - Build dependency graph (who depends on whom)

3. ACTIVITY CORRELATION: Determine project health:
   - Most recent GitHub commit → project activity signal
   - Query/invocation patterns → which services are actually used
   - Passive services (databases, caches) vs. active (compute)

4. CONFIDENCE SCORING: For each resource → project assignment:
   - 0.6-0.8: Medium confidence (name pattern + activity)
   - 0.8-1.0: High confidence (explicit tag + recent activity)
   - <0.6: Ambiguous (flag for human review)

5. DEADLINE DETECTION: Track when resources/subscriptions expire:
   - Identify critical path (which services are most urgent)
   - Flag multi-service expirations on same project (coordination needed)

Output a project graph with confidence scores and evidence for each assignment.
Escalate ambiguous resources (confidence <0.6) to human review.
```

### Tools (To Bind)
```
[inference:build_dependency_graph]
  Description: Infer service dependencies from env vars, security groups, configs
  Input:
    resources: List[Dict]
    subscriptions: List[Dict]
  Output:
    dependencies: Dict[project_name][service_id] -> List[depends_on_ids]
    evidence: Dict explaining each dependency (env_var, sg_rule, etc.)

[inference:search_github_activity]
  Description: Get recent commit activity for repos matching project pattern
  Input:
    project_pattern: str
    github_token: str
    days_back: int = 30
  Output:
    commits: List[Dict] with last_commit_date, branch, message

[inference:calculate_project_confidence]
  Description: Score confidence of resource → project assignment
  Input:
    resource_id: str
    assigned_project: str
    evidence: Dict (tag_match, name_pattern_score, github_recency, etc.)
  Output:
    confidence_score: float (0.0-1.0)
    reasoning: str
    confidence_level: "AMBIGUOUS|MEDIUM|HIGH"

[inference:detect_unassigned_resources]
  Description: Identify resources that don't fit any project
  Input:
    resources: List[Dict]
    projects_assigned: Dict
  Output:
    unassigned: List[Dict] with reason, recommendation

[inference:extract_lifecycle_signals]
  Description: Determine project lifecycle state (ACTIVE, DEPRECATED, ARCHIVED)
  Input:
    project: Dict (resources, subscriptions, github_activity)
  Output:
    status: "ACTIVE|DEPRECATED|ARCHIVED|ABANDONED"
    days_since_activity: int
    confidence: float
```

### Input Schema
```json
{
  "resources": "List[Dict] from Discovery Agent",
  "subscriptions": "List[Dict] from Discovery Agent",
  "github_activity": {
    "repo_name": {
      "last_commit": "ISO-8601",
      "last_commit_author": "string",
      "last_commit_message": "string"
    }
  },
  "naming_patterns": {
    "project_separators": ["-", "_"],
    "common_suffixes": ["_backend", "_db", "_api", "_frontend", "_worker"]
  },
  "inference_options": {
    "confidence_threshold_ambiguous": 0.60,
    "confidence_threshold_medium": 0.80,
    "github_recency_window_days": 30,
    "activity_threshold_days": 90
  }
}
```

### Output Schema
```json
{
  "timestamp": "ISO-8601",
  "projects": [
    {
      "name": "string (e.g., pet-tracker)",
      "inferred_membership_confidence": "float (0.0-1.0)",
      "resources": [
        {
          "resource_id": "string",
          "resource_type": "string",
          "monthly_cost": "float",
          "confidence": "float (0.0-1.0)",
          "membership_reason": "explicit_tag|naming_pattern|dependency|github_correlation",
          "evidence": {
            "tag_match": "boolean",
            "name_pattern": "string or null",
            "github_repo_found": "boolean",
            "depends_on_ids": ["list"]
          }
        }
      ],
      "subscriptions": [
        {
          "subscription_id": "string",
          "service": "string",
          "renewal_date": "ISO-8601",
          "cost": "float",
          "confidence": "float"
        }
      ],
      "dependencies": {
        "resource_id_1": {
          "depends_on": ["resource_id_2", "resource_id_3"],
          "evidence": ["env_var_MONGO_URI", "sg_rule_3306"],
          "blast_radius": "if_removed: loses_X_dependent_services"
        }
      },
      "critical_deadlines": [
        {
          "resource_id": "string",
          "service_name": "string",
          "expiry_date": "ISO-8601",
          "days_until": "int",
          "urgency": "critical|urgent|upcoming",
          "is_critical_path": "boolean"
        }
      ],
      "lifecycle": {
        "status": "ACTIVE|DEPRECATED|ARCHIVED|ABANDONED",
        "last_github_activity": "ISO-8601 or null",
        "days_since_activity": "int",
        "activity_trend": "increasing|stable|decreasing",
        "has_production_services": "boolean",
        "has_recent_commits": "boolean"
      },
      "risk_flags": [
        {
          "flag": "string (e.g., missing_backup, orphaned_storage)",
          "severity": "low|medium|high"
        }
      ]
    }
  ],
  "unassigned_resources": [
    {
      "resource_id": "string",
      "resource_type": "string",
      "confidence_score": "float",
      "ambiguity_reason": "no_clear_project_match|multiple_possible_projects|orphaned_tag",
      "recommendation": "ESCALATE_FOR_REVIEW|INVESTIGATE_MORE"
    }
  ],
  "cross_project_anomalies": [
    {
      "anomaly": "string (e.g., active_deployment_abandoned_repo)",
      "projects_involved": ["list"],
      "severity": "low|medium|high"
    }
  ],
  "summary": {
    "projects_inferred": "int",
    "total_resources_assigned": "int",
    "total_unassigned": "int",
    "high_confidence_projects": "int",
    "ambiguous_resources": "int",
    "cross_project_dependencies": "int"
  }
}
```

### Success Criteria
- [ ] Infer project membership with average confidence > 0.80
- [ ] Detect ≥95% of direct service dependencies
- [ ] Correctly flag ambiguous resources for escalation
- [ ] Identify all critical-path services (deadlines)
- [ ] Latency < 20 seconds for inference

---

## AGENT 3: RISK ASSESSMENT AGENT

### Role
Evaluate risk, urgency, and blast radius for each project. Detect unsafe recommendations.

### System Prompt
```
You are the Risk Assessment Agent. Your job is to evaluate what could go wrong if we act
(or don't act) on each resource.

For each project, compute:

1. URGENCY: How soon is action required?
   - CRITICAL: ≥1 deadline in next 3 days (service will fail/renew if not renewed)
   - HIGH: ≥2 deadlines in next 7 days
   - MEDIUM: Deadlines 7-30 days out
   - LOW: Deadlines 30+ days out

2. BLAST RADIUS: If critical service fails, what breaks?
   - WIDE: Production service with 3+ dependent services
   - MEDIUM: Non-prod or 1-2 dependent services
   - NARROW: Isolated service, few dependents

3. CONFIDENCE: How confident are we in the project assessment?
   - HIGH (0.85+): Explicit tags + recent activity
   - MEDIUM (0.60-0.85): Good evidence but some ambiguity
   - LOW (<0.60): Conflicting signals; require human review

4. RECOMMENDED ACTION:
   - RENEW: If deadline imminent, renew all critical services
   - KEEP: If healthy and no urgent deadlines
   - ARCHIVE: If abandoned (90+ days) and no dependencies
   - MONITOR: If recent but inactivity trend, or optimization opportunity
   - ESCALATE: If ambiguous ownership, conflicting signals, or manual API required

5. UNSAFE FLAGS: Detect risky scenarios:
   - Stopping a database without backup
   - Deleting a service with unconfirmed dependents
   - Renewing high-cost service with zero usage
   - Ambiguous cross-account dependency

6. DAMAGE ESTIMATE: If we don't act by deadline:
   - Service will stop/fail
   - Users will be impacted (if production)
   - Data loss risk (if storage)
   - Cost multiplier if we let it auto-renew

Output detailed risk assessment. ESCALATE if unsafe.
```

### Tools (To Bind)
```
[risk:compute_blast_radius]
  Description: Estimate impact of stopping/deleting a service
  Input:
    resource_id: str
    dependency_graph: Dict
    is_production: boolean
    has_data: boolean
  Output:
    blast_radius_score: float (0.0-1.0)
    affected_services: List[str]
    user_impact: str

[risk:assess_project_health]
  Description: Overall project lifecycle state + trend
  Input:
    project: Dict
    github_activity: List[Dict]
    resource_metrics: Dict
  Output:
    health_score: float
    status: str
    trend: str

[risk:detect_irreversible_actions]
  Description: Flag actions that cannot be undone
  Input:
    action: "STOP|DELETE|ARCHIVE"
    resource: Dict
    backups_exist: boolean
  Output:
    is_reversible: boolean
    risk_level: str
    mitigation: str

[risk:flag_unsafe_recommendations]
  Description: Detect problematic recommendations
  Input:
    resource: Dict
    proposed_action: str
    project_context: Dict
  Output:
    is_safe: boolean
    flags: List[str]
    escalation_required: boolean

[risk:estimate_outage_time]
  Description: If service expires/fails, how long until user-facing impact?
  Input:
    service_type: str
    dependent_services: List[str]
    failover_available: boolean
  Output:
    hours_to_impact: float
    severity: str
```

### Input Schema
```json
{
  "projects": "List[Dict] from Inference Agent",
  "risk_thresholds": {
    "urgent_days": 3,
    "upcoming_days": 7,
    "abandoned_days": 90,
    "confidence_high": 0.85,
    "confidence_medium": 0.60
  },
  "user_context": {
    "is_production": "boolean",
    "has_users": "boolean",
    "revenue_impact": "boolean"
  }
}
```

### Output Schema
```json
{
  "timestamp": "ISO-8601",
  "risk_assessments": [
    {
      "project_name": "string",
      "urgency_level": "CRITICAL|HIGH|MEDIUM|LOW",
      "days_to_outage": "int (999 if n/a)",
      "blast_radius": "WIDE|MEDIUM|NARROW",
      "confidence": "float (0.0-1.0)",
      "recommended_action": "RENEW|KEEP|ARCHIVE|MONITOR|ESCALATE",
      "unsafe_action_flags": [
        {
          "flag": "string (e.g., no_backup_before_delete)",
          "severity": "warning|critical",
          "mitigation": "string"
        }
      ],
      "escalate_for_review": "boolean (if true, require human approval)",
      "escalation_reason": "string or null",
      "estimated_damage_if_ignored": {
        "project_downtime_hours": "float",
        "user_impact": "none|internal_only|production_unavailable",
        "data_risk": "none|backup_vulnerable|data_loss_risk",
        "cost_multiplier": "float (e.g., 1.5x if auto-renew + unused)"
      },
      "evidence": {
        "urgency_reason": "string",
        "blast_radius_reason": "string",
        "confidence_reason": "string",
        "recommended_action_reason": "string"
      }
    }
  ],
  "summary": {
    "critical_count": "int",
    "escalation_required_count": "int",
    "safe_to_automate": "int",
    "requires_human_review": "int"
  }
}
```

### Success Criteria
- [ ] Correctly identify 100% of deadline-driven urgencies
- [ ] Flag ≥95% of unsafe recommendations
- [ ] Properly calibrate confidence scores
- [ ] Escalate ambiguous cases (confidence <0.60)
- [ ] Latency < 10 seconds

---

## AGENT 4: OPTIMIZATION AGENT

### Role
Generate multiple optimization plans (MAINTAIN, SIMPLIFY, MIGRATE, ARCHIVE) with cost estimates
and effort/risk trade-offs.

### System Prompt
```
You are the Optimization Agent. Your job is to generate multiple viable plans for each project,
not just a single recommendation.

For each project, generate 2-4 plans in priority order:

1. MAINTAIN: Renew all critical services, keep current stack
   - Cost: $X/month
   - Effort: low (0.5 hours)
   - Risk: none
   - Action: Renew all deadlines

2. SIMPLIFY: Remove unused services from current stack
   - Identify services with zero usage in 60+ days
   - Remove non-critical dependencies
   - Cost: $X - $Y/month savings
   - Effort: medium (1-2 hours)
   - Risk: low (unused services only)
   - Action: Stop unused + renew critical

3. MIGRATE: Move to cheaper/better alternative stack
   - Example: Render → Railway, Firebase → Supabase
   - Cost: $X - $Y/month savings (after migration)
   - Effort: high (4-8 hours)
   - Risk: medium (requires testing, potential downtime)
   - Action: Parallel deployment, cutover, verify

4. ARCHIVE: Backup and shut down entire project
   - Cost: $X/month savings (complete)
   - Effort: medium (1-2 hours)
   - Risk: high if data recovery needed
   - Action: Snapshot DB, backup code, stop services
   - Note: Only if project abandoned 90+ days AND safe to delete

5. CONSOLIDATE: Merge with another project to share infrastructure
   - Cost: $X/month savings
   - Effort: high
   - Risk: medium

For each plan:
- List specific actions (RENEW service X, STOP service Y, MIGRATE X→Y)
- Estimate cost savings in $/month
- Estimate effort in hours
- Rank risk: LOW, MEDIUM, HIGH
- Mark if recommended (based on urgency + cost-benefit)
- Provide implementation steps

Order plans by recommendation priority.
Never recommend DELETE/ARCHIVE without 90+ days abandonment + no critical data.
```

### Tools (To Bind)
```
[optimization:recommend_consolidation]
  Description: Find opportunities to merge services/projects
  Input:
    projects: List[Dict]
    cost_threshold: float = 10
  Output:
    consolidation_opportunities: List[Dict]
    estimated_savings: float

[optimization:estimate_cost_savings]
  Description: Calculate cost delta for a plan
  Input:
    current_resources: List[Dict]
    proposed_actions: List[Dict]
    new_stack: Optional[List[Dict]]
  Output:
    monthly_savings: float
    confidence: float
    assumptions: List[str]

[optimization:check_technical_feasibility]
  Description: Can this migration/consolidation actually work?
  Input:
    from_stack: List[str]
    to_stack: List[str]
    project_constraints: Dict
  Output:
    is_feasible: boolean
    risk_score: float
    blockers: List[str]
    migration_steps: List[str]

[optimization:find_cheaper_alternatives]
  Description: For each service, what are cheaper alternatives?
  Input:
    services: List[Dict]
  Output:
    alternatives: Dict[service_name] -> List[alternative_options]

[optimization:estimate_migration_effort]
  Description: Time to migrate from X to Y
  Input:
    from_service: str
    to_service: str
    data_size: float
    downtime_tolerance: str
  Output:
    effort_hours: float
    migration_strategy: str
    downtime_minutes: float
```

### Input Schema
```json
{
  "projects": "List[Dict] from Risk Assessment",
  "optimization_options": {
    "max_downtime_minutes": 30,
    "consider_migrations": true,
    "cost_threshold_for_plan": 5,
    "archive_threshold_days": 90
  }
}
```

### Output Schema
```json
{
  "timestamp": "ISO-8601",
  "optimization_plans": {
    "project_name": [
      {
        "plan_id": "string (e.g., plan_pet_tracker_maintain)",
        "plan_name": "MAINTAIN|SIMPLIFY|MIGRATE|ARCHIVE|CONSOLIDATE",
        "description": "string (human-readable summary)",
        "priority": "int (1=highest)",
        "recommended": "boolean",
        "actions": [
          {
            "action": "RENEW|STOP|DELETE|MIGRATE|BACKUP|CONSOLIDATE",
            "resource_id": "string",
            "resource_name": "string",
            "reason": "string",
            "dry_run_expected": "string (what will happen)"
          }
        ],
        "cost_analysis": {
          "current_monthly_cost": "float",
          "proposed_monthly_cost": "float",
          "monthly_savings": "float",
          "annual_savings": "float",
          "one_time_migration_cost": "float or null"
        },
        "effort": {
          "total_hours": "float",
          "breakdown": {
            "planning": "float",
            "implementation": "float",
            "testing": "float",
            "verification": "float"
          }
        },
        "risk": {
          "overall_level": "LOW|MEDIUM|HIGH",
          "blockers": ["list"],
          "assumptions": ["list"],
          "mitigation": "string"
        },
        "timeline": {
          "can_execute_immediately": "boolean",
          "recommended_window": "string (e.g., 'weekend')",
          "downtime_minutes": "float or 0"
        },
        "success_criteria": [
          "All services healthy post-action",
          "Cost reduction confirmed",
          "No data loss"
        ],
        "rollback_plan": "string or null (if reversible)"
      }
    ]
  },
  "summary": {
    "total_monthly_savings_opportunity": "float",
    "total_annual_savings_opportunity": "float",
    "projects_with_optimization": "int",
    "high_impact_plans": "int"
  }
}
```

### Success Criteria
- [ ] Generate ≥2 viable plans per project
- [ ] Correctly estimate cost savings (within ±10%)
- [ ] Feasibility assessment accurate
- [ ] Rollback/mitigation strategy for risky plans
- [ ] Latency < 15 seconds

---

## AGENT 5: EXECUTION AGENT

### Role
Execute human-approved plans: dry-run, real execution, logging, verification.

### System Prompt
```
You are the Execution Agent. After a human approves a plan, you execute it safely:

WORKFLOW:
1. DRY-RUN: Simulate all actions. Log what would happen. Abort if errors.
2. EXECUTE: Call provider APIs to perform real actions.
3. LOG: Record execution with timestamps, results, who approved.
4. VERIFY: Poll resource state after action to confirm success.
5. ROLLBACK: If critical action fails, attempt rollback.

SAFETY PRINCIPLES:
- NEVER execute without human approval + timestamp
- ALWAYS dry-run before real execution
- ALWAYS log every decision + result
- ALWAYS verify post-execution (poll 1m, 5m, 30m after)
- ESCALATE on any error during real execution

REVERSIBLE vs. IRREVERSIBLE:
- REVERSIBLE (can undo): STOP, DOWNGRADE
- IRREVERSIBLE (cannot undo): DELETE, ARCHIVE without backup
- Do NOT execute irreversible actions without backup confirmation

STATE MACHINE:
  APPROVED_PLAN
    → DRY_RUN (simulate, log expected changes)
    → [errors? → ABORT]
    → [no errors → proceed]
    → EXECUTE (real actions)
    → [errors? → ATTEMPT_ROLLBACK]
    → [success? → proceed]
    → LOG (audit trail)
    → VERIFY (poll 1m, 5m, 30m)
    → SUCCESS or PARTIAL_SUCCESS or FAILURE
```

### Tools (To Bind)
```
[execution:execute_action]
  Description: Perform one action (RENEW, STOP, DELETE, MIGRATE, etc.)
  Input:
    action: str
    resource_id: str
    provider: str
    credentials: Dict
    dry_run: boolean = true
  Output:
    status: "DRY_RUN_OK|SUCCESS|FAILURE"
    result: Dict (action-specific)
    error: str or null
    timestamp: ISO-8601

[execution:verify_action_success]
  Description: Confirm resource state after action
  Input:
    action: str
    resource_id: str
    expected_state: str
    provider: str
    timeout_seconds: int = 300
  Output:
    is_healthy: boolean
    actual_state: str
    metrics: Dict
    verification_time: ISO-8601

[execution:log_agent_decision]
  Description: Create audit trail entry
  Input:
    agent_name: str
    decision: str
    confidence: float
    evidence: List[str]
    approved_by: str
    approved_at: ISO-8601
    action_id: str
  Output:
    log_id: str
    timestamp: ISO-8601

[execution:attempt_rollback]
  Description: Undo an action if it failed
  Input:
    action: str
    resource_id: str
    provider: str
    previous_state: Dict
  Output:
    rollback_success: boolean
    result: Dict

[execution:poll_resource_status]
  Description: Check resource health after action
  Input:
    resource_id: str
    provider: str
    poll_count: int = 3
    interval_seconds: List[int] = [60, 300, 1800]
  Output:
    polls: List[Dict] with timestamp, status, metrics
    final_status: str
```

### Input Schema
```json
{
  "plan_id": "string (from Optimization Agent)",
  "plan": "Dict (full plan object)",
  "user_id": "string",
  "approval": {
    "approved_by": "string (user email)",
    "approved_at": "ISO-8601",
    "approval_notes": "string or null"
  },
  "execution_options": {
    "dry_run_only": false,
    "skip_verification": false,
    "auto_rollback_on_error": true
  }
}
```

### Output Schema
```json
{
  "execution_id": "string (unique)",
  "plan_id": "string",
  "user_id": "string",
  "status": "DRY_RUN_OK|IN_PROGRESS|SUCCESS|PARTIAL_SUCCESS|FAILURE|ROLLED_BACK",
  "started_at": "ISO-8601",
  "completed_at": "ISO-8601 or null",
  "total_duration_seconds": "float",
  "approval": {
    "approved_by": "string",
    "approved_at": "ISO-8601"
  },
  "dry_run_results": [
    {
      "action": "string",
      "resource_id": "string",
      "expected_result": "string",
      "dry_run_status": "OK|ERROR",
      "dry_run_error": "string or null"
    }
  ],
  "actions_executed": [
    {
      "action_index": "int",
      "action": "string",
      "resource_id": "string",
      "resource_name": "string",
      "status": "SUCCESS|FAILURE|ROLLED_BACK",
      "executed_at": "ISO-8601",
      "result": {
        "new_expiry": "ISO-8601 or null",
        "stopped_at": "ISO-8601 or null",
        "deleted_at": "ISO-8601 or null",
        "migrated_to": "string or null"
      },
      "error": "string or null",
      "rollback_attempted": "boolean",
      "rollback_success": "boolean or null"
    }
  ],
  "verification": {
    "poll_count": "int",
    "polls": [
      {
        "poll_time": "ISO-8601 (1m, 5m, 30m post-action)",
        "resource_id": "string",
        "status": "HEALTHY|DEGRADED|UNHEALTHY",
        "metrics": {
          "http_status": "int or null",
          "response_time_ms": "float or null",
          "availability_percent": "float or null"
        }
      }
    ],
    "final_status": "ALL_HEALTHY|PARTIAL_UNHEALTHY|ALL_UNHEALTHY"
  },
  "audit_log_id": "string",
  "summary": {
    "actions_planned": "int",
    "actions_completed": "int",
    "cost_savings_realized": "float",
    "errors_encountered": "int",
    "rollbacks_performed": "int"
  }
}
```

### Success Criteria
- [ ] Execute 100% of approved actions
- [ ] Log all decisions with audit trail
- [ ] Verify health post-execution
- [ ] Rollback on critical errors
- [ ] Zero unlogged executions

---

## ORCHESTRATION FLOW

### Main Loop

```
INPUT: user_id, credentials
CALL: Discovery Agent
  OUTPUT: resources, subscriptions, billing_events

CALL: Inference Agent
  INPUT: (discovery output + github_activity)
  OUTPUT: projects, dependencies, confidence_scores, unassigned_resources

CALL: Risk Assessment Agent
  INPUT: projects, risk_thresholds
  OUTPUT: risk_assessments, escalation_flags

CALL: Optimization Agent
  INPUT: (risk_assessments)
  OUTPUT: optimization_plans[project] = [Plan A, Plan B, Plan C]

FORMAT: Dashboard (projects ordered by urgency)

WAIT: Human approval
  INPUT: user selects plan_id for each project

CALL: Execution Agent
  INPUT: plan_id, approval_record
  OUTPUT: execution_record (with verification)

RETURN: Updated dashboard + execution summary
```

### Error Handling

```
If Discovery fails on provider X:
  - Log error, continue with other providers
  - Mark provider as unavailable
  - Note in summary: "AWS unavailable, results from GCP only"

If Inference detects conflict (ambiguous resource):
  - Flag for escalation
  - Include in "requires_review" section
  - Do NOT make decision on behalf of human

If Risk Assessment flags unsafe action:
  - Set escalate_for_review = true
  - Do NOT include in "recommended" plans
  - Explain unsafe flag to human

If Execution dry-run fails:
  - Abort real execution
  - Return error with suggestions
  - Do NOT retry without human review

If Execution real fails:
  - Log error
  - Attempt rollback (if reversible)
  - Escalate to human with rollback status
```

### Escalation Criteria

**ALWAYS escalate to human review if:**
- Confidence < 0.60
- Unsafe action flags present
- Cross-account dependencies
- Missing backup for irreversible action
- Conflicting signals (active_deployment + abandoned_repo)
- Resource cost > $200/month + zero usage
- Shadow IT (unauthorized subscription)
- Email parsing failed (manual renewal needed)
- API rate limit hit (retry needed)

---

## CONFIGURATION & SETUP

### Environment Variables
```bash
# Discovery credentials
export AWS_ACCOUNT_ID="123456789"
export AWS_ROLE_ARN="arn:aws:iam::123456789:role/LifecycleAgent"
export GCP_PROJECT_ID="my-project"
export GCP_CREDENTIALS_JSON="/path/to/credentials.json"
export GITHUB_TOKEN="ghp_..."
export GMAIL_USER="user@example.com"
export STRIPE_API_KEY="sk_live_..."
export RENDER_API_KEY="rnd_..."
export FIREBASE_CREDENTIALS_JSON="/path/to/firebase.json"

# Execution credentials
export EXECUTION_APPROVAL_WEBHOOK="https://..."
export AUDIT_LOG_DB="postgresql://..."
```

### Initialization
```
1. Test all provider credentials
2. Run Discovery on small subset (1 resource per provider)
3. Verify Inference clustering (manual spot-check 3-5 projects)
4. Dry-run a single Optimization plan
5. Verify Execution logging + audit trail
```

---

## EVALUATION CHECKLIST

- [ ] Accuracy: Agent decisions vs. ground truth > 90%
- [ ] False-positive rate: < 5% (unsafe recommendations)
- [ ] Escalation rate: > 80% of ambiguous cases → human
- [ ] Confidence calibration: 90% confidence = 90% accuracy
- [ ] Cost detection: Identify ≥$10K/month of waste (if exists)
- [ ] Latency: Full cycle < 60 seconds
- [ ] Execution success: > 95% of approved plans execute fully
- [ ] Logging: 100% audit trail for all decisions

---

## PITCH (30-Second Summary)

> Developers accumulate infrastructure across 5–10 cloud providers while building projects.
> When projects end, services silently renew, costing money and creating security debt.
> 
> HANDOFF is a multi-agent system that discovers everything you've signed up for, groups it
> by project, models dependencies, predicts what will expire or cost you money, and creates
> safe, coordinated action plans—all with human approval and zero assumptions.
> 
> It turns "I have 47 random resources spread across 6 providers" into "Pet Tracker expires
> tomorrow, here's how to renew all 3 services together." All logged, all verified, all safe.

---

## METADATA FOR ANTIGRAVITY FRAMEWORK

```yaml
system_type: "multi_agent_orchestrator"
num_agents: 5
agent_topology:
  orchestrator:
    name: "lifecycle_orchestrator"
    role: "central_dispatcher"
    manages: ["discovery", "inference", "risk_assessment", "optimization", "execution"]
    
  agents:
    - name: "discovery"
      type: "data_collection"
      parallelizable: true
      tools: 12
      async_capable: true
      
    - name: "inference"
      type: "graph_building"
      parallelizable: false
      depends_on: ["discovery"]
      tools: 8
      
    - name: "risk_assessment"
      type: "evaluation"
      parallelizable: true
      depends_on: ["inference"]
      tools: 7
      
    - name: "optimization"
      type: "planning"
      parallelizable: true
      depends_on: ["risk_assessment"]
      tools: 6
      
    - name: "execution"
      type: "action"
      parallelizable: false
      depends_on: ["optimization"]
      tools: 5
      requires_approval: true

human_in_loop: true
approval_gates:
  - agent: "optimization"
    after_step: "plan_generation"
    approval_type: "user_selects_plan"
  
  - agent: "execution"
    after_step: "dry_run"
    approval_type: "execute_real_actions"
  
  - agent: "risk_assessment"
    after_step: "flagging"
    approval_type: "escalate_unsafe_recommendations"

error_handling:
  strategy: "graceful_degradation"
  recovery: "escalate_to_human"
  
output_formats:
  dashboard: "json"
  logs: "json"
  audit_trail: "json"
  
performance_targets:
  discovery_latency_seconds: 30
  inference_latency_seconds: 20
  risk_latency_seconds: 10
  optimization_latency_seconds: 15
  execution_latency_seconds: 60
  total_cycle_seconds: 120

success_metrics:
  accuracy_percent: 90
  false_positive_rate_percent: 5
  escalation_rate_percent: 80
  execution_success_rate_percent: 95
  cost_detection_usd: 10000
```

---

**End of Executable Agent Specification**  
**Ready for Antigravity Framework Instantiation**  
**Version 1.0 — August 29, 2026**
