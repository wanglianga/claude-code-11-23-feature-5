"""差异桥接器 + 冻结后调整候选。

差异桥三列分别列示：
- metering_accrual 应计量口径：来自当前活动分摊片段（含待证/归属候选说明）；
- external_fact   外部账务事实：来自外部账务事实账（按金额方向带符号）；
- late_revision   后补修订影响：来自调整候选（冻结后才到达的补传/人工更正等）。

冻结纪律（题面硬约束）：
已确认冻结的账期之后收到人工读数更正/迟到补传时，只生成引用原快照的
调整候选（adjustment_candidates），原账期快照与其明细保持不可变、可复演；
候选的接受/拒绝仅为审阅留痕，不触发任何收款、退款或资金状态迁移。
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ManualRevision, RawMessage
from ..models_billing import (
    AdjustmentCandidate, BillingSnapshot,
    BillingSnapshotItem, DifferenceBridgeEntry, ExternalBillingEntry,
    PriceVersion, TenureRecord,
    ADJ_PROPOSED, BRIDGE_EXTERNAL, BRIDGE_LATE, BRIDGE_METERING,
    FRAG_BLOCKED, FRAG_CANDIDATE, FRAG_CONFIRMED, SNAPSHOT_CONFIRMED,
)
from .billing_common import money, period_key_of, uid, volume
from .billing_ledger import active_fragments


# ---------------------------------------------------------------- 差异桥
def build_bridge(db: Session, period_start, period_end,
                 snapshot: BillingSnapshot | None = None) -> dict:
    pkey = period_key_of(period_start)
    # 派生工作结果：重跑替换未冻结部分；已确认快照绑定的桥接行保留
    confirmed_snap_ids = {s.id for s in db.scalars(select(BillingSnapshot).where(
        BillingSnapshot.period_key == pkey,
        BillingSnapshot.status == SNAPSHOT_CONFIRMED)).all()}
    for row in db.scalars(select(DifferenceBridgeEntry).where(
            DifferenceBridgeEntry.period_key == pkey)).all():
        if not row.snapshot_id or row.snapshot_id not in confirmed_snap_ids:
            db.delete(row)
    db.flush()

    snap_id = snapshot.id if snapshot else None
    entries: list[DifferenceBridgeEntry] = []
    fragments = [f for f in active_fragments(db, pkey)
                 if f.interval_start < period_end and f.interval_end > period_start]
    externals = [x for x in db.scalars(select(ExternalBillingEntry).where(
        ExternalBillingEntry.period_start < period_end,
        ExternalBillingEntry.period_end > period_start)).all()]
    candidates = db.scalars(select(AdjustmentCandidate).where(
        AdjustmentCandidate.period_key == pkey)).all()

    # 列 1：应计量口径（逐片段，保证每条差额可回查版本依据）
    for f in fragments:
        if f.status == FRAG_BLOCKED:
            reason = f.blocking_reason
            narration = (f"待证片段：{f.blocking_reason}；区间 "
                         f"{f.interval_start.isoformat()}~{f.interval_end.isoformat()}，"
                         f"阻断确认对账，需补中间读数/归属凭证")
        elif f.status == FRAG_CANDIDATE:
            reason = "ATTRIBUTION_DISPUTED"
            narration = (f"归属双候选组 {f.conflict_group_uid}："
                         f"候选 {f.detail.get('candidate')}，等待凭证裁定")
        else:
            reason = None
            narration = (f"价目 v{f.price_version_no} 阶梯{f.tier_no or '-'} "
                         f"× {f.volume}m³")
        entries.append(DifferenceBridgeEntry(
            bridge_uid=uid("brdg"), period_key=pkey, plot_code=f.plot_code,
            column_kind=BRIDGE_METERING, source_kind="fragment",
            source_ref={"fragment_id": f.id, "fragment_uid": f.fragment_uid,
                        "supply_fact_id": f.supply_fact_id,
                        "tenure_version": f.tenure_version,
                        "tenure_id": f.tenure_id,
                        "price_version_no": f.price_version_no,
                        "price_version_id": f.price_version_id,
                        "snapshot_id": snap_id},
            volume=volume(f.volume or 0.0), amount=money(f.amount or 0.0),
            diff_reason=reason, narration=narration, snapshot_id=snap_id))

    # 列 2：外部账务事实（借正贷负）
    for x in externals:
        signed = x.amount if x.direction == "debit" else -x.amount
        entries.append(DifferenceBridgeEntry(
            bridge_uid=uid("brdg"), period_key=pkey,
            plot_code=x.plot_code or "", column_kind=BRIDGE_EXTERNAL,
            source_kind="external_entry",
            source_ref={"external_entry_id": x.id, "entry_uid": x.entry_uid,
                        "receipt_no": x.receipt_no,
                        "bill_line_fingerprint": x.bill_line_fingerprint,
                        "direction": x.direction, "entry_kind": x.entry_kind,
                        "snapshot_id": snap_id},
            volume=0.0, amount=money(signed),
            diff_reason=None,
            narration=f"外部{x.entry_kind} 回执{x.receipt_no or '无'} "
                      f"{x.direction} {x.amount}",
            snapshot_id=snap_id))

    # 列 3：后补修订影响（调整候选）
    for c in candidates:
        entries.append(DifferenceBridgeEntry(
            bridge_uid=uid("brdg"), period_key=pkey, plot_code=c.plot_code,
            column_kind=BRIDGE_LATE, source_kind="adjustment_candidate",
            source_ref={"adjustment_candidate_id": c.id,
                        "candidate_uid": c.candidate_uid,
                        "original_snapshot_id": c.original_snapshot_id,
                        "trigger_kind": c.trigger_kind, "status": c.status},
            volume=volume(c.impact_volume), amount=money(c.impact_amount),
            diff_reason=c.trigger_kind,
            narration=(f"[{c.status}] {c.trigger_kind} 调整候选，"
                       f"引用原快照 #{c.original_snapshot_id}，原快照不变"),
            snapshot_id=snap_id))

    db.add_all(entries); db.flush()

    # 按地块汇总三列金额/水量
    summary: dict[str, dict] = {}
    for e in entries:
        b = summary.setdefault(e.plot_code, {
            "metering_amount": 0.0, "external_amount": 0.0,
            "late_revision_amount": 0.0, "metering_volume": 0.0,
            "diff_amount": 0.0, "reasons": []})
        if e.column_kind == BRIDGE_METERING:
            b["metering_amount"] += e.amount
            b["metering_volume"] += e.volume
        elif e.column_kind == BRIDGE_EXTERNAL:
            b["external_amount"] += e.amount
        else:
            b["late_revision_amount"] += e.amount
        if e.diff_reason and e.diff_reason not in b["reasons"]:
            b["reasons"].append(e.diff_reason)
    for plot, b in summary.items():
        b["metering_amount"] = money(b["metering_amount"])
        b["external_amount"] = money(b["external_amount"])
        b["late_revision_amount"] = money(b["late_revision_amount"])
        b["metering_volume"] = volume(b["metering_volume"])
        b["diff_amount"] = money(
            b["metering_amount"] + b["late_revision_amount"] - b["external_amount"])
    return {"bridge_entries": len(entries), "plot_summary": summary}


# ---------------------------------------------------------------- 冻结后修订扫描
def _frozen_totals(snapshot: BillingSnapshot) -> dict[str, dict]:
    """从快照明细副本复算冻结时的确认口径。"""
    totals: dict[str, dict] = {}
    for item in db_query_items(snapshot):
        if item.item_kind != "fragment":
            continue
        c = item.content
        if c.get("status") != FRAG_CONFIRMED:
            continue
        b = totals.setdefault(c["plot_code"], {"volume": 0.0, "amount": 0.0})
        b["volume"] += c.get("volume") or 0.0
        b["amount"] += c.get("amount") or 0.0
    return totals


def db_query_items(snapshot: BillingSnapshot) -> list[BillingSnapshotItem]:
    from sqlalchemy.orm import object_session
    db = object_session(snapshot)
    return list(db.scalars(select(BillingSnapshotItem).where(
        BillingSnapshotItem.snapshot_id == snapshot.id,
        BillingSnapshotItem.item_kind == "fragment")).all())


def scan_post_freeze_revisions(db: Session, period_start, period_end) -> list[AdjustmentCandidate]:
    """扫描已确认快照之后到达、且事件时间落在该账期的修订，生成调整候选。

    触发类型：manual_reading_correction / late_backfill /
    tenure_correction / price_correction。
    原快照不做任何修改；候选按 (快照, 地块, 触发类型, 证据键) 幂等。
    """
    pkey = period_key_of(period_start)
    snapshots = db.scalars(select(BillingSnapshot).where(
        BillingSnapshot.period_key == pkey,
        BillingSnapshot.status == SNAPSHOT_CONFIRMED)).all()
    if not snapshots:
        return []
    snapshot = max(snapshots, key=lambda s: s.frozen_at)
    frozen_at = snapshot.frozen_at
    # 按地块冻结的快照只扫描其范围内的修订，避免跨地块噪声
    scope_plots = set((snapshot.summary or {}).get("plot_scope") or []) or None

    def in_scope(plot: str | None) -> bool:
        return bool(plot) and (scope_plots is None or plot in scope_plots)

    new_manual = [m for m in db.scalars(select(ManualRevision).where(
        ManualRevision.received_at > frozen_at,
        ManualRevision.event_time >= period_start,
        ManualRevision.event_time < period_end)).all()
        if in_scope(_plot_of_device(db, m.device_id))]
    new_raw = [r for r in db.scalars(select(RawMessage).where(
        RawMessage.received_at > frozen_at,
        RawMessage.event_time >= period_start,
        RawMessage.event_time < period_end,
        RawMessage.is_late.is_(True))).all()
        if in_scope(_plot_of_device(db, r.device_id))]
    new_tenure = [t for t in db.scalars(select(TenureRecord).where(
        TenureRecord.recorded_at > frozen_at,
        TenureRecord.effective_from < period_end,
        (TenureRecord.effective_to.is_(None)) |
        (TenureRecord.effective_to > period_start))).all()
        if in_scope(t.plot_code)]
    new_price = [p for p in db.scalars(select(PriceVersion).where(
        PriceVersion.approved_at > frozen_at,
        PriceVersion.effective_from < period_end,
        (PriceVersion.effective_to.is_(None)) |
        (PriceVersion.effective_to > period_start))).all()
        if (scope_plots is None) or (p.plot_code is None) or (p.plot_code in scope_plots)]

    # 当前活动确认口径（重编译后的最新依据）
    current: dict[str, dict] = {}
    for f in active_fragments(db, pkey):
        if f.status != FRAG_CONFIRMED:
            continue
        if not (f.interval_start < period_end and f.interval_end > period_start):
            continue
        b = current.setdefault(f.plot_code, {"volume": 0.0, "amount": 0.0,
                                             "fragment_ids": []})
        b["volume"] += f.volume or 0.0
        b["amount"] += f.amount or 0.0
        b["fragment_ids"].append(f.id)
    frozen_totals = _frozen_totals(snapshot)

    # 证据 -> 触发类型/地块
    triggers: dict[tuple, dict] = {}
    for m in new_manual:
        plot = _plot_of_device(db, m.device_id)
        triggers.setdefault(("manual_reading_correction", plot, m.revision_uid), {
            "trigger_kind": "manual_reading_correction", "plot_code": plot,
            "evidence": {"manual_revision_id": m.id, "revision_uid": m.revision_uid,
                         "basis": m.basis, "operator": m.operator,
                         "received_at": m.received_at.isoformat()}})
    for r in new_raw:
        plot = _plot_of_device(db, r.device_id)
        triggers.setdefault(("late_backfill", plot, r.message_uid), {
            "trigger_kind": "late_backfill", "plot_code": plot,
            "evidence": {"raw_message_id": r.id, "message_uid": r.message_uid,
                         "received_at": r.received_at.isoformat(),
                         "supersedes_uid": r.supersedes_uid}})
    for t in new_tenure:
        triggers.setdefault(("tenure_correction", t.plot_code, t.tenure_uid), {
            "trigger_kind": "tenure_correction", "plot_code": t.plot_code,
            "evidence": {"tenure_id": t.id, "tenure_uid": t.tenure_uid,
                         "tenure_version": t.tenure_version,
                         "operator_code": t.operator_code,
                         "recorded_at": t.recorded_at.isoformat()}})
    for p in new_price:
        for plot in {f.plot_code for f in active_fragments(db, pkey)}:
            triggers.setdefault(("price_correction", plot, f"price-v{p.version_no}"), {
                "trigger_kind": "price_correction", "plot_code": plot,
                "evidence": {"price_version_id": p.id,
                             "price_version_no": p.version_no,
                             "approved_at": p.approved_at.isoformat()}})

    out: list[AdjustmentCandidate] = []
    for (kind, plot, evidence_key), info in triggers.items():
        if not plot:
            continue
        exists = db.scalar(select(AdjustmentCandidate).where(
            AdjustmentCandidate.original_snapshot_id == snapshot.id,
            AdjustmentCandidate.plot_code == plot,
            AdjustmentCandidate.trigger_kind == kind,
            AdjustmentCandidate.evidence_refs["key"].astext == evidence_key))
        if exists:
            out.append(exists)
            continue
        cur = current.get(plot, {"volume": 0.0, "amount": 0.0, "fragment_ids": []})
        fro = frozen_totals.get(plot, {"volume": 0.0, "amount": 0.0})
        impact_vol = volume(cur["volume"] - fro["volume"])
        impact_amt = money(cur["amount"] - fro["amount"])
        cand = AdjustmentCandidate(
            candidate_uid=uid("adj"), period_key=pkey, plot_code=plot,
            original_snapshot_id=snapshot.id, trigger_kind=kind,
            status=ADJ_PROPOSED, impact_volume=impact_vol, impact_amount=impact_amt,
            evidence_refs={**info["evidence"], "key": evidence_key},
            detail={"frozen_totals": {"volume": volume(fro["volume"]),
                                      "amount": money(fro["amount"])},
                    "current_totals": {"volume": volume(cur["volume"]),
                                       "amount": money(cur["amount"])},
                    "current_fragment_ids": cur["fragment_ids"],
                    "note": "调整候选仅引用原快照，原账期快照保持可复演"})
        db.add(cand); db.flush()
        out.append(cand)
    return out


def _plot_of_device(db: Session, device_id: str) -> str | None:
    from ..models import DeviceMapping, MeterPoint, Plot
    dm = db.scalar(select(DeviceMapping).where(
        DeviceMapping.device_id == device_id,
        DeviceMapping.effective_to.is_(None)))
    if not dm:
        dm = db.scalar(select(DeviceMapping).where(
            DeviceMapping.device_id == device_id))
    if not dm:
        return None
    mp = db.scalar(select(MeterPoint).where(
        MeterPoint.code == dm.meter_point_code))
    if not mp:
        return None
    plot = db.scalar(select(Plot).where(Plot.channel_code == mp.channel_code))
    return plot.code if plot else None


def decide_adjustment(db: Session, candidate_id: int, decision: str,
                      rationale: str, operator: str) -> AdjustmentCandidate:
    """审阅留痕：接受/拒绝调整候选。不改动快照，不产生资金动作。"""
    cand = db.get(AdjustmentCandidate, candidate_id)
    if not cand:
        raise ValueError("调整候选不存在")
    if cand.status != ADJ_PROPOSED:
        raise ValueError(f"候选已处置为 {cand.status}")
    from ..models_billing import ADJ_ACCEPTED, ADJ_REJECTED
    if decision not in (ADJ_ACCEPTED, ADJ_REJECTED):
        raise ValueError("决策必须为 accepted/rejected")
    cand.status = decision
    cand.rationale = rationale
    cand.decision_operator = operator
    from datetime import datetime, timezone
    cand.decided_at = datetime.now(timezone.utc)
    db.flush()
    return cand
