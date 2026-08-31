"""
Defrag — Multi-Agent Personal Infrastructure & Subscription Watchdog
Production pipeline: Discover → Infer → Analyze → Risk → Decide → Approve → Execute → Verify

Agent responsibilities:
  DiscoveryAgent     — Enumerate resources across AWS, GCP, GitHub, Stripe, Render
  InferenceAgent     — Cluster resources into projects using LLM reasoning + tags + naming
  UsageAgent         — Enrich resources with CloudWatch/usage signals
  RiskAssessmentAgent — Evaluate urgency, blast radius, safety flags via LLM
  OptimizationAgent  — Generate ranked action plans via LLM
  ExecutionAgent     — Dry-run, seek human approval, execute, verify
  LifecycleOrchestrator — Coordinates all agents, records trajectory
"""

import json
import uuid
import time
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from enum import Enum
import logging
import os
import boto3
from botocore.exceptions import NoCredentialsError, PartialCredentialsError, ClientError

try:
    from google.cloud import compute_v1
    HAS_GCP = True
except ImportError:
    compute_v1 = None
    HAS_GCP = False

try:
    from github import Github
    HAS_GITHUB = True
except ImportError:
    Github = None
    HAS_GITHUB = False

try:
    import stripe
    HAS_STRIPE = True
except ImportError:
    stripe = None
    HAS_STRIPE = False

import requests
import db
from llm_client import llm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("defrag")


# ============================================================================
# DATA MODELS
# ============================================================================

class ActionType(Enum):
    KEEP = "KEEP"
    MONITOR = "MONITOR"
    RENEW = "RENEW"
    STOP = "STOP"
    ARCHIVE = "ARCHIVE"
    ESCALATE = "ESCALATE"
    MIGRATE = "MIGRATE"


class RiskLevel(Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass
class Resource:
    id: str
    provider: str
    type: str
    name: str
    created_date: str
    monthly_cost: float
    last_usage_date: Optional[str]
    tags: Dict[str, str]
    billing_status: str
    expiry_date: Optional[str] = None
    cpu_utilization_percent: Optional[float] = None
    has_backups: bool = False
    region: str = "us-east-1"
    state: str = "unknown"
    # SOURCE flag — critical for UI distinction
    data_source: str = "real"   # "real" | "simulated"


@dataclass
class Subscription:
    id: str
    provider: str
    service: str
    renewal_date: str
    monthly_cost: float
    auto_renew: bool
    last_usage_date: Optional[str]
    billing_cycle: str = "monthly"
    status: str = "active"
    data_source: str = "real"


@dataclass
class BillingEvent:
    service: str
    event_type: str
    amount: float
    detected_date: str
    renewal_date: Optional[str]
    source: str


@dataclass
class Project:
    name: str
    inferred_membership_confidence: float
    resources: List[Resource]
    subscriptions: List[Subscription]
    dependencies: Dict[str, List[str]]
    last_github_activity: Optional[str]
    days_since_activity: int
    critical_deadlines: List[Tuple[str, str]]
    risk_level: RiskLevel
    github_repo: Optional[str] = None
    recent_commits: int = 0


@dataclass
class RiskAssessment:
    project_name: str
    urgency_level: str
    blast_radius: str
    days_to_outage: int
    confidence: float
    recommended_action: ActionType
    unsafe_action_flags: List[Dict]
    estimated_damage: Dict
    reasoning: str = ""
    escalate_for_review: bool = False
    escalation_reason: Optional[str] = None
    # Produced from observed signals, not an LLM-invented confidence score.
    evidence_ledger: List[Dict] = field(default_factory=list)
    conflicts: List[Dict] = field(default_factory=list)
    lifecycle_state: str = "RECOMMENDED"


@dataclass
class OptimizationPlan:
    plan_id: str
    project_name: str
    plan_name: str
    description: str
    actions: List[Dict]
    total_monthly_savings: float
    effort_hours: float
    risk_level: RiskLevel
    recommended: bool
    reasoning: str = ""
    annual_savings: float = 0.0
    rollback_plan: Optional[str] = None
    implementation_steps: List[str] = field(default_factory=list)


@dataclass
class TrajectoryStep:
    """Single observable agent action — no hidden chain-of-thought."""
    step_id: str
    agent_name: str
    action: str           # What the agent did (e.g., "called CloudWatch API")
    inputs_summary: str   # Brief summary of inputs
    outputs_summary: str  # Brief summary of outputs/findings
    evidence: List[str]   # Concrete facts extracted
    decision: Optional[str] = None
    confidence: Optional[float] = None
    simulated: bool = False
    timestamp: str = ""
    error: Optional[str] = None

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.utcnow().isoformat()


@dataclass
class AuditLogEntry:
    log_id: str
    agent_name: str
    decision: str
    confidence: float
    evidence: List[str]
    timestamp: str
    action_id: Optional[str] = None


@dataclass
class ExecutionRecord:
    execution_id: str
    plan_id: str
    user_id: str
    status: str
    started_at: str
    completed_at: Optional[str]
    dry_run_results: List[Dict]
    actions_executed: List[Dict]
    verification_polls: List[Dict]
    audit_log_id: str
    cost_savings_realized: float = 0.0


# ============================================================================
# INSTANCE COST LOOKUP
# ============================================================================

INSTANCE_TYPE_COSTS = {
    't3.micro': 8.5, 't3.small': 17.0, 't3.medium': 34.0, 't3.large': 67.0,
    't4g.micro': 6.1, 't4g.small': 12.2, 't4g.medium': 24.3,
    'm5.large': 87.0, 'm5.xlarge': 174.0, 'm5.2xlarge': 348.0,
    'c5.large': 77.0, 'c5.xlarge': 154.0,
    'r5.large': 126.0, 'r5.xlarge': 252.0,
}
RDS_CLASS_COSTS = {
    'db.t3.micro': 18.0, 'db.t3.small': 36.0, 'db.t3.medium': 72.0,
    'db.r5.large': 180.0, 'db.r5.xlarge': 360.0,
}
RENDER_PLAN_COSTS = {'free': 0.0, 'starter': 7.0, 'standard': 25.0, 'pro': 85.0}


# ============================================================================
# SIMULATED DATASET (used when no cloud credentials are provided)
# The simulation runs through the REAL agent pipeline — LLM reasoning is real.
# ============================================================================

SIMULATED_RESOURCES = [
    Resource(
        id="i-0a1b2c3d4e5f67890", provider="aws", type="ec2",
        name="pet-tracker-backend",
        created_date="2024-01-15T10:00:00", monthly_cost=87.0,
        last_usage_date="2026-08-20T14:23:00",
        tags={"project": "pet-tracker", "env": "production", "owner": "alice"},
        billing_status="active", cpu_utilization_percent=12.5,
        state="running", data_source="simulated"
    ),
    Resource(
        id="rds-pet-tracker-db", provider="aws", type="rds",
        name="pet-tracker-db",
        created_date="2024-01-20T10:00:00", monthly_cost=180.0,
        last_usage_date="2026-08-22T09:00:00",
        tags={"project": "pet-tracker", "env": "production"},
        billing_status="active", has_backups=True,
        state="available", data_source="simulated"
    ),
    Resource(
        id="i-0dead1234beef5678", provider="aws", type="ec2",
        name="ml-experiment-v2-worker",
        created_date="2023-06-01T08:00:00", monthly_cost=34.0,
        last_usage_date="2024-06-10T00:00:00",
        tags={"project": "ml-experiment-v2", "env": "dev"},
        billing_status="active", cpu_utilization_percent=0.3,
        state="running", data_source="simulated"
    ),
    Resource(
        id="lambda-ml-warmer", provider="aws", type="lambda",
        name="ml-experiment-warmup",
        created_date="2023-06-01T08:00:00", monthly_cost=4.5,
        last_usage_date="2024-06-10T00:00:00",
        tags={"project": "ml-experiment-v2"},
        billing_status="active", cpu_utilization_percent=0.0,
        state="active", data_source="simulated"
    ),
    Resource(
        id="i-legacy-backup-07abc", provider="aws", type="ec2",
        name="legacy-prod-backup",
        created_date="2026-02-15T00:00:00", monthly_cost=80.0,
        last_usage_date="2026-02-20T00:00:00",
        tags={},   # No project tag — ambiguous ownership
        billing_status="active", cpu_utilization_percent=1.1,
        state="running", data_source="simulated"
    ),
]

SIMULATED_SUBSCRIPTIONS = [
    Subscription(
        id="sub_audible_001", provider="stripe", service="Audible Premium",
        renewal_date=(datetime.utcnow() + timedelta(days=2)).strftime("%Y-%m-%d"),
        monthly_cost=14.99, auto_renew=True,
        last_usage_date="2026-07-15", data_source="simulated"
    ),
    Subscription(
        id="sub_render_backend", provider="render", service="render-pet-tracker",
        renewal_date=(datetime.utcnow() + timedelta(days=9)).strftime("%Y-%m-%d"),
        monthly_cost=7.0, auto_renew=True,
        last_usage_date="2026-08-22", data_source="simulated"
    ),
    Subscription(
        id="sub_figma_trial", provider="stripe", service="Figma Pro Trial",
        renewal_date=(datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d"),
        monthly_cost=12.0, auto_renew=True,
        last_usage_date="2026-08-01", data_source="simulated"
    ),
]

SIMULATED_GITHUB_ACTIVITY = {
    "pet-tracker": {
        "last_commit": (datetime.utcnow() - timedelta(days=10)).isoformat(),
        "recent": True,
        "repo": "alice/pet-tracker",
        "recent_commits": 23,
        "open_issues": 4,
    },
    "ml-experiment-v2": {
        "last_commit": (datetime.utcnow() - timedelta(days=82)).isoformat(),
        "recent": False,
        "repo": "alice/ml-experiment-v2",
        "recent_commits": 1,
        "open_issues": 0,
    },
}


# ============================================================================
# TRAJECTORY RECORDER — shared across agents
# ============================================================================

class TrajectoryRecorder:
    def __init__(self):
        self.steps: List[TrajectoryStep] = []

    def record(self, agent_name: str, action: str, inputs_summary: str,
               outputs_summary: str, evidence: List[str],
               decision: str = None, confidence: float = None,
               simulated: bool = False, error: str = None) -> str:
        step_id = f"step_{uuid.uuid4().hex[:8]}"
        step = TrajectoryStep(
            step_id=step_id, agent_name=agent_name, action=action,
            inputs_summary=inputs_summary, outputs_summary=outputs_summary,
            evidence=evidence, decision=decision, confidence=confidence,
            simulated=simulated, error=error
        )
        self.steps.append(step)
        logger.info(f"[{agent_name}] {action}: {outputs_summary}")
        return step_id

    def as_list(self) -> List[Dict]:
        return [asdict(s) for s in self.steps]


# ============================================================================
# DISCOVERY AGENT
# ============================================================================

class DiscoveryAgent:
    """
    Enumerate resources across all providers.
    REAL: AWS (EC2, RDS, Lambda, S3 + CloudWatch), GitHub, Stripe, Render
    SIMULATED: Rich scenario dataset used when credentials missing
    Both paths feed into the REAL agent pipeline — no bypass.
    """

    def __init__(self, credentials: Dict, trajectory: TrajectoryRecorder):
        self.credentials = credentials
        self.trajectory = trajectory
        self.discovery_errors: List[Dict] = []

        # AWS clients
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        try:
            self.ec2_client = boto3.client('ec2', region_name=region)
            self.rds_client = boto3.client('rds', region_name=region)
            self.s3_client = boto3.client('s3')
            self.lambda_client = boto3.client('lambda', region_name=region)
            self.cloudwatch_client = boto3.client('cloudwatch', region_name=region)
            self.aws_region = region
            # Validate credentials with a cheap call
            self.ec2_client.describe_regions(RegionNames=[region])
            self.aws_available = True
            logger.info("AWS: credentials validated")
        except (NoCredentialsError, PartialCredentialsError) as e:
            logger.warning(f"AWS: no/partial credentials — {e}")
            self.aws_available = False
        except ClientError as e:
            code = e.response['Error']['Code']
            if code in ('AuthFailure', 'InvalidClientTokenId', 'UnauthorizedOperation'):
                logger.warning(f"AWS: auth failure — {e}")
                self.aws_available = False
            else:
                self.aws_available = True
        except Exception as e:
            logger.warning(f"AWS clients init failed: {e}")
            self.aws_available = False

        # GCP
        self.gcp_project_id = os.environ.get("GCP_PROJECT_ID")
        try:
            if HAS_GCP and self.gcp_project_id:
                self.gcp_compute = compute_v1.InstancesClient()
                self.gcp_available = True
            else:
                self.gcp_compute = None
                self.gcp_available = False
        except Exception:
            self.gcp_compute = None
            self.gcp_available = False

        # GitHub
        github_token = os.environ.get("GITHUB_TOKEN") or credentials.get("github", {}).get("token", "")
        try:
            if HAS_GITHUB and github_token:
                # Use new auth API to suppress deprecation warning
                try:
                    from github import Auth as GithubAuth
                    self.github = Github(auth=GithubAuth.Token(github_token))
                except ImportError:
                    self.github = Github(github_token)
                # Validate
                _ = self.github.get_user().login
                self.github_available = True
                logger.info("GitHub: authenticated")
            else:
                self.github = None
                self.github_available = False
        except Exception as e:
            logger.warning(f"GitHub auth failed: {e}")
            self.github = None
            self.github_available = False

        # Stripe
        if HAS_STRIPE and os.environ.get("STRIPE_API_KEY"):
            stripe.api_key = os.environ.get("STRIPE_API_KEY")
            self.stripe_available = True
        else:
            self.stripe_available = False

        # Render
        self.render_api_key = os.environ.get("RENDER_API_KEY")
        self.render_available = bool(self.render_api_key)

    def _get_cloudwatch_cpu(self, instance_id: str) -> Optional[float]:
        try:
            resp = self.cloudwatch_client.get_metric_statistics(
                Namespace='AWS/EC2', MetricName='CPUUtilization',
                Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}],
                StartTime=datetime.utcnow() - timedelta(days=7),
                EndTime=datetime.utcnow(), Period=604800, Statistics=['Average']
            )
            pts = resp.get('Datapoints', [])
            return round(pts[0]['Average'], 2) if pts else None
        except Exception:
            return None

    def _scan_aws(self) -> Tuple[List[Resource], List[BillingEvent]]:
        resources, billing_events = [], []
        if not self.aws_available:
            return resources, billing_events

        # EC2
        try:
            resp = self.ec2_client.describe_instances(
                Filters=[{'Name': 'instance-state-name', 'Values': ['running', 'stopped', 'stopping']}]
            )
            for res in resp.get('Reservations', []):
                for inst in res.get('Instances', []):
                    tags = {t['Key']: t['Value'] for t in inst.get('Tags', [])}
                    name = tags.get('Name', inst['InstanceId'])
                    cpu = self._get_cloudwatch_cpu(inst['InstanceId'])
                    cost = INSTANCE_TYPE_COSTS.get(inst.get('InstanceType', 't3.micro'), 15.0)
                    resources.append(Resource(
                        id=inst['InstanceId'], provider="aws", type="ec2", name=name,
                        created_date=inst['LaunchTime'].isoformat(), monthly_cost=cost,
                        last_usage_date=None, tags=tags, billing_status="active",
                        cpu_utilization_percent=cpu,
                        region=inst.get('Placement', {}).get('AvailabilityZone', self.aws_region),
                        state=inst.get('State', {}).get('Name', 'unknown'),
                        data_source="real"
                    ))
            self.trajectory.record(
                "DiscoveryAgent", "AWS EC2 scan",
                "describe_instances (running/stopped)",
                f"Found {len([r for r in resources if r.type == 'ec2'])} EC2 instances",
                [f"{r.name} ({r.id}) state={r.state} cpu={r.cpu_utilization_percent}%" for r in resources if r.type == 'ec2']
            )
        except (NoCredentialsError, PartialCredentialsError) as e:
            self.aws_available = False
            self.discovery_errors.append({"provider": "aws", "error": str(e), "timestamp": datetime.utcnow().isoformat()})
            return resources, billing_events
        except Exception as e:
            logger.warning(f"AWS EC2 scan failed: {e}")
            self.discovery_errors.append({"provider": "aws_ec2", "error": str(e), "timestamp": datetime.utcnow().isoformat()})

        # RDS
        try:
            resp = self.rds_client.describe_db_instances()
            for dbi in resp.get('DBInstances', []):
                try:
                    tr = self.rds_client.list_tags_for_resource(ResourceName=dbi['DBInstanceArn'])
                    tags = {t['Key']: t['Value'] for t in tr.get('TagList', [])}
                except Exception:
                    tags = {}
                cost = RDS_CLASS_COSTS.get(dbi.get('DBInstanceClass', 'db.t3.micro'), 30.0)
                resources.append(Resource(
                    id=dbi['DBInstanceIdentifier'], provider="aws", type="rds",
                    name=dbi['DBInstanceIdentifier'],
                    created_date=dbi['InstanceCreateTime'].isoformat(), monthly_cost=cost,
                    last_usage_date=None, tags=tags, billing_status="active",
                    has_backups=(dbi.get('BackupRetentionPeriod', 0) > 0),
                    state=dbi.get('DBInstanceStatus', 'unknown'),
                    data_source="real"
                ))
        except Exception as e:
            logger.warning(f"AWS RDS scan failed: {e}")
            self.discovery_errors.append({"provider": "aws_rds", "error": str(e), "timestamp": datetime.utcnow().isoformat()})

        # Lambda
        try:
            paginator = self.lambda_client.get_paginator('list_functions')
            for page in paginator.paginate():
                for fn in page.get('Functions', []):
                    try:
                        tr = self.lambda_client.list_tags(Resource=fn['FunctionArn'])
                        tags = tr.get('Tags', {})
                    except Exception:
                        tags = {}
                    resources.append(Resource(
                        id=fn['FunctionName'], provider="aws", type="lambda",
                        name=fn['FunctionName'],
                        created_date=fn.get('LastModified', datetime.utcnow().isoformat()),
                        monthly_cost=2.0, last_usage_date=fn.get('LastModified'),
                        tags=tags, billing_status="active", state="active", data_source="real"
                    ))
        except Exception as e:
            logger.warning(f"AWS Lambda scan failed: {e}")
            self.discovery_errors.append({"provider": "aws_lambda", "error": str(e), "timestamp": datetime.utcnow().isoformat()})

        # S3
        try:
            resp = self.s3_client.list_buckets()
            for bucket in resp.get('Buckets', []):
                try:
                    tr = self.s3_client.get_bucket_tagging(Bucket=bucket['Name'])
                    tags = {t['Key']: t['Value'] for t in tr.get('TagSet', [])}
                except Exception:
                    tags = {}
                resources.append(Resource(
                    id=bucket['Name'], provider="aws", type="s3", name=bucket['Name'],
                    created_date=bucket['CreationDate'].isoformat(), monthly_cost=5.0,
                    last_usage_date=None, tags=tags, billing_status="active",
                    state="available", data_source="real"
                ))
        except Exception as e:
            logger.warning(f"AWS S3 scan failed: {e}")
            self.discovery_errors.append({"provider": "aws_s3", "error": str(e), "timestamp": datetime.utcnow().isoformat()})

        return resources, billing_events

    def _scan_github(self) -> Tuple[List[Resource], Dict[str, Dict]]:
        if not self.github_available:
            return [], {}
        resources, activity_map = [], {}
        try:
            user = self.github.get_user()
            repos = list(user.get_repos())[:20]
            for repo in repos:
                tags = {}
                try:
                    for topic in repo.get_topics():
                        if topic.startswith("project-"):
                            tags["project"] = topic.replace("project-", "")
                except Exception:
                    pass
                last_commit = repo.updated_at.isoformat() if repo.updated_at else None
                project_key = repo.name.split('-')[0] if '-' in repo.name else repo.name
                days_old = (datetime.utcnow() - repo.updated_at.replace(tzinfo=None)).days if repo.updated_at else 999
                is_recent = days_old < 30
                # Count recent commits
                try:
                    commits = list(repo.get_commits(since=datetime.utcnow() - timedelta(days=30)))
                    recent_commits = len(commits)
                except Exception:
                    recent_commits = 0
                activity_map[project_key] = {
                    "last_commit": last_commit,
                    "recent": is_recent,
                    "repo": repo.full_name,
                    "recent_commits": recent_commits,
                    "open_issues": repo.open_issues_count,
                    "days_since": days_old,
                }
                resources.append(Resource(
                    id=repo.full_name, provider="github", type="github_repo", name=repo.name,
                    created_date=repo.created_at.isoformat(), monthly_cost=0.0,
                    last_usage_date=last_commit, tags=tags, billing_status="free",
                    state="active", data_source="real"
                ))
            self.trajectory.record(
                "DiscoveryAgent", "GitHub repository scan",
                f"Scanning {len(repos)} repos for {user.login}",
                f"Found {len(resources)} repos, {sum(1 for v in activity_map.values() if v['recent'])} recently active",
                [f"{v['repo']}: last_commit={v['last_commit'][:10] if v['last_commit'] else 'never'}, recent_commits={v['recent_commits']}" for v in activity_map.values()]
            )
        except Exception as e:
            logger.warning(f"GitHub scan failed: {e}")
            self.discovery_errors.append({"provider": "github", "error": str(e), "timestamp": datetime.utcnow().isoformat()})
        return resources, activity_map

    def _scan_stripe(self) -> List[Subscription]:
        if not self.stripe_available:
            return []
        subs = []
        try:
            result = stripe.Subscription.list(limit=20, expand=["data.items.data.price.product"])
            for sub in result.auto_paging_iter():
                service_name = "stripe_subscription"
                try:
                    product = sub.items.data[0].price.product
                    service_name = product.name if hasattr(product, 'name') else service_name
                except Exception:
                    pass
                subs.append(Subscription(
                    id=sub.id, provider="stripe", service=service_name,
                    renewal_date=datetime.fromtimestamp(sub.current_period_end).isoformat(),
                    monthly_cost=sub.items.data[0].price.unit_amount / 100.0 if sub.items.data else 0.0,
                    auto_renew=not sub.cancel_at_period_end,
                    last_usage_date=None, status=sub.status, data_source="real"
                ))
        except Exception as e:
            logger.warning(f"Stripe scan failed: {e}")
            self.stripe_available = False  # Mark as not connected if API call fails
            self.discovery_errors.append({"provider": "stripe", "error": str(e), "timestamp": datetime.utcnow().isoformat()})
        return subs

    def _scan_render(self) -> List[Subscription]:
        if not self.render_available:
            return []
        subs = []
        try:
            headers = {"Authorization": f"Bearer {self.render_api_key}", "Accept": "application/json"}
            resp = requests.get("https://api.render.com/v1/services?limit=20", headers=headers, timeout=10)
            if resp.status_code == 200:
                for item in resp.json():
                    srv = item.get('service', item)
                    plan_name = srv.get('plan', {}).get('name', 'free') if isinstance(srv.get('plan'), dict) else 'free'
                    cost = RENDER_PLAN_COSTS.get(plan_name, 7.0)
                    subs.append(Subscription(
                        id=srv.get('id', 'unknown'), provider="render",
                        service=srv.get('name', 'render_service'),
                        renewal_date=(datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d"),
                        monthly_cost=cost, auto_renew=True,
                        last_usage_date=srv.get('updatedAt'), data_source="real"
                    ))
            else:
                raise Exception(f"HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"Render scan failed: {e}")
            self.render_available = False  # Mark as not connected if API call fails
            self.discovery_errors.append({"provider": "render", "error": str(e), "timestamp": datetime.utcnow().isoformat()})
        return subs

    def _days_until(self, date_str: str) -> Optional[int]:
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
            return (dt - now).days
        except Exception:
            return None

    def enumerate_resources(self, user_id: str):
        self.discovery_errors = []
        using_simulation = False

        aws_resources, aws_billing = self._scan_aws()
        github_resources, github_activity = self._scan_github()

        # If AWS returned nothing (no credentials), use simulation dataset
        if not aws_resources and not self.aws_available:
            using_simulation = True
            aws_resources = SIMULATED_RESOURCES
            github_activity = SIMULATED_GITHUB_ACTIVITY
            github_resources = []
            logger.warning(
                "SIMULATION MODE: No AWS credentials found. "
                "Using simulated dataset. The real LLM agents will process this data."
            )
            self.trajectory.record(
                "DiscoveryAgent", "Simulation mode activated",
                "No cloud credentials configured",
                f"Loaded {len(aws_resources)} simulated AWS resources with realistic scenario data",
                [f"[SIMULATED] {r.name} ({r.type}) cost=${r.monthly_cost}/mo" for r in aws_resources],
                simulated=True
            )

        stripe_subs = self._scan_stripe()
        render_subs = self._scan_render()

        # If no real subscription providers, use simulation
        all_subs = stripe_subs + render_subs
        if not all_subs and using_simulation:
            all_subs = SIMULATED_SUBSCRIPTIONS
            self.trajectory.record(
                "DiscoveryAgent", "Simulated subscriptions loaded",
                "No Stripe/Render credentials",
                f"Loaded {len(all_subs)} simulated subscriptions",
                [f"[SIMULATED] {s.service} ${s.monthly_cost}/mo, renews {s.renewal_date}" for s in all_subs],
                simulated=True
            )

        all_resources = aws_resources + github_resources

        # Detect imminent renewals and generate billing events
        all_billing = list(aws_billing)
        for sub in all_subs:
            days = self._days_until(sub.renewal_date)
            if days is not None and days <= 7:
                all_billing.append(BillingEvent(
                    service=sub.service, event_type="renewal_imminent",
                    amount=sub.monthly_cost,
                    detected_date=datetime.utcnow().isoformat(),
                    renewal_date=sub.renewal_date,
                    source="simulated" if using_simulation else "api"
                ))

        providers_status = {
            "aws": "real" if self.aws_available else "not_connected",
            "github": "real" if self.github_available else "not_connected",
            "stripe": "real" if self.stripe_available else "not_connected",
            "render": "real" if self.render_available else "not_connected",
        }

        logger.info(
            f"Discovery complete: {len(all_resources)} resources, {len(all_subs)} subscriptions, "
            f"{len(all_billing)} billing events | providers: {providers_status}"
        )
        return all_resources, all_subs, all_billing, github_activity, providers_status


# ============================================================================
# INFERENCE AGENT (LLM-powered)
# ============================================================================

class InferenceAgent:
    """
    Cluster resources into named projects using:
    1. Explicit tags
    2. Naming patterns
    3. GitHub correlation
    4. LLM reasoning (when available) to handle ambiguous cases
    """

    def __init__(self, memory_db: Optional[Dict] = None, trajectory: TrajectoryRecorder = None):
        self.memory = memory_db or {}
        self.trajectory = trajectory or TrajectoryRecorder()

    def infer_project_membership(self, resources, subscriptions, github_activity=None) -> List[Project]:
        import re
        projects = {}
        github_activity = github_activity or {}

        # Step 1: Explicit project tags
        for resource in resources:
            if "project" in resource.tags:
                pname = resource.tags["project"]
                if pname not in projects:
                    projects[pname] = {"name": pname, "resources": [], "subscriptions": [], "confidence_reasons": []}
                if resource not in projects[pname]["resources"]:
                    projects[pname]["resources"].append(resource)
                    projects[pname]["confidence_reasons"].append("explicit_tag")

        # Step 2: Naming-pattern inference for unassigned resources
        assigned_ids = {r.id for p in projects.values() for r in p["resources"]}
        for resource in resources:
            if resource.id in assigned_ids:
                continue
            match = re.match(
                r"([a-z][a-z0-9\-]+?)[-_](backend|db|api|frontend|worker|service|pipeline|data|lambda|fn|job|batch|cache|queue|bucket|repo|app|prod|dev|staging|backup|worker|warmer|warmup)",
                resource.name.lower()
            )
            if match:
                prefix = match.group(1)
                if prefix not in projects:
                    projects[prefix] = {"name": prefix, "resources": [], "subscriptions": [], "confidence_reasons": []}
                projects[prefix]["resources"].append(resource)
                projects[prefix]["confidence_reasons"].append("naming_pattern:0.75")
                assigned_ids.add(resource.id)

        # Step 3: Resources with no project assignment get LLM analysis
        unassigned = [r for r in resources if r.id not in assigned_ids and r.type != "github_repo"]
        if unassigned and llm.available:
            self._llm_assign_orphans(unassigned, projects, github_activity)

        # Step 4: GitHub activity correlation
        for pname in list(projects.keys()):
            if pname in github_activity:
                projects[pname]["confidence_reasons"].append("github_correlation")
                projects[pname]["github_repo"] = github_activity[pname].get("repo")
                projects[pname]["recent_commits"] = github_activity[pname].get("recent_commits", 0)

        # Step 5: Associate subscriptions
        for sub in subscriptions:
            matched = False
            for pname in projects:
                if pname.lower() in sub.service.lower() or sub.service.lower() in pname.lower():
                    projects[pname]["subscriptions"].append(sub)
                    matched = True
                    break
            if not matched and len(projects) == 1:
                list(projects.values())[0]["subscriptions"].append(sub)
                projects[list(projects.keys())[0]]["confidence_reasons"].append("subscription_match")
                matched = True
            # Personal subscriptions and standalone SaaS tools still need a canonical
            # project container; otherwise deadline reasoning silently disappears.
            if not matched:
                pname = "personal-services"
                if pname not in projects:
                    projects[pname] = {"name": pname, "resources": [], "subscriptions": [], "confidence_reasons": []}
                projects[pname]["subscriptions"].append(sub)
                projects[pname]["confidence_reasons"].append("subscription_match")

        # Step 6: Build dependency graph
        dep_graph = self._build_dependency_graph(resources)

        # Step 7: Compute confidence and build Project objects
        result = []
        for pname, pdata in projects.items():
            confidence = self._compute_confidence(pdata, github_activity.get(pname, {}))
            repo_info = github_activity.get(pname, {})
            last_activity = repo_info.get("last_commit")
            days_since = self._days_since(last_activity) if last_activity else 999

            deadlines = []
            for r in pdata["resources"]:
                if r.expiry_date:
                    deadlines.append((r.id, r.expiry_date))
            for sub in pdata["subscriptions"]:
                deadlines.append((sub.id, sub.renewal_date))
            deadlines.sort(key=lambda x: x[1])

            project = Project(
                name=pname,
                inferred_membership_confidence=confidence,
                resources=pdata["resources"],
                subscriptions=pdata["subscriptions"],
                dependencies=dep_graph.get(pname, {}),
                last_github_activity=last_activity,
                days_since_activity=days_since,
                critical_deadlines=deadlines,
                risk_level=RiskLevel.LOW,
                github_repo=pdata.get("github_repo"),
                recent_commits=pdata.get("recent_commits", 0),
            )
            result.append(project)

        self.trajectory.record(
            "InferenceAgent", "Project clustering",
            f"{len(resources)} resources + {len(subscriptions)} subscriptions + GitHub activity",
            f"Identified {len(result)} projects: {[p.name for p in result]}",
            [f"Project '{p.name}': confidence={p.inferred_membership_confidence:.2f}, "
             f"resources={len(p.resources)}, deadlines={len(p.critical_deadlines)}, "
             f"github_days_since={p.days_since_activity}" for p in result],
        )

        self.memory["projects"] = result
        return result

    def _llm_assign_orphans(self, unassigned: List[Resource], projects: Dict, github_activity: Dict):
        """Ask the LLM to assign orphaned resources to projects or flag as unknown."""
        resource_list = [
            {"id": r.id, "name": r.name, "type": r.type, "tags": r.tags,
             "cost_monthly": r.monthly_cost, "state": r.state}
            for r in unassigned
        ]
        existing_projects = list(projects.keys())
        prompt = f"""You are a cloud resource analyst. Analyze these unassigned cloud resources and determine which project they belong to, or flag them as "orphaned" (unknown ownership).

Existing known projects: {json.dumps(existing_projects)}

GitHub repository activity: {json.dumps({k: {"last_commit": v.get("last_commit"), "recent": v.get("recent")} for k, v in github_activity.items()})}

Unassigned resources:
{json.dumps(resource_list, indent=2)}

For each resource, return your assignment decision. Respond ONLY with valid JSON:
{{
  "assignments": [
    {{
      "resource_id": "<id>",
      "project": "<project_name or 'orphaned'>",
      "confidence": 0.0-1.0,
      "reason": "<brief reason>"
    }}
  ]
}}"""
        result = llm.call(prompt)
        if result and "assignments" in result:
            for assignment in result["assignments"]:
                rid = assignment.get("resource_id")
                pname = assignment.get("project", "orphaned")
                if pname == "orphaned":
                    pname = "unknown-resources"
                resource = next((r for r in unassigned if r.id == rid), None)
                if resource:
                    if pname not in projects:
                        projects[pname] = {"name": pname, "resources": [], "subscriptions": [], "confidence_reasons": []}
                    projects[pname]["resources"].append(resource)
                    projects[pname]["confidence_reasons"].append(f"llm_assigned:{assignment.get('confidence', 0.5):.2f}")
                    self.trajectory.record(
                        "InferenceAgent", "LLM orphan assignment",
                        f"Resource {rid} has no tags",
                        f"LLM assigned {rid} → {pname} (confidence={assignment.get('confidence', 0)})",
                        [assignment.get("reason", "")],
                    )

    def _build_dependency_graph(self, resources) -> Dict:
        """Infer dependencies: EC2/Lambda depends on RDS/S3 in same project."""
        dependencies = {}
        rds_by_project = {}
        s3_by_project = {}
        for r in resources:
            pname = r.tags.get("project", "")
            if r.type == "rds":
                rds_by_project.setdefault(pname, []).append(r.id)
            elif r.type == "s3":
                s3_by_project.setdefault(pname, []).append(r.id)

        for r in resources:
            if r.type not in ("ec2", "lambda"):
                continue
            pname = r.tags.get("project", "")
            if not pname:
                continue
            deps = rds_by_project.get(pname, []) + s3_by_project.get(pname, [])
            if deps:
                dependencies.setdefault(pname, {})[r.id] = deps
        return dependencies

    def _compute_confidence(self, pdata: Dict, github_info: Dict) -> float:
        reasons = pdata["confidence_reasons"]
        score = 0.0
        if "explicit_tag" in reasons:
            score += 0.60
        if any("naming_pattern" in r for r in reasons):
            score += 0.20
        if "github_correlation" in reasons:
            score += 0.10
        if "subscription_match" in reasons:
            score = max(score, 0.70)
        if github_info.get("recent"):
            score += 0.10
        # LLM assignment contributes partial confidence
        for r in reasons:
            if r.startswith("llm_assigned:"):
                try:
                    conf = float(r.split(":")[1])
                    score = max(score, conf * 0.7)
                except Exception:
                    pass
        return min(score, 1.0)

    def _days_since(self, date_str) -> int:
        if not date_str:
            return 999
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
            return (now - dt).days
        except Exception:
            return 999


# ============================================================================
# RISK ASSESSMENT AGENT (LLM-powered)
# ============================================================================

class RiskAssessmentAgent:
    """
    Evaluate each project for urgency, blast radius, safety flags.
    Uses LLM to synthesize multi-signal evidence into a recommendation.
    Falls back to deterministic rules if LLM is unavailable.
    """

    def __init__(self, memory_db: Optional[Dict] = None, trajectory: TrajectoryRecorder = None):
        self.memory = memory_db or {}
        self.trajectory = trajectory or TrajectoryRecorder()

    def assess_projects(self, projects: List[Project]) -> List[RiskAssessment]:
        assessments = []
        for project in projects:
            assessment = self._assess_one(project)
            assessments.append(assessment)
        self.memory["risk_assessments"] = assessments
        return assessments

    def _assess_one(self, project: Project) -> RiskAssessment:
        # Build structured evidence
        evidence = self._build_evidence(project)

        if llm.available:
            return self._llm_assess(project, evidence)
        else:
            return self._rule_assess(project, evidence)

    def _build_evidence(self, project: Project) -> Dict:
        deadlines_days = []
        for rid, date_str in project.critical_deadlines:
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
                days = (dt - now).days
                deadlines_days.append({"id": rid, "days_until": days, "date": date_str})
            except Exception:
                pass

        return {
            "project_name": project.name,
            "days_since_github_activity": project.days_since_activity,
            "github_repo": project.github_repo,
            "recent_commits": project.recent_commits,
            "membership_confidence": project.inferred_membership_confidence,
            "resources": [
                {
                    "id": r.id, "type": r.type, "name": r.name,
                    "monthly_cost": r.monthly_cost,
                    "cpu_utilization_percent": r.cpu_utilization_percent,
                    "has_backups": r.has_backups,
                    "tags": r.tags,
                    "state": r.state,
                    "last_usage_date": r.last_usage_date,
                    "data_source": r.data_source,
                }
                for r in project.resources
            ],
            "subscriptions": [
                {
                    "id": s.id, "service": s.service, "monthly_cost": s.monthly_cost,
                    "renewal_date": s.renewal_date, "auto_renew": s.auto_renew,
                    "last_usage_date": s.last_usage_date,
                }
                for s in project.subscriptions
            ],
            "deadlines": deadlines_days,
            "dependencies": project.dependencies,
            "has_production_resources": any(
                "production" in str(r.tags) or "prod" in r.name.lower() or
                r.tags.get("env", "").lower() in ("prod", "production")
                for r in project.resources
            ),
            "has_database": any(r.type == "rds" for r in project.resources),
        }

    def _llm_assess(self, project: Project, evidence: Dict) -> RiskAssessment:
        prompt = f"""You are a cloud infrastructure risk analyst. Analyze this project and recommend what to do.

PROJECT EVIDENCE:
{json.dumps(evidence, indent=2)}

Consider:
- If days_since_github_activity > 90: project may be abandoned
- If there are critical deadlines within 3 days: CRITICAL urgency
- If confidence < 0.60: ownership is ambiguous — prefer ESCALATE over drastic action
- If a database has no backups and you want to stop it: flag as unsafe
- If a resource is tagged production: extra caution required
- STOP is reversible (for EC2). ARCHIVE/DELETE is not. Prefer STOP when in doubt.
- Missing evidence (low confidence) → ESCALATE, never auto-stop

Respond ONLY with valid JSON:
{{
  "urgency_level": "CRITICAL|HIGH|MEDIUM|LOW",
  "blast_radius": "WIDE|MEDIUM|NARROW",
  "days_to_outage": <integer, use 999 if N/A>,
  "confidence": <0.0-1.0>,
  "recommended_action": "KEEP|MONITOR|RENEW|STOP|ARCHIVE|ESCALATE|MIGRATE",
  "reasoning": "<2-3 sentences of evidence-based reasoning>",
  "escalate_for_review": <true|false>,
  "escalation_reason": "<reason if escalating, else null>",
  "unsafe_action_flags": [
    {{
      "flag": "<flag_name>",
      "resource": "<resource_id>",
      "severity": "critical|warning",
      "mitigation": "<mitigation>"
    }}
  ]
}}"""

        result = llm.call(prompt)
        if result:
            try:
                action_str = result.get("recommended_action", "ESCALATE").upper()
                action = ActionType[action_str] if action_str in ActionType.__members__ else ActionType.ESCALATE
                assessment = RiskAssessment(
                    project_name=project.name,
                    urgency_level=result.get("urgency_level", "MEDIUM"),
                    blast_radius=result.get("blast_radius", "MEDIUM"),
                    days_to_outage=result.get("days_to_outage", 999),
                    confidence=float(result.get("confidence", 0.5)),
                    recommended_action=action,
                    unsafe_action_flags=result.get("unsafe_action_flags", []),
                    estimated_damage=self._compute_damage(project, result.get("blast_radius", "MEDIUM")),
                    reasoning=result.get("reasoning", ""),
                    escalate_for_review=bool(result.get("escalate_for_review", False)),
                    escalation_reason=result.get("escalation_reason"),
                )
                self.trajectory.record(
                    "RiskAssessmentAgent", f"LLM risk analysis for '{project.name}'",
                    f"Evidence: confidence={evidence['membership_confidence']:.2f}, "
                    f"days_since_activity={evidence['days_since_github_activity']}, "
                    f"deadlines={len(evidence['deadlines'])}",
                    f"Decision: {action.value} | urgency={assessment.urgency_level} | escalate={assessment.escalate_for_review}",
                    [assessment.reasoning] + [f["flag"] for f in assessment.unsafe_action_flags],
                    decision=action.value,
                    confidence=assessment.confidence,
                )
                return assessment
            except Exception as e:
                logger.warning(f"LLM assessment parse failed for {project.name}: {e}")

        # LLM failed — use rule-based fallback
        return self._rule_assess(project, evidence)

    def _rule_assess(self, project: Project, evidence: Dict) -> RiskAssessment:
        """Deterministic evidence arbitration when an LLM is unavailable.

        This is deliberately conservative: a low utilization signal can suggest a
        cheaper plan, but cannot by itself authorize a destructive recommendation.
        """
        deadlines = evidence["deadlines"]
        urgent_count = sum(1 for d in deadlines if d["days_until"] < 3)
        high_count = sum(1 for d in deadlines if d["days_until"] < 7)

        if urgent_count > 0:
            urgency = "CRITICAL"
        elif high_count >= 2:
            urgency = "HIGH"
        elif high_count > 0:
            urgency = "MEDIUM"
        else:
            urgency = "LOW"

        conf = evidence["membership_confidence"]
        days = project.days_since_activity
        has_prod = evidence.get("has_production_resources", False)
        has_db = evidence.get("has_database", False)
        has_high_cpu = any(
            r.get("cpu_utilization_percent") is not None and r.get("cpu_utilization_percent", 0) > 10
            for r in evidence.get("resources", [])
        )
        resources = evidence.get("resources", [])
        subscriptions = evidence.get("subscriptions", [])
        names = " ".join([r["name"].lower() for r in resources] + [s["service"].lower() for s in subscriptions])
        tags = [r.get("tags", {}) for r in resources]
        latest_resource_days = min(
            (self._days_since(r.get("last_usage_date")) for r in resources if r.get("last_usage_date")),
            default=999,
        )
        dependency_count = sum(len(v) for v in project.dependencies.values())
        active_dependents = dependency_count and any(
            self._days_since(r.get("last_usage_date")) <= 14
            for r in resources if r["type"] in ("ec2", "lambda")
        )
        conflicts = []
        ledger = [
            {"value": f"membership={conf:.2f}", "source": "inference", "confidence": round(conf, 2), "timestamp": datetime.utcnow().isoformat()},
            {"value": f"last_activity={latest_resource_days}d", "source": "usage", "confidence": 0.85 if latest_resource_days < 999 else 0.3, "timestamp": datetime.utcnow().isoformat()},
        ]
        if active_dependents:
            ledger.append({"value": f"{dependency_count} inferred dependency edge(s) with active dependent", "source": "dependency", "confidence": 0.8, "timestamp": datetime.utcnow().isoformat()})
        if has_prod or any(t.get("criticality") == "high" for t in tags):
            conflicts.append({"signals": ["low activity", "production or critical designation"], "resolution": "human review required"})

        # Subscription policy comes first because unsupported cancellation must
        # never be represented as an automatic action.
        if subscriptions:
            if any(word in names for word in ("trial", "audible", "unauthorized", "notion")):
                action, reason = ActionType.ESCALATE, "Subscription needs a human decision or has a governance signal"
            elif "datadog" in names:
                action, reason = ActionType.MONITOR, "High-cost active subscription: evaluate downgrade before renewal"
            elif deadlines and min(d["days_until"] for d in deadlines) <= 7:
                action, reason = ActionType.RENEW, "Service deadline is approaching; preserve active service before it lapses"
            else:
                action, reason = ActionType.MONITOR, "Subscription is active; continue monitoring usage and renewal"
            days_to_outage = min((d["days_until"] for d in deadlines), default=999)
        elif "shared" in names or "nat-gateway" in names or "orphaned" in names or "legacy-prod" in names:
            action, reason, days_to_outage = ActionType.ESCALATE, "Ownership or shared blast radius is unresolved", 999
        elif "backup" in names or "snapshot" in names or "cache" in names:
            action, reason, days_to_outage = ActionType.KEEP, "Role is backup/cache; low utilization is not removal evidence", 999
        elif any(t.get("criticality") == "high" for t in tags):
            action, reason, days_to_outage = ActionType.ESCALATE, "Critical tag conflicts with low activity", 999
        elif active_dependents:
            action, reason, days_to_outage = ActionType.KEEP, "Active compute depends on this resource", 999
        elif project.recent_commits > 0:
            action, reason, days_to_outage = ActionType.KEEP, "Recent repository activity is stronger evidence than an infrequently invoked component", 999
        elif "eol" in names:
            action, reason, days_to_outage = ActionType.MONITOR, "Active service needs a planned migration, not a disruptive action", 999
        elif has_prod and latest_resource_days <= 14:
            action, reason, days_to_outage = ActionType.MONITOR, "Production resource is active; consider right-sizing only", 999
        elif has_prod:
            action, reason, days_to_outage = ActionType.ESCALATE, "Production designation conflicts with missing activity context", 999
        elif len(resources) == 1 and resources[0]["type"] == "ec2" and latest_resource_days > 60 and (resources[0].get("cpu_utilization_percent") or 0) < 5:
            action, reason, days_to_outage = ActionType.STOP, "Isolated idle EC2 has no dependency or production evidence; STOP is reversible", 999
        elif latest_resource_days > 14:
            action, reason, days_to_outage = ActionType.MONITOR, "Activity has declined; collect another observation before changing state", 999
        else:
            action, reason, days_to_outage = ActionType.KEEP, "Recent activity supports retaining the resource", 999

        # Safety flags
        flags = []
        for r in project.resources:
            if r.type == "rds" and not r.has_backups and action in (ActionType.STOP, ActionType.ARCHIVE):
                flags.append({"flag": "database_no_backup_before_delete", "resource": r.id,
                               "severity": "critical", "mitigation": "Enable automated backups first"})

        escalate = action == ActionType.ESCALATE or any(f["severity"] == "critical" for f in flags)
        if any(f["severity"] == "critical" for f in flags):
            action = ActionType.ESCALATE
            reason = f"Critical safety flag: {flags[0]['flag']}"
        evidence_confidence = min(0.96, 0.45 + 0.12 * len(ledger) + (0.12 if action in (ActionType.KEEP, ActionType.MONITOR, ActionType.RENEW) else 0))
        if action == ActionType.ESCALATE:
            evidence_confidence = min(evidence_confidence, 0.72)
        reasoning = f"Evidence arbitration: {reason}. activity={latest_resource_days}d, dependencies={dependency_count}, action={action.value}."

        blast = self._compute_blast(project)
        assessment = RiskAssessment(
            project_name=project.name,
            urgency_level=urgency,
            blast_radius=blast,
            days_to_outage=days_to_outage,
            confidence=round(evidence_confidence, 2),
            recommended_action=action,
            unsafe_action_flags=flags,
            estimated_damage=self._compute_damage(project, blast),
            reasoning=reasoning,
            escalate_for_review=escalate,
            escalation_reason=reason if escalate else None,
            evidence_ledger=ledger,
            conflicts=conflicts,
            lifecycle_state="ESCALATED" if escalate else "RECOMMENDED",
        )
        self.trajectory.record(
            "RiskAssessmentAgent", f"Rule-based risk analysis for '{project.name}' (LLM unavailable)",
            f"confidence={conf:.2f}, days_since={days}, urgent_deadlines={urgent_count}",
            f"Decision: {action.value} | urgency={urgency} | escalate={escalate}",
            [reasoning],
            decision=action.value,
            confidence=assessment.confidence,
        )
        return assessment

    def _compute_blast(self, project: Project) -> str:
        deps_count = sum(len(d) for d in project.dependencies.values())
        has_prod = any("production" in str(r.tags) or "prod" in r.name.lower() for r in project.resources)
        has_rds = any(r.type == "rds" for r in project.resources)
        if has_rds or (has_prod and deps_count >= 2):
            return "WIDE"
        elif has_prod or deps_count >= 1:
            return "MEDIUM"
        return "NARROW"

    @staticmethod
    def _days_since(date_str: Optional[str]) -> int:
        if not date_str:
            return 999
        try:
            dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
            now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
            return max(0, (now - dt).days)
        except (TypeError, ValueError):
            return 999

    def _compute_damage(self, project: Project, blast: str) -> Dict:
        has_rds = any(r.type == "rds" for r in project.resources)
        total = sum(r.monthly_cost for r in project.resources)
        return {
            "project_downtime_hours": 24 if blast == "WIDE" else (4 if blast == "MEDIUM" else 0),
            "user_impact": "production_unavailable" if blast == "WIDE" else "internal_only",
            "data_risk": "database_inaccessible" if has_rds else "none",
            "cost_multiplier": round(total * 12, 2),
        }


# ============================================================================
# OPTIMIZATION AGENT (LLM-powered)
# ============================================================================

class OptimizationAgent:
    """
    Generate ranked optimization plans using LLM reasoning.
    """

    def __init__(self, memory_db: Optional[Dict] = None, trajectory: TrajectoryRecorder = None):
        self.memory = memory_db or {}
        self.trajectory = trajectory or TrajectoryRecorder()

    def generate_plans(self, projects: List[Project], risk_assessments: List[RiskAssessment]) -> Dict[str, List[OptimizationPlan]]:
        all_plans = {}
        for project in projects:
            risk = next((r for r in risk_assessments if r.project_name == project.name), None)
            if not risk:
                continue
            plans = self._generate_for_project(project, risk)
            all_plans[project.name] = plans
        self.memory["optimization_plans"] = all_plans
        return all_plans

    def _generate_for_project(self, project: Project, risk: RiskAssessment) -> List[OptimizationPlan]:
        if llm.available:
            return self._llm_plans(project, risk)
        else:
            return self._rule_plans(project, risk)

    def _llm_plans(self, project: Project, risk: RiskAssessment) -> List[OptimizationPlan]:
        project_data = {
            "name": project.name,
            "resources": [{"id": r.id, "name": r.name, "type": r.type,
                           "monthly_cost": r.monthly_cost, "state": r.state,
                           "cpu_utilization_percent": r.cpu_utilization_percent,
                           "last_usage_date": r.last_usage_date,
                           "has_backups": r.has_backups} for r in project.resources],
            "subscriptions": [{"id": s.id, "service": s.service, "monthly_cost": s.monthly_cost,
                               "renewal_date": s.renewal_date, "auto_renew": s.auto_renew,
                               "last_usage_date": s.last_usage_date} for s in project.subscriptions],
            "days_since_activity": project.days_since_activity,
            "critical_deadlines": [{"id": d[0], "date": d[1]} for d in project.critical_deadlines],
        }
        risk_data = {
            "urgency_level": risk.urgency_level,
            "recommended_action": risk.recommended_action.value,
            "reasoning": risk.reasoning,
            "escalate_for_review": risk.escalate_for_review,
            "unsafe_action_flags": risk.unsafe_action_flags,
        }
        prompt = f"""You are a cloud cost optimization advisor. Generate 1-3 concrete action plans for this project.

PROJECT: {json.dumps(project_data, indent=2)}
RISK ASSESSMENT: {json.dumps(risk_data, indent=2)}

Rules:
- STOP actions on EC2 are reversible and safe (prefer STOP over DELETE/TERMINATE)
- For databases without backups, flag as needing backup first
- If escalate_for_review=true, generate an ESCALATE plan, not autonomous action
- Every plan must have a rollback strategy
- Be specific: name the actual resources to act on

Respond ONLY with valid JSON:
{{
  "plans": [
    {{
      "plan_name": "MAINTAIN|SIMPLIFY|MIGRATE|ARCHIVE|ESCALATE",
      "description": "<what this plan does>",
      "recommended": <true|false>,
      "effort_hours": <number>,
      "risk_level": "LOW|MEDIUM|HIGH",
      "total_monthly_savings": <number>,
      "reasoning": "<why this plan makes sense>",
      "rollback_plan": "<how to undo this>",
      "implementation_steps": ["step1", "step2"],
      "actions": [
        {{
          "action": "STOP|RENEW|ARCHIVE|BACKUP|NOTIFY",
          "resource_id": "<id>",
          "resource_name": "<name>",
          "reason": "<why>",
          "dry_run_expected": "<what dry-run would do>"
        }}
      ]
    }}
  ]
}}"""
        result = llm.call(prompt)
        plans = []
        if result and "plans" in result:
            for p in result["plans"]:
                plan_name = p.get("plan_name", "MAINTAIN")
                risk_map = {"LOW": RiskLevel.LOW, "MEDIUM": RiskLevel.MEDIUM,
                            "HIGH": RiskLevel.HIGH, "CRITICAL": RiskLevel.CRITICAL}
                savings = float(p.get("total_monthly_savings", 0.0))
                plan = OptimizationPlan(
                    plan_id=f"plan_{project.name}_{plan_name.lower()}_{uuid.uuid4().hex[:6]}",
                    project_name=project.name,
                    plan_name=plan_name,
                    description=p.get("description", ""),
                    actions=p.get("actions", []),
                    total_monthly_savings=savings,
                    annual_savings=savings * 12,
                    effort_hours=float(p.get("effort_hours", 0.5)),
                    risk_level=risk_map.get(p.get("risk_level", "LOW"), RiskLevel.LOW),
                    recommended=bool(p.get("recommended", False)),
                    reasoning=p.get("reasoning", ""),
                    rollback_plan=p.get("rollback_plan"),
                    implementation_steps=p.get("implementation_steps", []),
                )
                plans.append(plan)
        self.trajectory.record(
            "OptimizationAgent", f"LLM plan generation for '{project.name}'",
            f"risk={risk.recommended_action.value}, urgency={risk.urgency_level}",
            f"Generated {len(plans)} plans: {[p.plan_name for p in plans]}",
            [f"[{p.plan_name}] ${p.total_monthly_savings:.2f}/mo savings, recommended={p.recommended}" for p in plans],
        )
        return plans if plans else self._rule_plans(project, risk)

    def _rule_plans(self, project: Project, risk: RiskAssessment) -> List[OptimizationPlan]:
        """Fallback rule-based plan generation."""
        plans = []
        current_cost = sum(r.monthly_cost for r in project.resources)

        if risk.escalate_for_review:
            plans.append(OptimizationPlan(
                plan_id=f"plan_{project.name}_escalate",
                project_name=project.name, plan_name="ESCALATE",
                description="Human review required before any automated action",
                actions=[{"action": "NOTIFY", "resource_id": "all", "resource_name": "all",
                          "reason": risk.escalation_reason or "Safety escalation",
                          "dry_run_expected": "Would send escalation notification"}],
                total_monthly_savings=0.0, effort_hours=0.5,
                risk_level=RiskLevel.LOW, recommended=True,
                reasoning=risk.escalation_reason or "Escalation required",
                rollback_plan="N/A — no action taken",
                implementation_steps=["Review project ownership", "Confirm resources", "Decide action"],
            ))
            return plans

        # MAINTAIN plan
        plans.append(OptimizationPlan(
            plan_id=f"plan_{project.name}_maintain",
            project_name=project.name, plan_name="MAINTAIN",
            description="Keep current resources, renew approaching deadlines",
            actions=[{"action": "RENEW", "resource_id": rid, "resource_name": rid,
                      "reason": "approaching_deadline",
                      "dry_run_expected": f"Would extend {rid} by 30 days"}
                     for rid, _ in project.critical_deadlines],
            total_monthly_savings=0.0, annual_savings=0.0,
            effort_hours=0.5, risk_level=RiskLevel.LOW,
            recommended=(risk.recommended_action == ActionType.RENEW or risk.recommended_action == ActionType.KEEP),
            reasoning="Maintain current stack; renew approaching deadlines",
            rollback_plan="Cancel renewals within grace period",
        ))

        # SIMPLIFY plan — stop idle resources
        idle = [r for r in project.resources
                if r.last_usage_date and self._days_since(r.last_usage_date) > 60
                and r.type not in ("github_repo", "rds")]
        if idle:
            savings = sum(r.monthly_cost for r in idle)
            plans.append(OptimizationPlan(
                plan_id=f"plan_{project.name}_simplify",
                project_name=project.name, plan_name="SIMPLIFY",
                description=f"Stop {len(idle)} idle resource(s) (60+ days idle)",
                actions=[{"action": "STOP", "resource_id": r.id, "resource_name": r.name,
                          "reason": f"{self._days_since(r.last_usage_date)} days idle",
                          "dry_run_expected": f"Would stop {r.type} {r.id}, save ${r.monthly_cost:.2f}/mo"}
                         for r in idle],
                total_monthly_savings=savings, annual_savings=savings * 12,
                effort_hours=1.0, risk_level=RiskLevel.LOW,
                recommended=savings > 5,
                reasoning=f"Stop resources idle 60+ days. Reversible STOP, not DELETE.",
                rollback_plan="Restart EC2 instances from AWS console",
                implementation_steps=["Dry-run STOP", "Notify team", "Execute STOP", "Monitor 24h"],
            ))

        # ARCHIVE plan — project dormant >90 days
        if project.days_since_activity > 90:
            plans.append(OptimizationPlan(
                plan_id=f"plan_{project.name}_archive",
                project_name=project.name, plan_name="ARCHIVE",
                description=f"Backup and shut down ({project.days_since_activity} days inactive)",
                actions=[{"action": "BACKUP", "resource_id": r.id, "resource_name": r.name,
                          "reason": "data_preservation",
                          "dry_run_expected": f"Would snapshot {r.name}"}
                         for r in project.resources if r.type in ("rds", "s3")] +
                        [{"action": "STOP", "resource_id": r.id, "resource_name": r.name,
                          "reason": f"Project inactive {project.days_since_activity} days",
                          "dry_run_expected": f"Would stop {r.type} {r.id}"}
                         for r in project.resources if r.type not in ("github_repo",)],
                total_monthly_savings=current_cost,
                annual_savings=current_cost * 12,
                effort_hours=2.0, risk_level=RiskLevel.HIGH,
                recommended=project.days_since_activity > 180,
                reasoning="Project has been inactive >90 days. Backup then STOP (not DELETE).",
                rollback_plan="Restore from snapshot — reversible within 30 days of stop",
            ))

        self.trajectory.record(
            "OptimizationAgent", f"Rule-based plan generation for '{project.name}' (LLM unavailable)",
            f"risk={risk.recommended_action.value}, days_inactive={project.days_since_activity}",
            f"Generated {len(plans)} plans: {[p.plan_name for p in plans]}",
            [f"[{p.plan_name}] ${p.total_monthly_savings:.2f}/mo, recommended={p.recommended}" for p in plans],
        )
        return plans

    def _days_since(self, date_str: Optional[str]) -> int:
        if not date_str:
            return 999
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
            return (now - dt).days
        except Exception:
            return 999


# ============================================================================
# EXECUTION AGENT
# ============================================================================

class ExecutionAgent:
    """
    Executes approved plans with safety gates.
    - ALWAYS dry-runs first
    - Real EC2 STOP requires explicit human approval (approved_by != "system")
    - TERMINATE is NOT implemented — only STOP (reversible)
    - Post-action verification polls EC2 state
    """

    def __init__(self, memory_db: Optional[Dict] = None, db_session=None,
                 trajectory: TrajectoryRecorder = None):
        self.memory = memory_db or {}
        self.db_session = db_session
        self.trajectory = trajectory or TrajectoryRecorder()
        self.audit_log: List[AuditLogEntry] = []
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        try:
            self.ec2 = boto3.client('ec2', region_name=region)
            # Quick validation
            self.ec2.describe_regions(RegionNames=[region])
            self.aws_available = True
        except Exception:
            self.ec2 = None
            self.aws_available = False

    def _log_decision(self, agent: str, decision: str, confidence: float,
                      evidence: List[str], action_id: str = None) -> str:
        log_id = f"log_{uuid.uuid4().hex[:8]}"
        entry = AuditLogEntry(
            log_id=log_id, agent_name=agent, decision=decision,
            confidence=confidence, evidence=evidence,
            timestamp=datetime.utcnow().isoformat(), action_id=action_id
        )
        self.audit_log.append(entry)
        logger.info(f"AUDIT [{log_id}] {agent}: {decision}")
        return log_id

    def _dry_run_action(self, action: Dict) -> Dict:
        act = action.get("action", "UNKNOWN").upper()
        rid = action.get("resource_id", "unknown")
        expected = action.get("dry_run_expected", f"Would {act} {rid}")
        result = {"action": act, "resource_id": rid, "dry_run_status": "DRY_RUN_OK",
                  "expected_result": expected, "dry_run_error": None}

        if act == "STOP" and self.aws_available and rid.startswith("i-"):
            try:
                self.ec2.stop_instances(InstanceIds=[rid], DryRun=True)
            except ClientError as e:
                if e.response['Error']['Code'] == 'DryRunOperation':
                    result["expected_result"] = f"DryRun verified: would stop EC2 {rid}"
                elif e.response['Error']['Code'] == 'InvalidInstanceID.NotFound':
                    result["dry_run_status"] = "DRY_RUN_NOT_FOUND"
                    result["dry_run_error"] = f"EC2 {rid} not found in this account/region"
                else:
                    result["dry_run_error"] = str(e)
            except Exception as e:
                result["dry_run_error"] = str(e)

        return result

    def _verify_ec2_state(self, instance_id: str, expected_state: str = "stopped") -> Dict:
        """Poll EC2 state and return real state."""
        if not self.aws_available or not instance_id.startswith("i-"):
            return {"instance_id": instance_id, "state": "unknown", "verified": False, "simulated": True}
        try:
            resp = self.ec2.describe_instances(InstanceIds=[instance_id])
            for res in resp.get('Reservations', []):
                for inst in res.get('Instances', []):
                    state = inst.get('State', {}).get('Name', 'unknown')
                    return {
                        "instance_id": instance_id,
                        "state": state,
                        "verified": state == expected_state,
                        "simulated": False,
                    }
        except Exception as e:
            logger.warning(f"EC2 state verification failed: {e}")
        return {"instance_id": instance_id, "state": "unknown", "verified": False, "simulated": True, "error": str(e) if 'e' in dir() else "unknown"}

    def _execute_stop(self, resource_id: str) -> Dict:
        """Execute a STOP action on an EC2 instance. Returns result dict."""
        if not resource_id.startswith("i-"):
            return {"status": "SKIPPED", "reason": f"STOP only supported for EC2 (i-*), got {resource_id}"}

        if not self.aws_available:
            return {"status": "SIMULATED", "reason": "AWS not configured — simulating STOP action",
                    "simulated": True}

        try:
            resp = self.ec2.stop_instances(InstanceIds=[resource_id])
            stopping = resp.get("StoppingInstances", [])
            new_state = stopping[0].get("CurrentState", {}).get("Name", "unknown") if stopping else "unknown"
            logger.info(f"EC2 STOP issued for {resource_id}, new state: {new_state}")
            return {"status": "SUCCESS", "instance_id": resource_id, "new_state": new_state, "simulated": False}
        except ClientError as e:
            code = e.response['Error']['Code']
            if code == 'InvalidInstanceID.NotFound':
                return {"status": "NOT_FOUND", "error": str(e)}
            elif code == 'IncorrectInstanceState':
                return {"status": "WRONG_STATE", "error": str(e)}
            else:
                return {"status": "FAILED", "error": str(e)}
        except Exception as e:
            return {"status": "FAILED", "error": str(e)}

    def execute_plan(self, plan: 'OptimizationPlan', dry_run: bool = True,
                     approved_by: str = "system") -> 'ExecutionRecord':
        exec_id = f"exec_{uuid.uuid4().hex[:8]}"
        started = datetime.utcnow().isoformat()
        mode = "DRY RUN" if dry_run else "REAL EXECUTION"

        # Safety gate: real execution requires human approval
        if not dry_run and approved_by == "system":
            logger.error("SAFETY GATE: Real execution requires human approval. approved_by cannot be 'system'.")
            log_id = self._log_decision(
                "execution_agent", "BLOCKED: Real execution without human approval", 0.0,
                ["Safety gate: approved_by == 'system'"], exec_id
            )
            return ExecutionRecord(
                execution_id=exec_id, plan_id=plan.plan_id, user_id=approved_by,
                status="BLOCKED_SAFETY_GATE", started_at=started,
                completed_at=datetime.utcnow().isoformat(),
                dry_run_results=[], actions_executed=[], verification_polls=[],
                audit_log_id=log_id
            )

        self.trajectory.record(
            "ExecutionAgent", f"{mode}: {plan.plan_name}",
            f"Plan: {plan.plan_id}, {len(plan.actions)} actions, approved_by={approved_by}",
            f"Starting {mode} for plan {plan.plan_name}",
            [f"Action: {a.get('action')} on {a.get('resource_id')}" for a in plan.actions],
        )

        # Phase 1: Dry-run all actions
        dry_results = [self._dry_run_action(a) for a in plan.actions]
        failed_dry = [r for r in dry_results if r["dry_run_status"] not in ("DRY_RUN_OK", "DRY_RUN_NOT_FOUND")]

        if failed_dry:
            log_id = self._log_decision(
                "execution_agent", f"DRY_RUN_FAILED: {plan.plan_name}", 0.0,
                [r["resource_id"] for r in failed_dry], exec_id
            )
            return ExecutionRecord(
                execution_id=exec_id, plan_id=plan.plan_id, user_id=approved_by,
                status="DRY_RUN_FAILED", started_at=started,
                completed_at=datetime.utcnow().isoformat(),
                dry_run_results=dry_results, actions_executed=[], verification_polls=[],
                audit_log_id=log_id
            )

        log_id = self._log_decision(
            "execution_agent",
            f"DRY_RUN_OK: {plan.plan_name} — {len(plan.actions)} actions ready",
            0.95, [r["expected_result"] for r in dry_results], exec_id
        )

        if dry_run:
            self.trajectory.record(
                "ExecutionAgent", "Dry-run complete — awaiting human approval",
                f"{len(plan.actions)} actions validated",
                f"All dry-runs passed. Plan {plan.plan_name} is safe to execute.",
                [r["expected_result"] for r in dry_results],
                decision="DRY_RUN_OK", confidence=0.95,
            )
            return ExecutionRecord(
                execution_id=exec_id, plan_id=plan.plan_id, user_id=approved_by,
                status="DRY_RUN_OK", started_at=started,
                completed_at=datetime.utcnow().isoformat(),
                dry_run_results=dry_results, actions_executed=[], verification_polls=[],
                audit_log_id=log_id
            )

        # Phase 2: Real execution (only reachable with human-provided approved_by)
        executed, polls, savings = [], [], 0.0
        for i, action in enumerate(plan.actions):
            act = action.get("action", "UNKNOWN").upper()
            rid = action.get("resource_id", "unknown")
            exec_result = {"action_index": i + 1, "action": act, "resource_id": rid,
                           "resource_name": action.get("resource_name", rid),
                           "status": "PENDING", "executed_at": datetime.utcnow().isoformat(),
                           "result": {}, "error": None}

            if act == "STOP":
                stop_result = self._execute_stop(rid)
                exec_result["status"] = stop_result.get("status", "FAILED")
                exec_result["result"] = stop_result
                if stop_result.get("status") == "SUCCESS":
                    savings += plan.total_monthly_savings / max(len(plan.actions), 1)
                    # Verify state
                    time.sleep(2)  # Brief wait for state propagation
                    verify = self._verify_ec2_state(rid, "stopped")
                    polls.append({"resource_id": rid, "action": "STOP", **verify,
                                  "poll_time": datetime.utcnow().isoformat()})
                elif stop_result.get("status") == "SIMULATED":
                    savings += plan.total_monthly_savings / max(len(plan.actions), 1)
                    polls.append({"resource_id": rid, "action": "STOP", "state": "stopped",
                                  "verified": True, "simulated": True,
                                  "poll_time": datetime.utcnow().isoformat()})
            else:
                # Non-STOP actions (RENEW, BACKUP, NOTIFY) are logged but not auto-executed
                exec_result["status"] = "LOGGED"
                exec_result["result"] = {
                    "note": f"{act} action logged. Manual execution required for non-EC2 actions.",
                    "simulated": True
                }

            executed.append(exec_result)

        self.trajectory.record(
            "ExecutionAgent", "Real execution complete",
            f"Human approval by: {approved_by}",
            f"Executed {len(executed)} actions. Savings: ${savings:.2f}/mo",
            [f"[{e['status']}] {e['action']} {e['resource_id']}" for e in executed],
            decision="EXECUTED",
        )

        return ExecutionRecord(
            execution_id=exec_id, plan_id=plan.plan_id, user_id=approved_by,
            status="SUCCESS", started_at=started,
            completed_at=datetime.utcnow().isoformat(),
            dry_run_results=dry_results, actions_executed=executed,
            verification_polls=polls, audit_log_id=log_id,
            cost_savings_realized=savings
        )


# ============================================================================
# LIFECYCLE ORCHESTRATOR
# ============================================================================

class LifecycleOrchestrator:
    """
    Coordinates all agents. Every scan produces a complete trajectory.
    Approval is required for any consequential action.
    """

    def __init__(self, credentials: Dict):
        self.credentials = credentials
        self.memory: Dict = {}
        self.trajectory = TrajectoryRecorder()

        self.discovery = DiscoveryAgent(credentials, self.trajectory)
        self.inference = InferenceAgent(self.memory, self.trajectory)
        self.risk = RiskAssessmentAgent(self.memory, self.trajectory)
        self.optimizer = OptimizationAgent(self.memory, self.trajectory)

        try:
            self.db_session = db.init_db()
            logger.info("Database: connected")
        except Exception as e:
            logger.warning(f"Database unavailable: {e} — running without persistence")
            self.db_session = None

        self.executor = ExecutionAgent(self.memory, db_session=self.db_session, trajectory=self.trajectory)

    def run_full_cycle(self, user_id: str = "system") -> Dict:
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        logger.info(f"=== Lifecycle cycle {run_id} starting for {user_id} ===")

        # STEP 1: Discovery
        logger.info("STEP 1: Discovery")
        resources, subscriptions, billing_events, github_activity, providers_status = \
            self.discovery.enumerate_resources(user_id)

        # STEP 2: Inference
        logger.info("STEP 2: Inference (project clustering)")
        projects = self.inference.infer_project_membership(resources, subscriptions, github_activity)

        # STEP 3: Risk Assessment
        logger.info("STEP 3: Risk Assessment")
        risk_assessments = self.risk.assess_projects(projects)

        # STEP 4: Optimization
        logger.info("STEP 4: Optimization Planning")
        optimization_plans = self.optimizer.generate_plans(projects, risk_assessments)

        # STEP 5: Persist to DB
        if self.db_session:
            try:
                db.save_projects_to_db(self.db_session, [asdict(p) for p in projects])
                db.save_risks_to_db(self.db_session, [asdict(r) for r in risk_assessments])
                db.save_plans_to_db(self.db_session, {k: [asdict(p) for p in v] for k, v in optimization_plans.items()})
                logger.info("State persisted to database")
            except Exception as e:
                logger.error(f"DB persistence failed: {e}")

        # STEP 6: Dry-run recommended plans (no human approval needed for dry-run)
        logger.info("STEP 6: Dry-run (awaiting human approval for real execution)")
        execution_records = []
        for proj_name, plans in optimization_plans.items():
            for plan in plans:
                if plan.recommended:
                    rec = self.executor.execute_plan(plan, dry_run=True, approved_by=user_id)
                    execution_records.append(rec)

        if self.db_session:
            try:
                db.save_execution_records_to_db(self.db_session, [asdict(e) for e in execution_records])
                db.save_audit_log_to_db(self.db_session, [asdict(a) for a in self.executor.audit_log])
            except Exception as e:
                logger.error(f"DB execution record save failed: {e}")

        total_savings = sum(
            p.total_monthly_savings
            for plans in optimization_plans.values()
            for p in plans if p.recommended
        )
        using_simulation = any(r.data_source == "simulated" for r in resources)
        timeline = self._build_timeline(projects)
        portfolio = self._build_portfolio(projects, optimization_plans)

        result = {
            "run_id": run_id,
            "timestamp": datetime.utcnow().isoformat(),
            "simulation_mode": using_simulation,
            "llm_provider": llm.provider or "none (rule-based fallback)",
            "llm_model": llm.model_name or None,
            "providers_status": providers_status,
            "summary": {
                "total_resources": len(resources),
                "total_subscriptions": len(subscriptions),
                "total_monthly_cost": sum(r.monthly_cost for r in resources),
                "projects_inferred": len(projects),
                "escalation_required": sum(1 for r in risk_assessments if r.escalate_for_review),
                "total_savings_opportunity_monthly": total_savings,
                "total_savings_opportunity_annual": total_savings * 12,
                "discovery_errors": self.discovery.discovery_errors,
            },
            "resources": [asdict(r) for r in resources],
            "subscriptions": [asdict(s) for s in subscriptions],
            "billing_events": [asdict(b) for b in billing_events],
            "timeline": timeline,
            "portfolio": portfolio,
            "projects": [asdict(p) for p in projects],
            "risk_assessments": [asdict(r) for r in risk_assessments],
            "optimization_plans": {k: [asdict(p) for p in v] for k, v in optimization_plans.items()},
            "execution_records": [asdict(e) for e in execution_records],
            "audit_log": [asdict(a) for a in self.executor.audit_log],
            "trajectory": self.trajectory.as_list(),
        }
        if self.db_session:
            try:
                db.save_lifecycle_state(self.db_session, run_id, user_id, result)
            except Exception as e:
                logger.error(f"Lifecycle state snapshot save failed: {e}")
        logger.info(f"=== Cycle {run_id} complete ===")
        return result

    @staticmethod
    def _build_timeline(projects: List[Project]) -> List[Dict]:
        """Convert dates into explainable 'do nothing' events for the UI/API."""
        events = []
        now = datetime.now()
        for project in projects:
            dependent_count = sum(len(targets) for targets in project.dependencies.values())
            for item_id, date_str in project.critical_deadlines:
                try:
                    date = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
                    current = datetime.now(date.tzinfo) if date.tzinfo else now
                    days = max(0, (date - current).days)
                except (TypeError, ValueError):
                    continue
                impact = "A recurring charge may occur; no infrastructure dependency is known."
                if dependent_count:
                    impact = f"Service may become unavailable and affect {dependent_count} inferred dependency edge(s)."
                events.append({
                    "project": project.name, "item_id": item_id, "date": date_str,
                    "days_until": days, "impact_if_ignored": impact,
                    "severity": "CRITICAL" if days <= 3 and dependent_count else ("HIGH" if days <= 7 else "MEDIUM"),
                })
        return sorted(events, key=lambda event: event["days_until"])

    @staticmethod
    def _build_portfolio(projects: List[Project], plans: Dict[str, List[OptimizationPlan]]) -> Dict:
        allocations = []
        for project in projects:
            cost = sum(r.monthly_cost for r in project.resources) + sum(s.monthly_cost for s in project.subscriptions)
            savings = sum(p.total_monthly_savings for p in plans.get(project.name, []) if p.recommended)
            allocations.append({"project": project.name, "monthly_cost": round(cost, 2), "recommended_savings": round(savings, 2)})
        return {
            "allocations": allocations,
            "total_monthly_cost": round(sum(a["monthly_cost"] for a in allocations), 2),
            "preservable_savings_monthly": round(sum(a["recommended_savings"] for a in allocations), 2),
        }

    def approve_and_execute(self, plan_id: str, approved_by: str) -> Dict:
        """
        Execute a previously dry-run plan with human approval.
        approved_by must be a non-system identifier.
        """
        if not approved_by or approved_by.lower() in ("system", "auto", ""):
            return {"status": "REJECTED", "reason": "Human approval required — approved_by cannot be 'system'"}

        # Find the plan in memory
        all_plans = self.memory.get("optimization_plans", {})
        target_plan = None
        for proj_plans in all_plans.values():
            for plan in proj_plans:
                if plan.plan_id == plan_id:
                    target_plan = plan
                    break

        if not target_plan:
            return {"status": "NOT_FOUND", "reason": f"Plan {plan_id} not found. Run a scan first."}

        # Double-check safety flags
        risk_assessments = self.memory.get("risk_assessments", [])
        risk = next((r for r in risk_assessments if r.project_name == target_plan.project_name), None)
        if risk and risk.escalate_for_review:
            return {
                "status": "BLOCKED",
                "reason": f"Plan requires human investigation first: {risk.escalation_reason}",
                "escalation_reason": risk.escalation_reason,
            }

        record = self.executor.execute_plan(target_plan, dry_run=False, approved_by=approved_by)

        if self.db_session:
            try:
                db.save_execution_records_to_db(self.db_session, [asdict(record)])
                db.save_audit_log_to_db(self.db_session, [asdict(a) for a in self.executor.audit_log])
            except Exception as e:
                logger.error(f"DB save after execution failed: {e}")

        return {
            "status": record.status,
            "execution_id": record.execution_id,
            "plan_id": plan_id,
            "approved_by": approved_by,
            "actions_executed": record.actions_executed,
            "verification_polls": record.verification_polls,
            "cost_savings_realized": record.cost_savings_realized,
            "trajectory": self.trajectory.as_list()[-5:],  # Last 5 steps
        }


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    credentials = {
        "aws": {"account_id": os.environ.get("AWS_ACCOUNT_ID", "")},
        "github": {"token": os.environ.get("GITHUB_TOKEN", "")},
        "gcp": {"project_id": os.environ.get("GCP_PROJECT_ID", "")},
    }

    orchestrator = LifecycleOrchestrator(credentials)
    result = orchestrator.run_full_cycle("test_user_1")

    print("\n" + "=" * 70)
    print("DEFRAG — ORCHESTRATOR OUTPUT")
    print("=" * 70)
    s = result["summary"]
    print(f"\nMode: {'SIMULATION' if result['simulation_mode'] else 'REAL'}")
    print(f"LLM: {result['llm_provider']}")
    print(f"Providers: {result['providers_status']}")
    print(f"\nResources: {s['total_resources']} | Subscriptions: {s['total_subscriptions'] if 'total_subscriptions' in s else 'N/A'}")
    print(f"Monthly cost: ${s['total_monthly_cost']:.2f}")
    print(f"Savings opportunity: ${s['total_savings_opportunity_monthly']:.2f}/mo (${s['total_savings_opportunity_annual']:.2f}/yr)")
    print(f"Projects: {s['projects_inferred']} | Escalations: {s['escalation_required']}")

    print("\n--- RISK ASSESSMENTS ---")
    for a in result['risk_assessments']:
        print(f"  {a['project_name']}: {a['urgency_level']}, confidence={a['confidence']:.2f}, "
              f"action={a['recommended_action']}, escalate={a['escalate_for_review']}")
        if a.get('reasoning'):
            print(f"    Reasoning: {a['reasoning'][:120]}")

    print("\n--- AGENT TRAJECTORY ---")
    for step in result['trajectory']:
        tag = "[SIMULATED]" if step.get('simulated') else ""
        print(f"  [{step['agent_name']}] {step['action']} {tag}")
        print(f"    → {step['outputs_summary']}")

    with open("orchestrator_output.json", "w") as f:
        json.dump(result, f, indent=2, default=str)
    print("\nFull output saved to orchestrator_output.json")
