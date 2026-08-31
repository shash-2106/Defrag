# HANDOFF Agent Architecture — Quick Reference

## System Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    LIFECYCLE ORCHESTRATOR                        │
│              (Central Multi-Agent Dispatcher + Memory)           │
└─────────────────────────────────────────────────────────────────┘
       │                    │                    │
       ▼                    ▼                    ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│ DISCOVERY AGENT │  │ INFERENCE AGENT │  │ RISK ASSESSMENT │
│                 │  │                 │  │     AGENT       │
│ Tools:          │  │ Tools:          │  │                 │
│ • aws_list      │  │ • project_graph │  │ Tools:          │
│ • gcp_list      │  │ • dependency_    │  │ • blast_radius  │
│ • github_list   │  │   graph         │  │ • escalation    │
│ • email_parse   │  │ • name_match    │  │   detector      │
│ • stripe_list   │  │ • git_activity  │  │                 │
│                 │  │                 │  │                 │
│ Output: Raw     │  │ Output: Projects│  │ Output: Risk    │
│ Resources +     │  │ with Context    │  │ Levels + Flags  │
│ Subscriptions   │  │                 │  │                 │
└─────────────────┘  └─────────────────┘  └─────────────────┘
       │                    │                    │
       └────────────────────┼────────────────────┘
                            ▼
       ┌────────────────────────────────────────┐
       │ OPTIMIZATION AGENT                     │
       │                                        │
       │ Tools:                                 │
       │ • consolidation_recommender            │
       │ • cost_estimator                       │
       │ • feasibility_checker                  │
       │                                        │
       │ Output: Multi-option plans             │
       │ (MAINTAIN, SIMPLIFY, MIGRATE, ARCHIVE)│
       └────────────────────────────────────────┘
                            │
                            ▼
       ┌────────────────────────────────────────┐
       │      HUMAN REVIEW DASHBOARD            │
       │   (User Approves 1 Plan Per Project)   │
       └────────────────────────────────────────┘
                            │
                            ▼
       ┌────────────────────────────────────────┐
       │ EXECUTION AGENT                        │
       │                                        │
       │ 1. Dry-Run (simulate all actions)     │
       │ 2. Real Execution (provider APIs)     │
       │ 3. Logging (audit trail)              │
       │ 4. Verification (poll 3x after)       │
       │                                        │
       │ Output: ExecutionRecord                │
       └────────────────────────────────────────┘
```

---

## Data Flow: One Full Cycle

```
User Action: "Run Lifecycle Check"
         ↓
    DISCOVERY AGENT
         ↓
    [AWS: 3 EC2, 2 RDS]
    [GCP: 1 Dataset]
    [GitHub: 6 repos]
    [Email: Render renewal alert]
    [Stripe: Audible renewal]
         ↓
    INFERENCE AGENT
         ↓
    [Project "pet-tracker": EC2+MongoDB+Render+GitHub repo]
    [Project "ml-experiment": EC2+Lambda]
    [Dependencies: EC2 → MongoDB → Render]
    [Deadlines: Render expires 2026-08-31, Firebase 2026-09-02]
         ↓
    RISK ASSESSMENT AGENT
         ↓
    [pet-tracker: URGENT (3 days to outage)]
    [ml-experiment: ARCHIVE (120 days idle)]
         ↓
    OPTIMIZATION AGENT
         ↓
    [pet-tracker Plan A: MAINTAIN (renew all 3)]
    [pet-tracker Plan B: MIGRATE (to Vercel, saves $60/mo)]
    [ml-experiment Plan: ARCHIVE (saves $124.50/mo)]
         ↓
    DASHBOARD
         ↓
    User sees:
    - 🔴 Pet Tracker: Renew 3 services by tomorrow [APPROVE PLAN A]
    - 🟡 ML Experiment: Archive or simplify [VIEW OPTIONS]
    - 🟡 Audible: Renews tomorrow [KEEP/CANCEL]
         ↓
    User approves Plan A for Pet Tracker
         ↓
    EXECUTION AGENT
         ↓
    [Dry-run: OK, no errors]
    [Execute: Renew Render, Firebase, MongoDB]
    [Verify at 1m, 5m, 30m: all healthy]
    [Log: execution_record_123.json]
         ↓
    Dashboard updated: ✓ Pet Tracker renewed, expires 2026-09-28
```

---

## Agent Specifications (Input/Output)

### 1. DISCOVERY AGENT

**Input:**
```json
{
  "user_id": "user_123",
  "credentials": {
    "aws": {"account_id": "123", "role_arn": "arn:..."},
    "gcp": {"project_id": "proj_id"},
    "github": {"token": "ghp_..."},
    "stripe": {"api_key": "sk_live_..."}
  }
}
```

**Output:**
```json
{
  "resources": [
    {
      "id": "i-backend",
      "provider": "aws",
      "type": "ec2",
      "monthly_cost": 120,
      "last_usage": "2026-08-22",
      "tags": {"project": "pet-tracker"}
    }
  ],
  "subscriptions": [
    {
      "id": "sub_render",
      "service": "render",
      "renewal_date": "2026-08-31",
      "cost": 7,
      "auto_renew": true
    }
  ],
  "billing_events": [
    {
      "service": "firebase",
      "event": "billing_alert",
      "amount": 45.23,
      "detected_date": "2026-08-29"
    }
  ]
}
```

---

### 2. INFERENCE AGENT

**Input:**
```json
{
  "resources": [/* from discovery */],
  "subscriptions": [/* from discovery */],
  "github_activity": {
    "pet-tracker": {"last_commit": "2026-08-15"},
    "ml-experiment": {"last_commit": "2024-06-10"}
  }
}
```

**Output:**
```json
{
  "projects": [
    {
      "name": "pet-tracker",
      "inferred_membership_confidence": 0.92,
      "resources": [
        {"id": "i-backend", "confidence": 0.95, "reason": "tag_match"},
        {"id": "rds-db", "confidence": 0.88, "reason": "dependency"}
      ],
      "dependencies": {
        "i-backend": ["rds-db", "firebase"],
        "rds-db": []
      },
      "critical_deadlines": [
        {"resource": "render", "date": "2026-08-31"},
        {"resource": "firebase", "date": "2026-09-02"}
      ],
      "days_since_activity": 7,
      "status": "ACTIVE"
    }
  ],
  "unassigned_resources": [
    {
      "id": "lambda-orphan",
      "reason": "no_project_match",
      "recommendation": "ESCALATE"
    }
  ]
}
```

---

### 3. RISK ASSESSMENT AGENT

**Input:**
```json
{
  "projects": [/* from inference */],
  "thresholds": {
    "urgent_days": 3,
    "upcoming_days": 7,
    "abandoned_days": 90
  }
}
```

**Output:**
```json
{
  "risk_assessments": [
    {
      "project": "pet-tracker",
      "urgency": "CRITICAL",
      "days_to_outage": 3,
      "blast_radius": "WIDE",
      "confidence": 0.94,
      "recommended_action": "RENEW_ALL",
      "unsafe_flags": [],
      "escalate": false,
      "damage_if_ignored": {
        "downtime_hours": 4,
        "user_impact": "production_unavailable"
      }
    }
  ]
}
```

---

### 4. OPTIMIZATION AGENT

**Input:**
```json
{
  "projects": [/* from inference */],
  "risk_assessments": [/* from risk */]
}
```

**Output:**
```json
{
  "pet-tracker": [
    {
      "plan_id": "plan_maintain",
      "plan_name": "MAINTAIN",
      "description": "Renew all critical services",
      "actions": [
        {"action": "RENEW", "resource": "render", "days": 30},
        {"action": "RENEW", "resource": "firebase", "days": 30}
      ],
      "savings_monthly": 0,
      "effort_hours": 0.5,
      "risk_level": "LOW",
      "recommended": true
    },
    {
      "plan_id": "plan_migrate",
      "plan_name": "MIGRATE",
      "description": "Move to Vercel + Supabase",
      "savings_monthly": 60,
      "effort_hours": 8,
      "risk_level": "MEDIUM",
      "recommended": false
    }
  ]
}
```

---

### 5. EXECUTION AGENT

**Input (after user approval):**
```json
{
  "plan_id": "plan_maintain",
  "user_id": "user_123",
  "approval_timestamp": "2026-08-29T16:00:00Z",
  "dry_run_first": true
}
```

**Output:**
```json
{
  "execution_id": "exec_123",
  "status": "SUCCESS",
  "started": "2026-08-29T16:01:00Z",
  "completed": "2026-08-29T16:02:15Z",
  "actions": [
    {
      "action": "RENEW",
      "resource": "render",
      "status": "SUCCESS",
      "result": {"new_expiry": "2026-09-28"}
    },
    {
      "action": "RENEW",
      "resource": "firebase",
      "status": "SUCCESS",
      "result": {"new_expiry": "2026-10-02"}
    }
  ],
  "verification": [
    {
      "resource": "render",
      "check_time": "2026-08-29T16:02:00Z",
      "status": "HEALTHY"
    },
    {
      "resource": "render",
      "check_time": "2026-08-29T16:07:00Z",
      "status": "HEALTHY"
    }
  ],
  "audit_log_id": "log_123"
}
```

---

## Tool Registry (Provider Integrations)

```
┌─────────────────────────────────────────────┐
│         PROVIDER INTEGRATION TOOLS          │
├─────────────────────────────────────────────┤
│ AWS                                         │
│  • ec2_list_instances()                    │
│  • rds_list_instances()                    │
│  • s3_list_buckets()                       │
│  • lambda_list_functions()                 │
│  • cloudwatch_get_metrics()                │
│  • iam_list_users()                        │
│                                             │
│ GCP                                         │
│  • compute_list_instances()                │
│  • sql_list_instances()                    │
│  • storage_list_buckets()                  │
│  • functions_list()                        │
│                                             │
│ GitHub                                      │
│  • repos_list()                            │
│  • commits_get_recent()                    │
│  • actions_get_runs()                      │
│                                             │
│ Email (Gmail API)                          │
│  • parse_billing_emails()                  │
│  • extract_renewal_dates()                 │
│  • detect_expiration_alerts()              │
│                                             │
│ Stripe                                      │
│  • subscriptions_list()                    │
│  • invoices_list()                         │
│  • customers_get()                         │
│                                             │
│ Render, Vercel, Firebase (API)             │
│  • get_project_status()                    │
│  • get_billing_info()                      │
│  • get_expiry_dates()                      │
│                                             │
│ Custom Heuristics                          │
│  • infer_project_from_tags()              │
│  • infer_project_from_naming()             │
│  • infer_dependencies()                    │
│  • calculate_blast_radius()                │
└─────────────────────────────────────────────┘
```

---

## Decision Tree (Confidence-Based)

```
START: Evaluate Resource
    │
    ├─→ Confidence > 0.85?
    │   ├─→ YES → Action decision confident
    │   │   ├─→ Abandoned? → STOP
    │   │   ├─→ Active? → KEEP
    │   │   ├─→ Expiring? → RENEW
    │   │   └─→ Old+inactive? → ARCHIVE
    │   │
    │   └─→ NO (0.60-0.85) → MEDIUM confidence
    │       ├─→ Passive dependency? → KEEP (but flag)
    │       ├─→ Ambiguous ownership? → ESCALATE
    │       └─→ Conflicting signals? → MONITOR
    │
    └─→ Confidence < 0.60?
        ├─→ Unassigned resource? → ESCALATE
        ├─→ Shadow IT? → ESCALATE
        ├─→ Cross-account? → ESCALATE
        └─→ Data-bearing? → ESCALATE
```

---

## Integration Checklist

- [ ] AWS IAM role configured (read-only for cost, discovery)
- [ ] GCP service account credentials
- [ ] GitHub personal access token
- [ ] Gmail API enabled + credentials
- [ ] Stripe API key
- [ ] Render/Vercel/Firebase API tokens
- [ ] Database for memory/state persistence
- [ ] Logging infrastructure
- [ ] Webhook for real-time billing alerts
- [ ] Email parser (Gmail labels: Billing, Lifecycle)

---

## Key Metrics to Track

**Agent Quality:**
- Accuracy (% correct decisions vs. ground truth)
- Confidence calibration (% correct among high-confidence decisions)
- False-positive rate (unsafe recommendations)
- Escalation rate (% of ambiguous cases → human)

**Business Impact:**
- Cost detected ($/month of waste identified)
- Cost avoided ($ of recommended actions approved)
- Execution success rate (% of approved plans complete)
- Human time saved (hours of manual review replaced)

**Operational:**
- Discovery latency (seconds to enumerate all resources)
- Inference latency (seconds to build dependency graph)
- End-to-end cycle time (discovery → dashboard ready)

---

## Known Limitations & Mitigations

| Limitation | Mitigation |
|---|---|
| Email parsing fragility | Multiple parsers, fallback to manual input |
| Missing provider integrations | Extensible tool registry; easy to add |
| Dependency inference incomplete | Conservative escalation when ambiguous |
| Rate limits on provider APIs | Exponential backoff, queued discovery |
| Circular dependencies | Detected and flagged in risk phase |
| New resource types | Custom tool templates for quick addition |

---

## Deployment Architecture

```
┌──────────────────────────────┐
│   Frontend Dashboard (React)  │
│  (Plan review + approval UI)  │
└──────────────────┬───────────┘
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
┌──────────────┐      ┌──────────────────┐
│ API Server   │      │ Background Worker│
│ (FastAPI)    │      │ (Celery/Ray)     │
│              │      │                  │
│ POST /approve│      │ run_discovery()  │
│ GET /status  │      │ build_projects() │
└──────┬───────┘      │ assess_risk()    │
       │              │ optimize()       │
       └──────┬───────┴──────────────────┘
              │
              ▼
    ┌──────────────────────┐
    │  Orchestrator Memory  │
    │  (PostgreSQL + Redis) │
    │                       │
    │ • raw_resources       │
    │ • projects            │
    │ • risk_assessments    │
    │ • optimization_plans  │
    │ • execution_records   │
    └──────────────────────┘
```

---

**Version:** 1.0  
**Last Updated:** 2026-08-29  
**Status:** Ready for Hackathon Submission
