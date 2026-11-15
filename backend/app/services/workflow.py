"""人工裁定与对账处置服务。"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Adjudication, QualityEvent, ReconciliationTask,
)


def decide_adjudication(db: Session, adjudication_id: int, decision: str,
                        rationale: str, operator: str) -> Adjudication:
    adj = db.get(Adjudication, adjudication_id)
    if not adj:
        raise ValueError("裁定记录不存在")
    if adj.decision != "pending":
        raise ValueError(f"该冲突已裁定为 {adj.decision}，不可重复裁定")
    adj.decision = decision
    adj.selected_evidence = decision.replace("accept_", "") if decision.startswith("accept_") else None
    adj.rationale = rationale
    adj.operator = operator
    adj.decided_at = datetime.now(timezone.utc)
    # 关联质量事件闭环
    if adj.quality_event_id:
        ev = db.get(QualityEvent, adj.quality_event_id)
        if ev:
            ev.status = "adjudicated"
    for ev in db.scalars(select(QualityEvent).where(
            QualityEvent.rule_code == "EVIDENCE_CONFLICT",
            QualityEvent.status == "open")).all():
        if ev.detail.get("device_id") == adj.device_id and \
                ev.interval_start == adj.interval_start:
            ev.status = "adjudicated"
    db.flush()
    return adj


def resolve_reconciliation(db: Session, task_id: int, resolution: str,
                           operator: str) -> ReconciliationTask:
    task = db.get(ReconciliationTask, task_id)
    if not task:
        raise ValueError("对账任务不存在")
    task.status = "resolved"
    task.resolution = f"[{operator}] {resolution}"
    task.resolved_at = datetime.now(timezone.utc)
    for ev in db.scalars(select(QualityEvent).where(
            QualityEvent.rule_code == "CHANNEL_IMBALANCE",
            QualityEvent.channel_code == task.channel_code,
            QualityEvent.status != "closed")).all():
        ev.status = "closed"
    db.flush()
    return task
