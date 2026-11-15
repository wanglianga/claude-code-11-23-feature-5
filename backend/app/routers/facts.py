from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    Adjudication, DataContract, QualityEvent, ReconciliationTask, SupplyFact,
)
from ..schemas import AdjudicationIn, ReconciliationResolveIn
from ..services import master_data as md
from ..services.fact_compiler import compile_device, compile_plan_facts
from ..services.quality import run_all_rules
from ..services.workflow import decide_adjudication, resolve_reconciliation

router = APIRouter(prefix="/api", tags=["事实编译与质量"])


@router.post("/compile", status_code=201)
def compile_all(db: Session = Depends(get_db)):
    contracts = db.scalars(select(DataContract)).all()
    device_ids = sorted({c for (c,) in db.execute(
        select(SupplyFact.device_id)).all() if c})
    # 编译对象来自暂存区出现过的设备
    from ..models import RawMessage
    device_ids = sorted({r[0] for r in db.execute(
        select(RawMessage.device_id).distinct()).all()})
    total = 0
    per_device = {}
    for dev in device_ids:
        contract = db.scalar(select(DataContract).where(
            DataContract.code == db.scalar(
                select(RawMessage.contract_code).where(
                    RawMessage.device_id == dev).limit(1))))
        rtype = contract.reading_type if contract else "cumulative"
        facts = compile_device(db, dev, rtype)
        per_device[dev] = len(facts)
        total += len(facts)
    plan_facts = compile_plan_facts(db)
    rules = run_all_rules(db)
    db.commit()
    return {"supply_facts_written": total, "plan_facts_written": len(plan_facts),
            "per_device": per_device, "quality": rules}


@router.get("/facts")
def list_facts(status: str | None = None, grain: str | None = None,
               db: Session = Depends(get_db)):
    stmt = select(SupplyFact).order_by(SupplyFact.interval_start, SupplyFact.id)
    rows = list(db.scalars(stmt).all())
    from ..services.ledger import superseded_fact_ids
    dead = superseded_fact_ids(db)
    out = []
    for f in rows:
        if f.id in dead:
            continue
        if status and f.status != status:
            continue
        if grain == "plot" and not f.plot_code:
            continue
        if grain == "channel" and not f.channel_code:
            continue
        out.append({
            "id": f.id, "fact_uid": f.fact_uid, "fact_kind": f.fact_kind,
            "status": f.status, "device_id": f.device_id,
            "meter_point_code": f.meter_point_code, "channel_code": f.channel_code,
            "plot_code": f.plot_code, "batch_code": f.batch_code,
            "interval_start": f.interval_start.isoformat(),
            "interval_end": f.interval_end.isoformat(),
            "reading_start": f.reading_start, "reading_end": f.reading_end,
            "value": f.value, "unit": f.unit, "method": f.method,
            "confidence": f.confidence, "truncated": f.truncated,
            "is_late": f.is_late, "quality_flag": f.quality_flag,
            "evidence_message_ids": f.evidence_message_ids,
            "evidence_manual_ids": f.evidence_manual_ids,
            "mapping_version": f.mapping_version, "revision_no": f.revision_no,
            "meter_change_id": f.meter_change_id,
            "adjudication_id": f.adjudication_id,
        })
    return out


@router.get("/quality/events")
def quality_events(status: str | None = None, db: Session = Depends(get_db)):
    stmt = select(QualityEvent).order_by(QualityEvent.id.desc())
    if status:
        stmt = stmt.where(QualityEvent.status == status)
    return [{"id": e.id, "event_uid": e.event_uid, "rule_code": e.rule_code,
             "severity": e.severity, "device_id": e.device_id,
             "meter_point_code": e.meter_point_code, "channel_code": e.channel_code,
             "interval_start": e.interval_start.isoformat() if e.interval_start else None,
             "interval_end": e.interval_end.isoformat() if e.interval_end else None,
             "status": e.status, "detail": e.detail,
             "related_fact_ids": e.related_fact_ids}
            for e in db.scalars(stmt).all()]


@router.get("/adjudications")
def list_adjudications(db: Session = Depends(get_db)):
    return [{"id": a.id, "conflict_key": a.conflict_key, "device_id": a.device_id,
             "interval_start": a.interval_start.isoformat(),
             "interval_end": a.interval_end.isoformat(), "decision": a.decision,
             "selected_evidence": a.selected_evidence, "rationale": a.rationale,
             "operator": a.operator}
            for a in db.scalars(select(Adjudication).order_by(Adjudication.id)).all()]


@router.post("/adjudications/{adjudication_id}", status_code=200)
def post_adjudication(adjudication_id: int, d: AdjudicationIn,
                      db: Session = Depends(get_db)):
    try:
        adj = decide_adjudication(db, adjudication_id, d.decision, d.rationale, d.operator)
        db.commit()
        return {"id": adj.id, "decision": adj.decision}
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.get("/reconciliations")
def list_reconciliations(db: Session = Depends(get_db)):
    return [{"id": t.id, "task_uid": t.task_uid, "channel_code": t.channel_code,
             "interval_start": t.interval_start.isoformat(),
             "interval_end": t.interval_end.isoformat(),
             "upstream_value": t.upstream_value,
             "downstream_explained": t.downstream_explained,
             "diff": t.diff, "diff_ratio": t.diff_ratio, "status": t.status,
             "frozen_metric_keys": t.frozen_metric_keys, "resolution": t.resolution}
            for t in db.scalars(select(ReconciliationTask).order_by(ReconciliationTask.id)).all()]


@router.post("/reconciliations/{task_id}/resolve", status_code=200)
def post_resolve(task_id: int, d: ReconciliationResolveIn,
                 db: Session = Depends(get_db)):
    try:
        task = resolve_reconciliation(db, task_id, d.resolution, d.operator)
        db.commit()
        return {"id": task.id, "status": task.status}
    except ValueError as e:
        raise HTTPException(409, str(e))
