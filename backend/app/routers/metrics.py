from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import MetricVersion, RecomputeRun
from ..services.lineage import build_lineage
from ..services.metrics import publish_metrics

router = APIRouter(prefix="/api", tags=["指标版本与血缘"])


@router.post("/metrics/publish", status_code=201)
def post_publish(trigger: str = "manual", note: str = "",
                 as_of: datetime | None = None, db: Session = Depends(get_db)):
    versions = publish_metrics(db, trigger=trigger, as_of=as_of, note=note)
    db.commit()
    return {"published": len(versions),
            "version_nos": [v.version_no for v in versions]}


@router.get("/metrics")
def list_metrics(grain_type: str | None = None, grain_key: str | None = None,
                 latest_only: bool = True, db: Session = Depends(get_db)):
    stmt = select(MetricVersion).order_by(
        MetricVersion.metric_date, MetricVersion.grain_type,
        MetricVersion.grain_key, MetricVersion.version_no)
    rows = list(db.scalars(stmt).all())
    if latest_only:
        latest: dict[tuple, MetricVersion] = {}
        for m in rows:
            latest[(m.metric_date, m.grain_type, m.grain_key)] = m
        rows = list(latest.values())
    if grain_type:
        rows = [m for m in rows if m.grain_type == grain_type]
    if grain_key:
        rows = [m for m in rows if m.grain_key == grain_key]
    return [_metric_dict(m) for m in rows]


@router.get("/metrics/{metric_id}/versions")
def metric_versions(metric_id: int, db: Session = Depends(get_db)):
    target = db.get(MetricVersion, metric_id)
    if not target:
        return []
    rows = db.scalars(select(MetricVersion).where(
        MetricVersion.metric_date == target.metric_date,
        MetricVersion.grain_type == target.grain_type,
        MetricVersion.grain_key == target.grain_key).order_by(
        MetricVersion.version_no)).all()
    return [_metric_dict(m) for m in rows]


@router.get("/metrics/{metric_id}/lineage")
def metric_lineage(metric_id: int, db: Session = Depends(get_db)):
    return build_lineage(db, metric_id)


@router.get("/recompute-runs")
def recompute_runs(db: Session = Depends(get_db)):
    return [{"id": r.id, "run_uid": r.run_uid, "trigger": r.trigger,
             "status": r.status, "affected_keys": r.affected_keys,
             "note": r.note, "started_at": r.started_at.isoformat(),
             "finished_at": r.finished_at.isoformat() if r.finished_at else None}
            for r in db.scalars(select(RecomputeRun).order_by(RecomputeRun.id)).all()]


def _metric_dict(m: MetricVersion) -> dict:
    return {
        "id": m.id, "version_no": m.version_no,
        "metric_date": m.metric_date.isoformat(),
        "grain_type": m.grain_type, "grain_key": m.grain_key,
        "batch_code": m.batch_code,
        "planned_volume": m.planned_volume,
        "confirmed_volume": m.confirmed_volume,
        "pending_volume": m.pending_volume,
        "loss_lower": m.loss_lower, "loss_upper": m.loss_upper,
        "missing_rate": m.missing_rate,
        "over_allocation_risk": m.over_allocation_risk,
        "plan_completion_rate": m.plan_completion_rate,
        "status": m.status,
        "watermark": m.watermark.isoformat(),
        "stability_deadline": m.stability_deadline.isoformat(),
        "superseded_by": m.superseded_by, "diff_note": m.diff_note,
        "input_fact_ids": m.input_fact_ids,
        "recompute_run_id": m.recompute_run_id,
        "published_at": m.published_at.isoformat(),
    }
