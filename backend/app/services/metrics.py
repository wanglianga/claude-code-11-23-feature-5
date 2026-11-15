"""指标版本服务。

按 事件时间 + 水位线 + 发布稳定期 计算并版本化发布：
- 计划水量 planned_volume（仅来自 target 事实）
- 可确认实灌量 confirmed_volume（仅 confirmed 事实；candidate 未裁定不进正式指标）
- 待核实量 pending_volume（pending_verification，含换表缺证截断关联量）
- 损耗区间 [loss_lower, loss_upper]（待核实带来不确定性；渠道冻结时置空）
- 缺测率 missing_rate（GAP_MISSING 折算缺失采集槽位占比）
- 超配风险 over_allocation_risk、计划完成率 plan_completion_rate

每次发布为每个 (日期, 粒度, 键) 追加新 MetricVersion，旧版本 superseded_by 指向新版本，
并写差异说明 diff_note。渠道存在开放/冻结对账任务时，该渠道损耗指标冻结且不得用计划量补平。
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    DataContract, MetricVersion, QualityEvent, RawMessage, ReconciliationTask,
    RecomputeRun, SupplyFact,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _active_facts(db: Session) -> list[SupplyFact]:
    from .ledger import active_facts
    return active_facts(db)


def _day(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day, tzinfo=dt.tzinfo or timezone.utc)


def _next_version_no(db: Session) -> int:
    last = db.scalar(select(MetricVersion.version_no).order_by(
        MetricVersion.version_no.desc()).limit(1))
    return (last or 0) + 1


def _missing_rate(db: Session, device_ids: list[str], day_start: datetime,
                  day_end: datetime) -> float:
    if not device_ids:
        return 0.0
    events = db.scalars(select(QualityEvent).where(
        QualityEvent.rule_code == "GAP_MISSING",
        QualityEvent.device_id.in_(device_ids),
        QualityEvent.interval_start >= day_start,
        QualityEvent.interval_start < day_end)).all()
    missing_slots = 0.0
    for ev in events:
        contract = db.scalar(select(DataContract).where(
            DataContract.code == db.scalar(
                select(RawMessage.contract_code).where(
                    RawMessage.device_id == ev.device_id).limit(1))))
        base = contract.collection_interval_sec if contract else 3600
        gap = ev.detail.get("gap_minutes", 0) * 60
        missing_slots += max(gap / base - 1.0, 0.0)
    expected = 0.0
    for dev in set(device_ids):
        contract_code = db.scalar(select(RawMessage.contract_code).where(
            RawMessage.device_id == dev).limit(1))
        contract = db.scalar(select(DataContract).where(
            DataContract.code == contract_code)) if contract_code else None
        base = contract.collection_interval_sec if contract else 3600
        expected += (day_end - day_start).total_seconds() / base
    if expected <= 0:
        return 0.0
    return round(min(missing_slots / expected, 1.0), 4)


def _diff_note(prev: MetricVersion | None, cur: dict) -> str:
    if prev is None:
        return "首次发布"
    parts = []
    for key, label in [("planned_volume", "计划水量"), ("confirmed_volume", "确认实灌量"),
                       ("pending_volume", "待核实量"), ("missing_rate", "缺测率")]:
        old, new = getattr(prev, key), cur[key]
        if old is None or abs((new or 0) - (old or 0)) > 1e-9:
            parts.append(f"{label}: {old} -> {new}")
    if prev.status != cur["status"]:
        parts.append(f"状态: {prev.status} -> {cur['status']}")
    return "；".join(parts) if parts else "无变化重发"


def _frozen_channels(db: Session) -> set[str]:
    rows = db.scalars(select(ReconciliationTask.channel_code).where(
        ReconciliationTask.status.in_(["open", "frozen"]))).all()
    return set(rows)


def publish_metrics(db: Session, trigger: str = "scheduled",
                    as_of: datetime | None = None, note: str = "") -> list[MetricVersion]:
    as_of = as_of or _now()
    watermark = as_of - timedelta(minutes=settings.allowed_lateness_minutes)
    stability_deadline = as_of + timedelta(minutes=settings.stability_minutes)
    facts = _active_facts(db)
    frozen = _frozen_channels(db)
    from ..models import MeterPoint
    point_role = {p.code: p.point_role for p in db.scalars(select(MeterPoint)).all()}

    # 自然键 -> 聚合桶
    buckets: dict[tuple, dict] = {}

    def bucket(grain_type, grain_key, day, batch_code=None):
        key = (grain_type, grain_key, day)
        b = buckets.setdefault(key, {
            "grain_type": grain_type, "grain_key": grain_key, "day": day,
            "batch_code": batch_code, "planned": 0.0, "confirmed": 0.0,
            "pending": 0.0, "candidate": 0.0, "upstream": 0.0, "downstream": 0.0,
            "devices": set(), "late": False, "fact_ids": [], "channels": set()})
        return b

    for f in facts:
        # 供水事实归属区间结束日；计划目标事实归属生效起始日，保证同日落同一桶
        day = _day(f.interval_start) if f.fact_kind == "plan" else _day(f.interval_end)
        grains: list[tuple] = []
        if f.plot_code:
            grains.append(("plot", f.plot_code))
        if f.channel_code:
            grains.append(("channel", f.channel_code))
        if f.batch_code:
            grains.append(("batch", f.batch_code))
        for grain_type, grain_key in grains:
            b = bucket(grain_type, grain_key, day, f.batch_code)
            b["fact_ids"].append(f.id)
            if f.device_id:
                b["devices"].add(f.device_id)
            if f.channel_code:
                b["channels"].add(f.channel_code)
            b["late"] = b["late"] or f.is_late
            if f.fact_kind == "plan":
                b["planned"] += f.value
                continue
            if f.status == "confirmed":
                b["confirmed"] += f.value
                if point_role.get(f.meter_point_code) in ("head", "transfer"):
                    b["upstream"] += f.value
                else:
                    b["downstream"] += f.value
            elif f.status == "pending_verification":
                b["pending"] += f.value
            elif f.status == "candidate":
                b["candidate"] += f.value  # 留痕但不进正式量

    run = RecomputeRun(run_uid=_uid("run"), trigger=trigger, status="running",
                       affected_keys=[f"{k[0]}:{k[1]}@{k[2].date()}" for k in buckets],
                       note=note)
    db.add(run); db.flush()

    versions: list[MetricVersion] = []
    for (grain_type, grain_key, day), b in sorted(buckets.items(),
                                                  key=lambda x: (x[0][2], x[0][0], x[0][1])):
        day_end = day + timedelta(days=1)
        missing = _missing_rate(db, sorted(b["devices"]), day, day_end)
        planned = round(b["planned"], 3)
        confirmed = round(b["confirmed"], 3)
        pending = round(b["pending"], 3)

        completion = round(confirmed / planned, 4) if planned > 0 else None
        risk = round(max((confirmed + pending) / planned - 1.0, 0.0), 4) if planned > 0 else 0.0

        loss_lower = loss_upper = None
        is_frozen = grain_type == "channel" and grain_key in frozen
        if grain_type == "channel" and b["upstream"] > 0 and not is_frozen:
            point_loss = b["upstream"] - b["downstream"]
            loss_upper = round(max(point_loss, 0.0), 3)
            loss_lower = round(max(point_loss - pending, 0.0), 3)

        if is_frozen:
            status = "frozen"
        elif b["late"]:
            status = "tentative"
        elif missing > 0 or pending > 0 or b["candidate"] > 0:
            status = "tentative"
        else:
            status = "stable"

        cur = {"planned_volume": planned, "confirmed_volume": confirmed,
               "pending_volume": pending, "missing_rate": missing, "status": status}

        prev = db.scalar(select(MetricVersion).where(
            MetricVersion.grain_type == grain_type,
            MetricVersion.grain_key == grain_key,
            MetricVersion.metric_date == day,
            MetricVersion.superseded_by.is_(None)).order_by(
            MetricVersion.version_no.desc()))
        vno = _next_version_no(db)
        mv = MetricVersion(
            version_no=vno, metric_date=day, grain_type=grain_type,
            grain_key=grain_key, batch_code=b["batch_code"],
            planned_volume=planned, confirmed_volume=confirmed,
            pending_volume=pending, loss_lower=loss_lower, loss_upper=loss_upper,
            missing_rate=missing, over_allocation_risk=risk,
            plan_completion_rate=completion, status=status,
            watermark=watermark, stability_deadline=stability_deadline,
            diff_note=_diff_note(prev, cur), input_fact_ids=b["fact_ids"],
            recompute_run_id=run.id)
        db.add(mv); db.flush()
        if prev:
            prev.superseded_by = mv.id
        versions.append(mv)

    run.status = "done"
    run.finished_at = _now()
    db.flush()
    return versions
