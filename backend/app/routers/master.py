from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    Channel, DeviceMapping, MeterChange, MeterPoint, Plot,
    PlotBatchAllocation, RotationBatch,
)
from ..schemas import (
    AllocationIn, BatchIn, ChannelIn, DeviceMappingIn, MeterChangeIn,
    MeterPointIn, PlotIn,
)
from ..services import master_data as md

router = APIRouter(prefix="/api/master", tags=["主数据与映射"])


@router.post("/channels", status_code=201)
def add_channel(d: ChannelIn, db: Session = Depends(get_db)):
    try:
        obj = md.create_channel(db, d); db.commit(); return {"id": obj.id, "code": obj.code}
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/meter-points", status_code=201)
def add_point(d: MeterPointIn, db: Session = Depends(get_db)):
    try:
        obj = md.create_meter_point(db, d); db.commit()
        return {"id": obj.id, "code": obj.code}
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/plots", status_code=201)
def add_plot(d: PlotIn, db: Session = Depends(get_db)):
    try:
        obj = md.create_plot(db, d); db.commit(); return {"id": obj.id, "code": obj.code}
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/batches", status_code=201)
def add_batch(d: BatchIn, db: Session = Depends(get_db)):
    try:
        obj = md.create_batch(db, d); db.commit(); return {"id": obj.id, "code": obj.code}
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/mapping/publish", status_code=201)
def publish(note: str = "映射发布", db: Session = Depends(get_db)):
    vno = md.publish_mapping_version(db, note); db.commit()
    return {"mapping_version": vno}


@router.post("/device-mappings", status_code=201)
def add_device_mapping(d: DeviceMappingIn, db: Session = Depends(get_db)):
    vno = md._next_mapping_version(db)
    # 先确保有版本容器
    if not db.scalar(select(md.MappingVersion).where(md.MappingVersion.version_no == vno)):
        md.publish_mapping_version(db, "自动版本")
    obj = md.upsert_device_mapping(db, d, vno); db.commit()
    return {"id": obj.id, "mapping_version": vno}


@router.post("/allocations", status_code=201)
def add_allocation(d: AllocationIn, db: Session = Depends(get_db)):
    vno = md._next_mapping_version(db)
    if not db.scalar(select(md.MappingVersion).where(md.MappingVersion.version_no == vno)):
        md.publish_mapping_version(db, "自动版本")
    obj = md.upsert_allocation(db, d, vno); db.commit()
    return {"id": obj.id, "mapping_version": vno}


@router.post("/meter-changes", status_code=201)
def add_meter_change(d: MeterChangeIn, db: Session = Depends(get_db)):
    try:
        obj = md.register_meter_change(db, d); db.commit()
        return {"id": obj.id, "status": obj.status}
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.get("/tree")
def tree(db: Session = Depends(get_db)):
    return {
        "channels": [{"code": c.code, "name": c.name, "parent_code": c.parent_code,
                      "level": c.level} for c in db.scalars(select(Channel))],
        "meter_points": [{"code": p.code, "name": p.name,
                          "channel_code": p.channel_code, "point_role": p.point_role}
                         for p in db.scalars(select(MeterPoint))],
        "plots": [{"code": p.code, "name": p.name, "area_mu": p.area_mu,
                   "channel_code": p.channel_code} for p in db.scalars(select(Plot))],
        "batches": [{"code": b.code, "name": b.name,
                     "planned_start": b.planned_start.isoformat(),
                     "planned_end": b.planned_end.isoformat()}
                    for b in db.scalars(select(RotationBatch))],
        "device_mappings": [{"device_id": m.device_id,
                             "meter_point_code": m.meter_point_code,
                             "effective_from": m.effective_from.isoformat(),
                             "effective_to": m.effective_to.isoformat()
                             if m.effective_to else None,
                             "mapping_version": m.mapping_version,
                             "change_reason": m.change_reason}
                            for m in db.scalars(select(DeviceMapping).order_by(
                                DeviceMapping.effective_from))],
        "allocations": [{"plot_code": a.plot_code, "batch_code": a.batch_code,
                         "planned_volume": a.planned_volume,
                         "effective_from": a.effective_from.isoformat()}
                        for a in db.scalars(select(PlotBatchAllocation))],
        "meter_changes": [{"id": c.id, "device_id": c.device_id,
                           "meter_point_code": c.meter_point_code,
                           "change_time": c.change_time.isoformat(),
                           "voucher_present": c.voucher_present,
                           "status": c.status, "note": c.note}
                          for c in db.scalars(select(MeterChange))],
    }
