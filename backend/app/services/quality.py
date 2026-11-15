"""质量规则引擎。

覆盖规则：
- READING_REVERSAL     读数倒退（事实编译器内联触发，禁止负供水）
- METER_CHANGE_NO_VOUCHER / METER_CHANGE_TRUNCATION 换表缺证与截断
- UNIT_MISMATCH        单位不一致
- TIME_OVERLAP         时间重叠（同设备区间重叠 / 报文重复时间窗）
- CLOCK_DRIFT          设备时钟漂移（事件时间与接收时间偏差超阈值）
- GAP_MISSING          缺测（相邻报文间隔超过 1.5 倍契约采集区间）
- CHANNEL_IMBALANCE    渠道守恒失衡（上游确认量 vs 下游可解释量超容差 -> 对账任务 + 冻结）
- EVIDENCE_CONFLICT    设备补传与人工补录证据冲突
- MANUAL_WITHOUT_BASIS 人工补录缺依据

事件全部追加；同一自然键的开放事件幂等，不重复制造噪声。
"""
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    DataContract, QualityEvent, RawMessage, ReconciliationTask, SupplyFact,
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def raise_event(db: Session, rule_code: str, severity: str, detail: dict,
                device_id=None, meter_point_code=None, channel_code=None,
                interval_start=None, interval_end=None,
                related_fact_ids=None, extra_key: str | None = None) -> QualityEvent:
    """开放事件按 规则+自然键 幂等；已存在则直接返回。"""
    key = extra_key or rule_code
    existing = db.scalar(select(QualityEvent).where(
        QualityEvent.rule_code == rule_code,
        QualityEvent.device_id.isnot(None) if device_id else QualityEvent.device_id.is_(None),
        QualityEvent.interval_start == interval_start,
        QualityEvent.status != "closed"))
    if existing and existing.detail.get("dedup_key") == key and (
            device_id is None or existing.device_id == device_id):
        return existing
    ev = QualityEvent(
        event_uid=_uid("qe"), rule_code=rule_code, severity=severity,
        device_id=device_id, meter_point_code=meter_point_code,
        channel_code=channel_code, interval_start=interval_start,
        interval_end=interval_end,
        detail={**detail, "dedup_key": key},
        related_fact_ids=related_fact_ids or [])
    db.add(ev); db.flush()
    return ev


# ---------------------------------------------------------------- 单条/区间规则
def check_clock_drift(db: Session, msg: RawMessage) -> None:
    drift = abs((msg.received_at - msg.event_time).total_seconds()) / 60.0
    if drift > settings.clock_drift_minutes:
        raise_event(db, "CLOCK_DRIFT", "warning",
                    {"message_uid": msg.message_uid, "drift_minutes": round(drift, 1),
                     "event_time": msg.event_time.isoformat(),
                     "received_at": msg.received_at.isoformat()},
                    device_id=msg.device_id, interval_start=msg.event_time,
                    extra_key=f"drift:{msg.message_uid}")


def check_unit(db: Session, contract: DataContract | None, msg: RawMessage) -> None:
    if contract and msg.payload.get("unit") and msg.payload["unit"] != contract.unit:
        raise_event(db, "UNIT_MISMATCH", "critical",
                    {"message_uid": msg.message_uid,
                     "payload_unit": msg.payload["unit"], "contract_unit": contract.unit},
                    device_id=msg.device_id, interval_start=msg.event_time,
                    extra_key=f"unit:{msg.message_uid}")


def check_gap(db: Session, device_id: str, contract_code: str | None,
              start: datetime, end: datetime, ctx: dict) -> None:
    contract = db.scalar(select(DataContract).where(
        DataContract.code == contract_code)) if contract_code else None
    base = contract.collection_interval_sec if contract else 3600
    gap = (end - start).total_seconds()
    if gap > base * settings.missing_gap_factor:
        raise_event(db, "GAP_MISSING", "warning",
                    {"device_id": device_id, "gap_minutes": round(gap / 60, 1),
                     "expected_interval_sec": base,
                     "interval": [start.isoformat(), end.isoformat()]},
                    device_id=device_id, meter_point_code=ctx.get("meter_point_code"),
                    channel_code=ctx.get("channel_code"),
                    interval_start=start, interval_end=end,
                    extra_key=f"gap:{device_id}:{start.isoformat()}")


def check_time_overlap(db: Session) -> list[QualityEvent]:
    """同设备相邻报文事件时间不得倒退重叠（排除修订版本同时间点）。"""
    events: list[QualityEvent] = []
    device_ids = [r[0] for r in db.execute(
        select(RawMessage.device_id).distinct()).all()]
    for dev in device_ids:
        msgs = db.scalars(select(RawMessage).where(
            RawMessage.device_id == dev).order_by(
            RawMessage.event_time, RawMessage.received_at)).all()
        seen: dict[datetime, str] = {}
        for m in msgs:
            if m.event_time in seen and seen[m.event_time] != m.message_uid and not m.supersedes_uid:
                ev = raise_event(db, "TIME_OVERLAP", "warning",
                                 {"device_id": dev, "event_time": m.event_time.isoformat(),
                                  "message_uids": [seen[m.event_time], m.message_uid]},
                                 device_id=dev, interval_start=m.event_time,
                                 extra_key=f"overlap:{dev}:{m.event_time.isoformat()}:{m.message_uid}")
                events.append(ev)
            else:
                seen[m.event_time] = m.message_uid
    return events


# ---------------------------------------------------------------- 渠道守恒
def _active_facts(db: Session) -> list[SupplyFact]:
    from .ledger import active_facts
    return active_facts(db)


def check_channel_conservation(db: Session) -> list[ReconciliationTask]:
    """按渠道层级做守恒：父渠道渠首确认量 vs 各子渠道确认量之和。

    超容差 => 对账任务并冻结父渠道损耗指标。只统计 confirmed；
    pending_verification/candidate 不计入可解释量，绝不以计划量补平。
    """
    from ..models import Channel, MeterPoint
    facts = [f for f in _active_facts(db) if f.fact_kind in ("supply", "transfer")]
    point_role = {p.code: p.point_role for p in db.scalars(select(MeterPoint)).all()}
    parent = {c.code: c.parent_code for c in db.scalars(select(Channel)).all()}
    children: dict[str, list[str]] = {}
    for code, p in parent.items():
        if p:
            children.setdefault(p, []).append(code)

    agg: dict[str, dict] = {}
    for f in facts:
        if not f.channel_code:
            continue
        b = agg.setdefault(f.channel_code, {
            "head_confirmed": 0.0, "confirmed": 0.0, "pending": 0.0,
            "candidate": 0.0, "starts": [], "ends": [], "fact_ids": []})
        b["fact_ids"].append(f.id)
        b["starts"].append(f.interval_start)
        b["ends"].append(f.interval_end)
        role = point_role.get(f.meter_point_code, "")
        if f.status == "confirmed":
            b["confirmed"] += f.value
            if role in ("head", "transfer"):
                b["head_confirmed"] += f.value
        elif f.status == "pending_verification":
            b["pending"] += f.value
        elif f.status == "candidate":
            b["candidate"] += f.value

    tasks: list[ReconciliationTask] = []
    for channel, kids in children.items():
        own = agg.get(channel, {"head_confirmed": 0.0, "pending": 0.0,
                                "starts": [], "ends": [], "fact_ids": []})
        upstream = own.get("head_confirmed", 0.0)
        if upstream <= 0:
            continue
        downstream = sum(agg.get(k, {}).get("confirmed", 0.0) for k in kids)
        pending = own.get("pending", 0.0) + sum(
            agg.get(k, {}).get("pending", 0.0) for k in kids)
        diff = upstream - downstream
        ratio = abs(diff) / upstream
        if ratio <= settings.imbalance_tolerance_ratio:
            continue
        all_buckets = [own] + [agg.get(k, {}) for k in kids]
        starts = [s for b in all_buckets for s in b.get("starts", [])]
        ends = [e for b in all_buckets for e in b.get("ends", [])]
        fact_ids = [i for b in all_buckets for i in b.get("fact_ids", [])]
        start, end = min(starts), max(ends)
        exists = db.scalar(select(ReconciliationTask).where(
            ReconciliationTask.channel_code == channel,
            ReconciliationTask.status.in_(["open", "frozen"])))
        if exists:
            # 重算后失衡仍存在：刷新工作任务数值（任务为工作项，非账本，不留旧值覆盖问题）
            exists.upstream_value = round(upstream, 3)
            exists.downstream_explained = round(downstream, 3)
            exists.diff = round(diff, 3)
            exists.diff_ratio = round(ratio, 4)
            exists.interval_start, exists.interval_end = start, end
            tasks.append(exists)
            continue
        task = ReconciliationTask(
            task_uid=_uid("rec"), channel_code=channel,
            interval_start=start, interval_end=end,
            upstream_value=round(upstream, 3),
            downstream_explained=round(downstream, 3),
            diff=round(diff, 3), diff_ratio=round(ratio, 4),
            status="frozen",
            frozen_metric_keys=[
                f"channel:{channel}:loss_lower", f"channel:{channel}:loss_upper"])
        db.add(task); db.flush()
        raise_event(db, "CHANNEL_IMBALANCE", "critical",
                    {"channel_code": channel, "upstream": round(upstream, 3),
                     "downstream_explained": round(downstream, 3),
                     "diff": round(diff, 3), "diff_ratio": round(ratio, 4),
                     "pending_volume": round(pending, 3),
                     "child_channels": kids,
                     "action": "冻结损耗指标并生成对账任务，禁止以计划量补平"},
                    channel_code=channel, interval_start=start, interval_end=end,
                    related_fact_ids=fact_ids)
        tasks.append(task)
    return tasks


def run_all_rules(db: Session) -> dict:
    """对暂存区全部报文执行时钟漂移/单位/重叠检查（倒退与缺测在编译期触发）。"""
    overlap = check_time_overlap(db)
    for msg in db.scalars(select(RawMessage)).all():
        contract = db.scalar(select(DataContract).where(
            DataContract.code == msg.contract_code))
        check_unit(db, contract, msg)
        # 迟到补传的时间差由 LATE_ARRIVAL 规则管辖，不再重复判时钟漂移
        if not msg.is_late:
            check_clock_drift(db, msg)
    tasks = check_channel_conservation(db)
    return {"overlap_events": len(overlap), "reconciliation_tasks": len(tasks)}
