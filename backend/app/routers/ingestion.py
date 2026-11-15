import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ManualRevision, RawMessage
from ..schemas import (
    IngestResult, ManualRevisionIn, RawMessageIn,
)
from ..services.ingestion import ingest_manual, ingest_raw

router = APIRouter(prefix="/api/ingestion", tags=["接入暂存区"])


@router.post("/raw", response_model=IngestResult, status_code=201)
def post_raw(data: RawMessageIn, db: Session = Depends(get_db)):
    result = ingest_raw(db, data, batch=f"batch_{uuid.uuid4().hex[:10]}")
    db.commit()
    return _result_dict(result)


@router.post("/manual", response_model=IngestResult, status_code=201)
def post_manual(data: ManualRevisionIn, db: Session = Depends(get_db)):
    result = ingest_manual(db, data, batch=f"batch_{uuid.uuid4().hex[:10]}")
    db.commit()
    return _result_dict(result)


def _result_dict(result) -> dict:
    return {
        "accepted": result.accepted, "stored_uid": result.stored_uid,
        "is_late": result.is_late, "revision_no": result.revision_no,
        "violations": result.violations, "message": result.message,
    }


@router.get("/raw")
def list_raw(device_id: str | None = None, db: Session = Depends(get_db)):
    stmt = select(RawMessage).order_by(RawMessage.event_time, RawMessage.revision_no)
    if device_id:
        stmt = stmt.where(RawMessage.device_id == device_id)
    return [_raw_dict(m) for m in db.scalars(stmt).all()]


@router.get("/manual")
def list_manual(device_id: str | None = None, db: Session = Depends(get_db)):
    stmt = select(ManualRevision).order_by(ManualRevision.event_time)
    if device_id:
        stmt = stmt.where(ManualRevision.device_id == device_id)
    return [_manual_dict(m) for m in db.scalars(stmt).all()]


def _raw_dict(m: RawMessage) -> dict:
    return {
        "id": m.id, "message_uid": m.message_uid, "contract_code": m.contract_code,
        "device_id": m.device_id, "payload": m.payload,
        "event_time": m.event_time.isoformat(), "received_at": m.received_at.isoformat(),
        "source_version": m.source_version, "ingestion_batch": m.ingestion_batch,
        "supersedes_uid": m.supersedes_uid, "revision_no": m.revision_no,
        "is_late": m.is_late,
    }


def _manual_dict(m: ManualRevision) -> dict:
    return {
        "id": m.id, "revision_uid": m.revision_uid, "contract_code": m.contract_code,
        "device_id": m.device_id, "event_time": m.event_time.isoformat(),
        "value": m.value, "unit": m.unit, "basis": m.basis, "operator": m.operator,
        "received_at": m.received_at.isoformat(), "supersedes_uid": m.supersedes_uid,
    }
