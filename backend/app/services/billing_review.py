"""审阅投影器与归属冲突裁定。

最小披露纪律（题面硬约束）：
- 投影仅输出地块、账期、差异来源与必要证据指针，不输出经营户姓名/编码、
  租约号等无关经营户信息；归属双候选只给「沿革归属/人工主张」两个口径
  及金额范围，等待凭证裁定；
- 差额回查链单独授权使用，按具体片段/桥接行返回计量区间、归属版本、
  价目版本、账期快照与外部回执号，仍然不附带经营户身份信息。
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ManualRevision, RawMessage, SupplyFact
from ..models_billing import (
    AttributionConflict, BillingFragment, BillingSnapshot,
    BillingSnapshotItem, DifferenceBridgeEntry, ExternalBillingEntry,
    PriceVersion, ReceiptMatch, TenureRecord,
    FRAG_BLOCKED, FRAG_CANDIDATE, FRAG_CONFIRMED,
)
from .billing_common import money, period_key_of, volume
from .billing_ledger import active_fragments


# ---------------------------------------------------------------- 审阅投影
def review_projection(db: Session, period_start, period_end,
                      plot_code: str | None = None) -> list[dict]:
    pkey = period_key_of(period_start)
    fragments = [f for f in active_fragments(db, pkey)
                 if f.interval_start < period_end and f.interval_end > period_start
                 and (not plot_code or f.plot_code == plot_code)]
    externals = [x for x in db.scalars(select(ExternalBillingEntry).where(
        ExternalBillingEntry.period_start < period_end,
        ExternalBillingEntry.period_end > period_start)).all()]
    if plot_code:
        externals = [x for x in externals if x.plot_code == plot_code]
    conflicts = db.scalars(select(AttributionConflict).where(
        AttributionConflict.period_key == pkey)).all()
    if plot_code:
        conflicts = [c for c in conflicts if c.plot_code == plot_code]
    snapshots = db.scalars(select(BillingSnapshot).where(
        BillingSnapshot.period_key == pkey)).all()

    plots = sorted({f.plot_code for f in fragments} |
                   {x.plot_code for x in externals if x.plot_code} |
                   {c.plot_code for c in conflicts})
    rows: list[dict] = []
    for plot in plots:
        pf = [f for f in fragments if f.plot_code == plot]
        confirmed = [f for f in pf if f.status == FRAG_CONFIRMED]
        blocked = [f for f in pf if f.status == FRAG_BLOCKED]
        candidate_groups = {f.conflict_group_uid for f in pf
                            if f.status == FRAG_CANDIDATE and f.conflict_group_uid}
        px = [x for x in externals if x.plot_code == plot]
        accrual_amount = money(sum(f.amount or 0.0 for f in confirmed))
        external_net = money(sum(x.amount if x.direction == "debit" else -x.amount
                                 for x in px))
        # 差异来源（去重）
        diff_sources = sorted({f.blocking_reason for f in blocked if f.blocking_reason}
                              | ({"ATTRIBUTION_DISPUTED"} if candidate_groups else set())
                              | _receipt_sources(db, pkey, plot))
        # 必要证据指针（版本号/回执号，不含经营户身份）
        evidence = {
            "supply_fact_ids": sorted({f.supply_fact_id for f in pf
                                       if f.supply_fact_id}),
            "tenure_versions": sorted({f.tenure_version for f in pf
                                       if f.tenure_version}),
            "price_versions": sorted({f.price_version_no for f in pf
                                      if f.price_version_no}),
            "receipt_nos": sorted({x.receipt_no for x in px if x.receipt_no}),
            "snapshot_ids": sorted({s.id for s in snapshots}),
        }
        conflict_views = []
        for c in conflicts:
            if c.plot_code != plot:
                continue
            conflict_views.append({
                "conflict_id": c.id, "conflict_group_uid": c.conflict_group_uid,
                "interval": [c.interval_start.isoformat(), c.interval_end.isoformat()],
                "candidate_a": "沿革归属候选（按转包生效点拆段）",
                "candidate_b": "人工补录主张候选（整段归属）",
                "amount_range": [c.amount_lower, c.amount_upper],
                "status": c.status,
                "evidence_refs": c.evidence_refs,
            })
        rows.append({
            "period_key": pkey, "plot_code": plot,
            "confirmed_volume": volume(sum(f.volume or 0.0 for f in confirmed)),
            "accrual_amount": accrual_amount,
            "external_net_amount": external_net,
            "diff_amount": money(accrual_amount - external_net),
            "blocked_count": len(blocked),
            "blocked_reasons": sorted({f.blocking_reason for f in blocked
                                       if f.blocking_reason}),
            "attribution_conflicts": conflict_views,
            "diff_sources": diff_sources,
            "evidence": evidence,
            "snapshot_statuses": sorted({s.status for s in snapshots}),
        })
    return rows


def _receipt_sources(db: Session, pkey: str, plot: str) -> set[str]:
    out: set[str] = set()
    for m in db.scalars(select(ReceiptMatch).where(
            ReceiptMatch.period_key == pkey,
            ReceiptMatch.plot_code == plot)).all():
        if m.match_status in ("conflict", "missing_external", "missing_internal",
                              "duplicate_idempotent"):
            out.add(f"RECEIPT_{m.match_status.upper()}")
    return out


# ---------------------------------------------------------------- 差额回查链
def trace_difference(db: Session, *, fragment_id: int | None = None,
                     bridge_entry_id: int | None = None,
                     match_id: int | None = None) -> dict:
    """按具体差额返回五类证据：计量区间/归属版本/价目版本/账期快照/外部回执。"""
    chain: dict = {"metering": [], "tenure_versions": [], "price_versions": [],
                   "snapshots": [], "external_receipts": []}
    frag_ids: set[int] = set()

    if bridge_entry_id:
        be = db.get(DifferenceBridgeEntry, bridge_entry_id)
        if be:
            chain["bridge_entry"] = {"id": be.id, "column_kind": be.column_kind,
                                     "source_kind": be.source_kind,
                                     "diff_reason": be.diff_reason,
                                     "narration": be.narration}
            fid = (be.source_ref or {}).get("fragment_id")
            if fid:
                frag_ids.add(fid)
            xid = (be.source_ref or {}).get("external_entry_id")
            if xid:
                _collect_external(db, xid, chain)
    if match_id:
        m = db.get(ReceiptMatch, match_id)
        if m:
            chain["match"] = {"id": m.id, "match_status": m.match_status,
                              "receipt_no": m.receipt_no, "detail": m.detail}
            frag_ids.update(m.fragment_ids or [])
            for xid in m.entry_ids or []:
                _collect_external(db, xid, chain)
    if fragment_id:
        frag_ids.add(fragment_id)

    for fid in frag_ids:
        f = db.get(BillingFragment, fid)
        if not f:
            continue
        fact = db.get(SupplyFact, f.supply_fact_id) if f.supply_fact_id else None
        raw_msgs, manuals = [], []
        if fact:
            raw_msgs = [{"id": i, "message_uid": m.message_uid,
                         "event_time": m.event_time.isoformat(),
                         "received_at": m.received_at.isoformat(),
                         "reading": m.payload.get("reading"),
                         "is_late": m.is_late, "revision_no": m.revision_no}
                        for i in (fact.evidence_message_ids or [])
                        if (m := db.get(RawMessage, i))]
            manuals = [{"id": i, "revision_uid": r.revision_uid,
                        "event_time": r.event_time.isoformat(),
                        "received_at": r.received_at.isoformat(),
                        "basis": r.basis, "operator": r.operator}
                       for i in (fact.evidence_manual_ids or [])
                       if (r := db.get(ManualRevision, i))]
        chain["metering"].append({
            "fragment_id": f.id, "fragment_uid": f.fragment_uid,
            "interval": [f.interval_start.isoformat(), f.interval_end.isoformat()],
            "volume": f.volume, "split_basis": f.split_basis,
            "status": f.status, "blocking_reason": f.blocking_reason,
            "supply_fact": ({"id": fact.id, "fact_uid": fact.fact_uid,
                             "method": fact.method, "value": fact.value,
                             "readings": [fact.reading_start, fact.reading_end],
                             "mapping_version": fact.mapping_version,
                             "raw_messages": raw_msgs,
                             "manual_revisions": manuals} if fact else None)})
        if f.tenure_id:
            t = db.get(TenureRecord, f.tenure_id)
            if t:
                chain["tenure_versions"].append({
                    "tenure_id": t.id, "tenure_version": t.tenure_version,
                    "effective_from": t.effective_from.isoformat(),
                    "effective_to": t.effective_to.isoformat()
                    if t.effective_to else None,
                    "change_reason": t.change_reason,
                    "recorded_at": t.recorded_at.isoformat(),
                    "evidence_ref": t.evidence_ref})
        if f.price_version_id:
            p = db.get(PriceVersion, f.price_version_id)
            if p:
                chain["price_versions"].append({
                    "price_version_id": p.id, "version_no": p.version_no,
                    "approved_at": p.approved_at.isoformat(),
                    "effective_from": p.effective_from.isoformat(),
                    "effective_to": p.effective_to.isoformat()
                    if p.effective_to else None,
                    "tiers": [{"tier_no": t.tier_no, "from": t.volume_from,
                               "to": t.volume_to, "unit_price": t.unit_price}
                              for t in p.tiers]})

    # 关联快照（含片段所在账期的全部快照及该片段的冻结副本）
    for f_id in frag_ids:
        f = db.get(BillingFragment, f_id)
        if not f:
            continue
        for item in db.scalars(select(BillingSnapshotItem).where(
                BillingSnapshotItem.item_kind == "fragment",
                BillingSnapshotItem.ref_id == f.id)).all():
            snap = db.get(BillingSnapshot, item.snapshot_id)
            chain["snapshots"].append({
                "snapshot_id": snap.id, "snapshot_uid": snap.snapshot_uid,
                "period_key": snap.period_key, "status": snap.status,
                "frozen_at": snap.frozen_at.isoformat(),
                "frozen_content": item.content})
    return chain


def _collect_external(db: Session, entry_id: int, chain: dict) -> None:
    x = db.get(ExternalBillingEntry, entry_id)
    if not x:
        return
    chain["external_receipts"].append({
        "entry_id": x.id, "entry_uid": x.entry_uid,
        "source_system": x.source_system, "entry_kind": x.entry_kind,
        "receipt_no": x.receipt_no, "bill_line_no": x.bill_line_no,
        "bill_line_fingerprint": x.bill_line_fingerprint,
        "period": [x.period_start.isoformat(), x.period_end.isoformat()],
        "amount": x.amount, "direction": x.direction, "status": x.status,
        "received_at": x.received_at.isoformat()})


# ---------------------------------------------------------------- 归属冲突裁定
def decide_attribution_conflict(db: Session, conflict_id: int, decision: str,
                                rationale: str, operator: str) -> AttributionConflict:
    """凭证裁定：accept_tenure 采信转包沿革；accept_claim 采信人工补录主张。

    仅登记裁定结论；调用方需重新运行区间分摊编译，候选片段才会转为确认片段。
    不产生任何资金动作。
    """
    c = db.get(AttributionConflict, conflict_id)
    if not c:
        raise ValueError("归属冲突不存在")
    if c.status != "pending":
        raise ValueError(f"冲突已裁定为 {c.status}，不可重复裁定")
    if decision not in ("accept_tenure", "accept_claim"):
        raise ValueError("决策必须为 accept_tenure / accept_claim")
    c.status = "adjudicated"
    c.decision_operator = operator
    c.rationale = rationale
    c.decided_at = datetime.now(timezone.utc)
    c.evidence_refs = {**(c.evidence_refs or {}), "decision": decision,
                       "decision_rationale": rationale,
                       "decided_by": operator,
                       "decided_at": c.decided_at.isoformat()}
    db.flush()
    return c
