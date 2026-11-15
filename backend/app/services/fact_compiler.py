"""事实编译器：把暂存区读数拆成可追溯的供水事实。

规则（对应题面硬约束）：
1. 累计表相邻有效读数做差；倒退不解释成负供水，转待核实并出质量事件；
2. 换表点落在区间内：
   - 有凭证：拆成换表前/后两段已确认事实；
   - 无凭证：截断该设备区间，关联渠道量列待核实(truncated/pending)，绝不产生负值；
3. 瞬时表按相邻流量梯形积分；
4. 同一区间设备补传与人工补录并存：两份证据都保留，生成 candidate 事实，
   未裁定前不进入正式指标；
5. 计划量只生成 fact_kind=plan/status=target 的目标事实，永不充当实灌；
6. 全部事实追加写入，重算时以 supersedes_fact_id 链取代旧事实而非删除。
"""
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    Adjudication, DeviceMapping, ManualRevision, MeterChange, MeterPoint,
    Plot, PlotBatchAllocation, RawMessage, SupplyFact,
)
from . import master_data as md
from .ledger import active_facts, superseded_fact_ids


def _fact_uid() -> str:
    return f"fact_{uuid.uuid4().hex[:16]}"


def _superseded_ids(db: Session) -> set[int]:
    return superseded_fact_ids(db)


# ---------------------------------------------------------------- 读数整理
def _effective_device_series(db: Session, device_id: str) -> list[dict]:
    """同一 event_time 取最高 revision_no 的报文为生效值，全部报文 id 留作证据。"""
    msgs = db.scalars(select(RawMessage).where(
        RawMessage.device_id == device_id).order_by(RawMessage.event_time)).all()
    bucket: dict[datetime, dict] = {}
    for m in msgs:
        cur = bucket.get(m.event_time)
        if cur is None or m.revision_no > cur["revision_no"]:
            bucket[m.event_time] = {
                "event_time": m.event_time,
                "reading": float(m.payload.get("reading", 0.0)),
                "unit": m.payload.get("unit"),
                "revision_no": m.revision_no,
                "is_late": m.is_late,
                "source_version": m.source_version,
                "contract_code": m.contract_code,
                "message_ids": [m.id],
            }
        else:
            cur["message_ids"].append(m.id)
    # 同时间点的其他版本报文也挂证据
    for m in msgs:
        if m.id not in bucket[m.event_time]["message_ids"]:
            bucket[m.event_time]["message_ids"].append(m.id)
    return [bucket[k] for k in sorted(bucket)]


def _manual_series(db: Session, device_id: str) -> list[ManualRevision]:
    return list(db.scalars(select(ManualRevision).where(
        ManualRevision.device_id == device_id).order_by(
        ManualRevision.event_time)).all())


def _context(db: Session, device_id: str, at: datetime) -> dict:
    """解析计量点/渠道/地块/批次/映射版本。"""
    ctx = {"meter_point_code": None, "channel_code": None, "plot_code": None,
           "batch_code": None, "mapping_version": None, "role": None}
    dm: DeviceMapping | None = md.resolve_device(db, device_id, at)
    if not dm:
        return ctx
    ctx["meter_point_code"] = dm.meter_point_code
    ctx["mapping_version"] = dm.mapping_version
    mp: MeterPoint | None = md.resolve_point(db, dm.meter_point_code)
    if mp:
        ctx["channel_code"] = mp.channel_code
        ctx["role"] = mp.point_role
        if mp.point_role in ("branch", "field"):
            plot = db.scalar(select(Plot).where(Plot.channel_code == mp.channel_code))
            if plot:
                ctx["plot_code"] = plot.code
                alloc = md.allocation_for_plot(db, plot.code, at)
                if alloc:
                    ctx["batch_code"] = alloc.batch_code
    return ctx


def _make_fact(db: Session, *, kind, status, device_id, ctx, start, end,
               value, method, reading_start=None, reading_end=None,
               confidence=1.0, truncated=False, is_late=False,
               quality_flag=None, evidence_msg=None, evidence_man=None,
               contract_code=None, source_version=None, meter_change_id=None,
               adjudication_id=None) -> SupplyFact:
    fact = SupplyFact(
        fact_uid=_fact_uid(), fact_kind=kind, status=status, device_id=device_id,
        meter_point_code=ctx.get("meter_point_code"), channel_code=ctx.get("channel_code"),
        plot_code=ctx.get("plot_code"), batch_code=ctx.get("batch_code"),
        interval_start=start, interval_end=end,
        reading_start=reading_start, reading_end=reading_end,
        value=round(max(value, 0.0), 3), unit="m3", method=method,
        confidence=confidence, truncated=truncated, is_late=is_late,
        quality_flag=quality_flag,
        evidence_message_ids=evidence_msg or [], evidence_manual_ids=evidence_man or [],
        mapping_version=ctx.get("mapping_version"), contract_code=contract_code,
        source_version=source_version, meter_change_id=meter_change_id,
        adjudication_id=adjudication_id)
    db.add(fact); db.flush()
    return fact


def _supersede(db: Session, old: SupplyFact, new: SupplyFact) -> None:
    new.supersedes_fact_id = old.id
    ids = set(new.replaces_fact_ids or [])
    ids.add(old.id)
    new.replaces_fact_ids = sorted(ids)
    new.revision_no = max(new.revision_no, old.revision_no + 1)


def _find_priors(db: Session, device_id, start, end, kind,
                 exclude_ids: set[int] | None = None) -> list[SupplyFact]:
    dead = _superseded_ids(db)
    exclude_ids = exclude_ids or set()
    return [f for f in db.scalars(select(SupplyFact).where(
            SupplyFact.device_id == device_id,
            SupplyFact.interval_start == start,
            SupplyFact.interval_end == end,
            SupplyFact.fact_kind == kind)).all()
            if f.id not in dead and f.id not in exclude_ids]


# ---------------------------------------------------------------- 冲突处理
def _handle_conflict(db: Session, device_id, start, end, ctx, device_value,
                     device_obs, manual: ManualRevision, contract) -> list[SupplyFact]:
    """同一区间设备与人工证据并存：双 candidate + 裁定登记，未裁定不进正式指标。"""
    adj = md.open_adjudication_for(db, device_id, start, end)
    if adj is None:
        adj = Adjudication(
            conflict_key=f"{device_id}|{start.isoformat()}|{end.isoformat()}",
            device_id=device_id, interval_start=start, interval_end=end,
            decision="pending")
        db.add(adj); db.flush()
        from .quality import raise_event
        raise_event(db, "EVIDENCE_CONFLICT", "critical",
                    {"device_id": device_id, "interval": [start.isoformat(), end.isoformat()],
                     "device_value": device_value, "manual_value": manual.value,
                     "manual_basis": manual.basis, "manual_operator": manual.operator},
                    device_id=device_id, meter_point_code=ctx.get("meter_point_code"),
                    channel_code=ctx.get("channel_code"),
                    interval_start=start, interval_end=end,
                    extra_key="conflict")
    facts: list[SupplyFact] = []
    if adj.decision == "pending":
        f1 = _make_fact(db, kind="supply", status="candidate", device_id=device_id, ctx=ctx,
                        start=start, end=end, value=device_value,
                        method="cumulative_delta", reading_start=device_obs[0]["reading"],
                        reading_end=device_obs[1]["reading"], confidence=0.8,
                        evidence_msg=device_obs[0]["message_ids"] + device_obs[1]["message_ids"],
                        contract_code=contract, source_version=device_obs[1]["source_version"],
                        quality_flag="EVIDENCE_CONFLICT", adjudication_id=adj.id,
                        is_late=device_obs[0]["is_late"] or device_obs[1]["is_late"])
        f2 = _make_fact(db, kind="supply", status="candidate", device_id=device_id, ctx=ctx,
                        start=start, end=end, value=manual.value, method="manual",
                        confidence=0.5 if not manual.basis else 0.7,
                        evidence_man=[manual.id], contract_code=contract,
                        quality_flag="EVIDENCE_CONFLICT", adjudication_id=adj.id)
        facts = [f1, f2]
    elif adj.decision == "accept_device":
        facts = [_make_fact(db, kind="supply", status="confirmed", device_id=device_id, ctx=ctx,
                            start=start, end=end, value=device_value,
                            method="cumulative_delta",
                            reading_start=device_obs[0]["reading"],
                            reading_end=device_obs[1]["reading"], confidence=0.9,
                            evidence_msg=device_obs[0]["message_ids"] + device_obs[1]["message_ids"],
                            evidence_man=[manual.id], contract_code=contract,
                            source_version=device_obs[1]["source_version"],
                            adjudication_id=adj.id)]
    elif adj.decision == "accept_manual":
        facts = [_make_fact(db, kind="supply", status="confirmed", device_id=device_id, ctx=ctx,
                            start=start, end=end, value=manual.value, method="manual",
                            confidence=0.9, evidence_man=[manual.id],
                            evidence_msg=device_obs[0]["message_ids"] + device_obs[1]["message_ids"],
                            contract_code=contract, adjudication_id=adj.id)]
    # reject_both：不产生正式事实，量留待核实
    return facts


# ---------------------------------------------------------------- 单设备编译
def compile_device(db: Session, device_id: str, reading_type: str) -> list[SupplyFact]:
    series = _effective_device_series(db, device_id)
    manuals = _manual_series(db, device_id)
    if len(series) < 2:
        return []
    contract = series[1].get("contract_code")
    produced: list[SupplyFact] = []

    for idx, (a, b) in enumerate(zip(series, series[1:])):
        start, end = a["event_time"], b["event_time"]
        ctx = _context(db, device_id, end)
        kind = "transfer" if ctx.get("role") == "transfer" else "supply"
        mc = md.meter_change_at(db, device_id, start, end)
        manual = next((r for r in manuals if start <= r.event_time < end), None)
        # 仅主张用水归属（claimed_operator_code）的人工补录不作为水量证据触发冲突，
        # 但其证据行挂到事实上，供计费对账识别归属双候选
        claim_only = bool(manual and (manual.payload or {}).get("claimed_operator_code"))
        claim_manual_ids = [manual.id] if claim_only else None
        if claim_only:
            manual = None

        # 缺测区间由质量引擎登记，事实编译继续，不因缺测而停发
        from .quality import check_gap, raise_event
        check_gap(db, device_id, contract, start, end, ctx)

        # ---- 换表处理 ----
        if mc is not None:
            if mc.voucher_present:
                # 有凭证：拆两段，连续性由凭证背书
                old_last = mc.old_last_reading if mc.old_last_reading is not None else a["reading"]
                new_first = mc.new_first_reading if mc.new_first_reading is not None else b["reading"]
                v1 = max(old_last - a["reading"], 0.0)
                v2 = max(b["reading"] - new_first, 0.0)
                f1 = _make_fact(db, kind=kind, status="confirmed", device_id=device_id, ctx=ctx,
                                start=start, end=mc.change_time, value=v1,
                                method="cumulative_delta", reading_start=a["reading"],
                                reading_end=old_last, evidence_msg=a["message_ids"],
                                contract_code=contract, source_version=a["source_version"],
                                meter_change_id=mc.id)
                f2 = _make_fact(db, kind=kind, status="confirmed", device_id=device_id, ctx=ctx,
                                start=mc.change_time, end=end, value=v2,
                                method="cumulative_delta", reading_start=new_first,
                                reading_end=b["reading"], evidence_msg=b["message_ids"],
                                contract_code=contract, source_version=b["source_version"],
                                meter_change_id=mc.id)
                produced.extend([f1, f2])
            else:
                # 无凭证：截断。前后两段均落待核实，绝不跨表做差、绝不产生负值。
                # 前段用换表前已确认速率估计待核实量；后段保留新表增量但未经凭证背书。
                pre_hours = (mc.change_time - start).total_seconds() / 3600.0
                est_pre = 0.0
                if idx > 0:
                    prev = series[idx - 1]
                    span = (a["event_time"] - prev["event_time"]).total_seconds() / 3600.0
                    rate = max(a["reading"] - prev["reading"], 0.0) / span if span > 0 else 0.0
                    est_pre = round(rate * pre_hours, 3)
                post_value = max(b["reading"] - (mc.new_first_reading or 0.0), 0.0)
                f1 = _make_fact(db, kind=kind, status="pending_verification",
                                device_id=device_id, ctx=ctx, start=start,
                                end=mc.change_time, value=est_pre,
                                method="cumulative_delta_truncated",
                                reading_start=a["reading"],
                                reading_end=mc.old_last_reading, truncated=True,
                                confidence=0.2, evidence_msg=a["message_ids"],
                                contract_code=contract, meter_change_id=mc.id,
                                quality_flag="METER_CHANGE_NO_VOUCHER")
                f2 = _make_fact(db, kind=kind, status="pending_verification",
                                device_id=device_id, ctx=ctx, start=mc.change_time,
                                end=end, value=post_value,
                                method="cumulative_delta_truncated",
                                reading_start=mc.new_first_reading,
                                reading_end=b["reading"], truncated=True,
                                confidence=0.2, evidence_msg=b["message_ids"],
                                contract_code=contract, meter_change_id=mc.id,
                                quality_flag="METER_CHANGE_NO_VOUCHER")
                raise_event(db, "METER_CHANGE_TRUNCATION", "critical",
                            {"device_id": device_id,
                             "interval": [start.isoformat(), end.isoformat()],
                             "channel_code": ctx.get("channel_code"),
                             "action": "区间截断，关联渠道量列待核实，禁止解释为负供水"},
                            device_id=device_id, meter_point_code=ctx.get("meter_point_code"),
                            channel_code=ctx.get("channel_code"),
                            interval_start=start, interval_end=end,
                            related_fact_ids=[f1.id, f2.id], extra_key="truncation")
                produced.extend([f1, f2])
            continue

        # ---- 常规差量/积分 ----
        if reading_type == "cumulative":
            delta = b["reading"] - a["reading"]
            if delta < 0:
                # 读数倒退：不产生负供水
                f = _make_fact(db, kind=kind, status="pending_verification",
                               device_id=device_id, ctx=ctx, start=start, end=end,
                               value=0.0, method="cumulative_delta",
                               reading_start=a["reading"], reading_end=b["reading"],
                               confidence=0.1, evidence_msg=a["message_ids"] + b["message_ids"],
                               contract_code=contract, quality_flag="READING_REVERSAL",
                               is_late=a["is_late"] or b["is_late"])
                raise_event(db, "READING_REVERSAL", "critical",
                            {"device_id": device_id,
                             "reading_start": a["reading"], "reading_end": b["reading"],
                             "interval": [start.isoformat(), end.isoformat()]},
                            device_id=device_id, meter_point_code=ctx.get("meter_point_code"),
                            channel_code=ctx.get("channel_code"),
                            interval_start=start, interval_end=end,
                            related_fact_ids=[f.id])
                produced.append(f)
                continue
            if manual is not None:
                produced.extend(_handle_conflict(
                    db, device_id, start, end, ctx, delta, (a, b), manual, contract))
                continue
            f = _make_fact(db, kind=kind, status="confirmed", device_id=device_id, ctx=ctx,
                           start=start, end=end, value=delta, method="cumulative_delta",
                           reading_start=a["reading"], reading_end=b["reading"],
                           evidence_msg=a["message_ids"] + b["message_ids"],
                           evidence_man=claim_manual_ids,
                           contract_code=contract, source_version=b["source_version"],
                           is_late=a["is_late"] or b["is_late"])
            produced.append(f)
        else:
            hours = (end - start).total_seconds() / 3600.0
            value = (a["reading"] + b["reading"]) / 2.0 * hours
            if manual is not None:
                produced.extend(_handle_conflict(
                    db, device_id, start, end, ctx, value, (a, b), manual, contract))
                continue
            produced.append(_make_fact(db, kind=kind, status="confirmed",
                                       device_id=device_id, ctx=ctx, start=start, end=end,
                                       value=value, method="flow_integration",
                                       reading_start=a["reading"], reading_end=b["reading"],
                                       evidence_msg=a["message_ids"] + b["message_ids"],
                                       evidence_man=claim_manual_ids,
                                       contract_code=contract,
                                       source_version=b["source_version"],
                                       is_late=a["is_late"] or b["is_late"]))

    # 重算：用新事实取代同自然键的全部旧活动事实（候选/分段可能不止一条），
    # 但排除本批新事实，避免同批候选互相取代
    new_ids = {f.id for f in produced}
    for new in produced:
        for prior in _find_priors(db, device_id, new.interval_start,
                                  new.interval_end, new.fact_kind,
                                  exclude_ids=new_ids):
            _supersede(db, prior, new)
    db.flush()  # autoflush 关闭：显式刷库，保证取代关系对后续质量/指标查询可见
    return produced


def compile_plan_facts(db: Session) -> list[SupplyFact]:
    """计划水量 -> 目标事实（target），只作比对。"""
    allocs = db.scalars(select(PlotBatchAllocation)).all()
    out: list[SupplyFact] = []
    for al in allocs:
        ctx = {"meter_point_code": None, "mapping_version": al.mapping_version}
        plot = db.scalar(select(Plot).where(Plot.code == al.plot_code))
        channel = plot.channel_code if plot else None
        prior = db.scalar(select(SupplyFact).where(
            SupplyFact.fact_kind == "plan",
            SupplyFact.plot_code == al.plot_code,
            SupplyFact.batch_code == al.batch_code))
        f = SupplyFact(
            fact_uid=_fact_uid(), fact_kind="plan", status="target",
            plot_code=al.plot_code, batch_code=al.batch_code, channel_code=channel,
            interval_start=al.effective_from,
            interval_end=al.effective_to or al.effective_from + timedelta(days=1),
            value=al.planned_volume, unit="m3", method="plan",
            mapping_version=al.mapping_version)
        db.add(f); db.flush()
        if prior:
            _supersede(db, prior, f)
        out.append(f)
    db.flush()
    return out
