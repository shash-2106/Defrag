"""
Database models and helpers for Lifecycle Orchestrator state persistence.
Primary: PostgreSQL via SQLAlchemy ORM (configured by DATABASE_URL env var).
Fallback: SQLite (defrag.db in project root) — no extra setup required.
"""

import os
import json
from sqlalchemy import create_engine, Column, String, Float, Integer, JSON, Boolean, Text
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

# Read from env var; default to local SQLite so the app works out of the box
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "sqlite:///./defrag.db"
)


class ProjectModel(Base):
    __tablename__ = "projects"
    name = Column(String, primary_key=True)
    inferred_membership_confidence = Column(Float)
    resources = Column(JSON)
    subscriptions = Column(JSON)
    dependencies = Column(JSON)
    last_github_activity = Column(String, nullable=True)
    days_since_activity = Column(Integer)
    critical_deadlines = Column(JSON)
    risk_level = Column(String)


class RiskAssessmentModel(Base):
    __tablename__ = "risk_assessments"
    project_name = Column(String, primary_key=True)
    urgency_level = Column(String)
    blast_radius = Column(String)
    days_to_outage = Column(Integer)
    confidence = Column(Float)
    recommended_action = Column(String)
    unsafe_action_flags = Column(JSON)
    estimated_damage = Column(JSON)
    escalate_for_review = Column(Boolean, default=False)
    escalation_reason = Column(String, nullable=True)


class OptimizationPlanModel(Base):
    __tablename__ = "optimization_plans"
    plan_id = Column(String, primary_key=True)
    project_name = Column(String, index=True)
    plan_name = Column(String)
    description = Column(Text)
    actions = Column(JSON)
    total_monthly_savings = Column(Float)
    annual_savings = Column(Float)
    effort_hours = Column(Float)
    risk_level = Column(String)
    recommended = Column(Boolean)
    rollback_plan = Column(Text, nullable=True)


class ExecutionRecordModel(Base):
    __tablename__ = "execution_records"
    execution_id = Column(String, primary_key=True)
    plan_id = Column(String, index=True)
    user_id = Column(String)
    status = Column(String)
    started_at = Column(String)
    completed_at = Column(String, nullable=True)
    dry_run_results = Column(JSON)
    actions_executed = Column(JSON)
    verification_polls = Column(JSON)
    audit_log_id = Column(String)
    cost_savings_realized = Column(Float, default=0.0)


class AuditLogModel(Base):
    __tablename__ = "audit_log"
    log_id = Column(String, primary_key=True)
    agent_name = Column(String)
    decision = Column(Text)
    confidence = Column(Float)
    evidence = Column(JSON)
    timestamp = Column(String)
    action_id = Column(String, nullable=True)


class LifecycleStateModel(Base):
    """Append-only canonical snapshot for observations, decisions, and outcomes."""
    __tablename__ = "lifecycle_state"
    run_id = Column(String, primary_key=True)
    user_id = Column(String, index=True)
    created_at = Column(String)
    state = Column(JSON)


def init_db(database_url: str = DATABASE_URL):
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return SessionLocal()


def _serialize(obj):
    """Recursively convert Enums and dataclasses to JSON-safe types."""
    if hasattr(obj, 'value'):
        return obj.value
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    return obj


def save_projects_to_db(db_session, projects_list):
    for p in projects_list:
        model = ProjectModel(
            name=p['name'],
            inferred_membership_confidence=p['inferred_membership_confidence'],
            resources=_serialize(p.get('resources', [])),
            subscriptions=_serialize(p.get('subscriptions', [])),
            dependencies=_serialize(p.get('dependencies', {})),
            last_github_activity=p.get('last_github_activity'),
            days_since_activity=p.get('days_since_activity', 0),
            critical_deadlines=_serialize(p.get('critical_deadlines', [])),
            risk_level=p['risk_level'].value if hasattr(p.get('risk_level'), 'value') else str(p.get('risk_level'))
        )
        db_session.merge(model)
    db_session.commit()


def save_risks_to_db(db_session, risks_list):
    for r in risks_list:
        model = RiskAssessmentModel(
            project_name=r['project_name'],
            urgency_level=r['urgency_level'],
            blast_radius=r['blast_radius'],
            days_to_outage=r['days_to_outage'],
            confidence=r['confidence'],
            recommended_action=r['recommended_action'].value if hasattr(r['recommended_action'], 'value') else str(r['recommended_action']),
            unsafe_action_flags=_serialize(r.get('unsafe_action_flags', [])),
            estimated_damage=_serialize(r.get('estimated_damage', {})),
            escalate_for_review=r.get('escalate_for_review', False),
            escalation_reason=r.get('escalation_reason')
        )
        db_session.merge(model)
    db_session.commit()


def save_plans_to_db(db_session, plans_dict):
    for proj_name, plans in plans_dict.items():
        for p in plans:
            model = OptimizationPlanModel(
                plan_id=p['plan_id'],
                project_name=proj_name,
                plan_name=p['plan_name'],
                description=p['description'],
                actions=_serialize(p.get('actions', [])),
                total_monthly_savings=p['total_monthly_savings'],
                annual_savings=p.get('annual_savings', p['total_monthly_savings'] * 12),
                effort_hours=p['effort_hours'],
                risk_level=p['risk_level'].value if hasattr(p.get('risk_level'), 'value') else str(p.get('risk_level')),
                recommended=p['recommended'],
                rollback_plan=p.get('rollback_plan')
            )
            db_session.merge(model)
    db_session.commit()


def save_execution_records_to_db(db_session, records_list):
    for r in records_list:
        model = ExecutionRecordModel(
            execution_id=r['execution_id'],
            plan_id=r['plan_id'],
            user_id=r['user_id'],
            status=r['status'],
            started_at=r['started_at'],
            completed_at=r.get('completed_at'),
            dry_run_results=r.get('dry_run_results', []),
            actions_executed=r.get('actions_executed', []),
            verification_polls=r.get('verification_polls', []),
            audit_log_id=r['audit_log_id'],
            cost_savings_realized=r.get('cost_savings_realized', 0.0)
        )
        db_session.merge(model)
    db_session.commit()


def save_audit_log_to_db(db_session, audit_log_list):
    for entry in audit_log_list:
        model = AuditLogModel(
            log_id=entry['log_id'],
            agent_name=entry['agent_name'],
            decision=entry['decision'],
            confidence=entry['confidence'],
            evidence=entry.get('evidence', []),
            timestamp=entry['timestamp'],
            action_id=entry.get('action_id')
        )
        db_session.merge(model)
    db_session.commit()


def save_lifecycle_state(db_session, run_id, user_id, state):
    """Persist a run snapshot without mutating prior decisions or observations."""
    db_session.merge(LifecycleStateModel(
        run_id=run_id,
        user_id=user_id,
        created_at=state.get("timestamp"),
        state=_serialize(state),
    ))
    db_session.commit()
