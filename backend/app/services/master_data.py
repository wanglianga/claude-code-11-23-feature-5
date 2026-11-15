"""水系与地块主数据解析器。

- 渠道/计量点/地块/轮灌批次为基础主数据；
- 设备->计量点映射与地块->批次分配按 SCD2 保存生效区间；
- 每次发布映射生成自增 mapping_version 并留快照，供血缘回溯；
- 换表关系写入 meter_changes，缺证状态由事实编译器读取。
"""
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Adjudication, Channel, DeviceMapping, MappingVersion, MeterChange,
    MeterPoint, Plot, PlotBatchAllocation, QualityEvent, RotationBatch,
)
from ..schemas import (
    AllocationIn, BatchIn, ChannelIn, DeviceMappingIn, MeterChangeIn,
    MeterPointIn, PlotIn,
)


def _next_mapping_version(db: Session) -> int:
    last = db.scalar(select(MappingVersion.version_no).order_by(
        MappingVersion.version_no.desc()).limit(1))
    return (last or 0) + 1


def publish_mapping_version(db: Session, note: str) -> int:
    vno = _next_mapping_version(db)
    snapshot = {
        "device_mappings": [
            {"device_id": m.device_id, "meter_point_code": m.meter_point_code,
             "effective_from": m.effective_from.isoformat(),
             "effective_to": m.effective_to.isoformat() if m.effective_to else None}
            for m in db.scalars(select(DeviceMapping))],
        "allocations": [
            {"plot_code": a.plot_code, "batch_code": a.batch_code,
             "planned_volume": a.planned_volume}
            for a in db.scalars(select(PlotBatchAllocation))],
    }
    db.add(MappingVersion(version_no=vno, note=note, snapshot=snapshot))
    db.flush()
    return vno


# ---------------- 基础主数据 ----------------
def create_channel(db: Session, d: ChannelIn) -> Channel:
    if db.scalar(select(Channel).where(Channel.code == d.code)):
        raise ValueError(f"渠道 {d.code} 已存在")
    obj = Channel(**d.model_dump())
    db.add(obj); db.flush(); return obj


def create_meter_point(db: Session, d: MeterPointIn) -> MeterPoint:
    if db.scalar(select(MeterPoint).where(MeterPoint.code == d.code)):
        raise ValueError(f"计量点 {d.code} 已存在")
    obj = MeterPoint(**d.model_dump())
    db.add(obj); db.flush(); return obj


def create_plot(db: Session, d: PlotIn) -> Plot:
    if db.scalar(select(Plot).where(Plot.code == d.code)):
        raise ValueError(f"地块 {d.code} 已存在")
    obj = Plot(**d.model_dump())
    db.add(obj); db.flush(); return obj


def create_batch(db: Session, d: BatchIn) -> RotationBatch:
    if db.scalar(select(RotationBatch).where(RotationBatch.code == d.code)):
        raise ValueError(f"轮灌批次 {d.code} 已存在")
    obj = RotationBatch(**d.model_dump())
    db.add(obj); db.flush(); return obj


# ---------------- SCD2 映射 ----------------
def upsert_device_mapping(db: Session, d: DeviceMappingIn, mapping_version: int) -> DeviceMapping:
    """新映射生效时关闭同一设备的旧开放区间，不删除历史行。"""
    open_rows = db.scalars(select(DeviceMapping).where(
        DeviceMapping.device_id == d.device_id,
        DeviceMapping.effective_to.is_(None))).all()
    for row in open_rows:
        row.effective_to = d.effective_from
    obj = DeviceMapping(**d.model_dump(), mapping_version=mapping_version)
    db.add(obj); db.flush(); return obj


def upsert_allocation(db: Session, d: AllocationIn, mapping_version: int) -> PlotBatchAllocation:
    open_rows = db.scalars(select(PlotBatchAllocation).where(
        PlotBatchAllocation.plot_code == d.plot_code,
        PlotBatchAllocation.batch_code == d.batch_code,
        PlotBatchAllocation.effective_to.is_(None))).all()
    for row in open_rows:
        row.effective_to = d.effective_from
    obj = PlotBatchAllocation(**d.model_dump(), mapping_version=mapping_version)
    db.add(obj); db.flush(); return obj


def resolve_device(db: Session, device_id: str, at: datetime) -> DeviceMapping | None:
    """按事件时间解析设备当前所属计量点（SCD2 点查询）。"""
    return db.scalar(select(DeviceMapping).where(
        DeviceMapping.device_id == device_id,
        DeviceMapping.effective_from <= at,
        (DeviceMapping.effective_to.is_(None)) | (DeviceMapping.effective_to > at)))


def resolve_point(db: Session, meter_point_code: str) -> MeterPoint | None:
    return db.scalar(select(MeterPoint).where(MeterPoint.code == meter_point_code))


def allocations_for_batch(db: Session, batch_code: str, at: datetime) -> list[PlotBatchAllocation]:
    return list(db.scalars(select(PlotBatchAllocation).where(
        PlotBatchAllocation.batch_code == batch_code,
        PlotBatchAllocation.effective_from <= at,
        (PlotBatchAllocation.effective_to.is_(None)) |
        (PlotBatchAllocation.effective_to > at))).all())


def allocation_for_plot(db: Session, plot_code: str, at: datetime) -> PlotBatchAllocation | None:
    return db.scalar(select(PlotBatchAllocation).where(
        PlotBatchAllocation.plot_code == plot_code,
        PlotBatchAllocation.effective_from <= at,
        (PlotBatchAllocation.effective_to.is_(None)) |
        (PlotBatchAllocation.effective_to > at)))


# ---------------- 换表 ----------------
def register_meter_change(db: Session, d: MeterChangeIn) -> MeterChange:
    if db.scalar(select(MeterChange).where(
            MeterChange.device_id == d.device_id,
            MeterChange.change_time == d.change_time)):
        raise ValueError("该设备同一时间的换表记录已存在")
    status = "certified" if d.voucher_present else "missing_voucher"
    obj = MeterChange(**d.model_dump(), status=status)
    db.add(obj); db.flush()
    if not d.voucher_present:
        db.add(QualityEvent(
            event_uid=f"qe_{uuid.uuid4().hex[:16]}",
            rule_code="METER_CHANGE_NO_VOUCHER", severity="critical",
            device_id=d.device_id, meter_point_code=d.meter_point_code,
            interval_start=d.change_time,
            detail={"change_time": d.change_time.isoformat(),
                    "old_last_reading": d.old_last_reading,
                    "new_first_reading": d.new_first_reading,
                    "note": d.note},
            status="open"))
        db.flush()
    return obj


def meter_change_at(db: Session, device_id: str,
                    interval_start: datetime, interval_end: datetime) -> MeterChange | None:
    """换表时间落在 [start, end) 区间内则返回。"""
    return db.scalar(select(MeterChange).where(
        MeterChange.device_id == device_id,
        MeterChange.change_time >= interval_start,
        MeterChange.change_time < interval_end))


def open_adjudication_for(db: Session, device_id: str,
                          start: datetime, end: datetime) -> Adjudication | None:
    return db.scalar(select(Adjudication).where(
        Adjudication.device_id == device_id,
        Adjudication.interval_start == start,
        Adjudication.interval_end == end))
