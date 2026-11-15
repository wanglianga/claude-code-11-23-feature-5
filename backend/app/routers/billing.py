"""用水计费双时间对账路由。

仅提供数据治理与对账能力：归属/价目/外部账务事实接入、区间分摊编译、
账期快照、回执匹配、差异桥、调整候选、审阅投影与回查。
不提供任何收款、退款、抵扣、争议冻结或资金状态迁移接口。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models_billing import (
    AdjustmentCandidate, AttributionConflict, BillingFragment, BillingOperator,
    BillingSnapshot, DifferenceBridgeEntry,
    ExternalBillingEntry, PriceVersion, ReceiptMatch, TenureRecord,
)
from ..schemas_billing import (
    AdjustmentDecisionIn, AttributionDecisionIn, ExternalBillingEntryIn,
    OperatorIn, PriceVersionIn, SnapshotConfirmIn, SnapshotIn, TenureIn,
)
from ..services.billing_bridge import (
    build_bridge, decide_adjustment, scan_post_freeze_revisions,
)
from ..services.billing_compiler import compile_billing
from ..services.billing_external import ingest_external_entry
from ..services.billing_ledger import superseded_fragment_ids
from ..services.billing_matcher import run_matching
from ..services.billing_review import decide_attribution_conflict, review_projection, trace_difference
from ..services.billing_snapshot import confirm_snapshot, create_snapshot, snapshot_items
from ..services import billing_master as bm

router = APIRouter(prefix="/api/billing", tags=["用水计费双时间对账"])


# ---------------- 经营户与归属沿革 ----------------
@router.post("/operators", status_code=201)
def add_operator(d: OperatorIn, db: Session = Depends(get_db)):
    try:
        obj = bm.create_operator(db, d); db.commit()
        return {"id": obj.id, "code": obj.code}
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.get("/operators")
def list_operators(db: Session = Depends(get_db)):
    return [{"id": o.id, "code": o.code, "name": o.name, "status": o.status}
            for o in db.scalars(select(BillingOperator).order_by(BillingOperator.code))]


@router.post("/tenures", status_code=201)
def add_tenure(d: TenureIn, db: Session = Depends(get_db)):
    try:
        obj = bm.register_tenure(db, d); db.commit()
        return {"id": obj.id, "tenure_uid": obj.tenure_uid,
                "tenure_version": obj.tenure_version}
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.get("/tenures")
def list_tenures(plot_code: str | None = None, db: Session = Depends(get_db)):
    stmt = select(TenureRecord).order_by(TenureRecord.plot_code,
                                         TenureRecord.effective_from)
    if plot_code:
        stmt = stmt.where(TenureRecord.plot_code == plot_code)
    return [{"id": t.id, "tenure_uid": t.tenure_uid,
             "tenure_version": t.tenure_version, "plot_code": t.plot_code,
             "operator_code": t.operator_code, "lease_code": t.lease_code,
             "effective_from": t.effective_from.isoformat(),
             "effective_to": t.effective_to.isoformat() if t.effective_to else None,
             "change_reason": t.change_reason, "evidence_ref": t.evidence_ref,
             "recorded_at": t.recorded_at.isoformat()}
            for t in db.scalars(stmt)]


# ---------------- 价目版本 ----------------
@router.post("/price-versions", status_code=201)
def add_price_version(d: PriceVersionIn, db: Session = Depends(get_db)):
    try:
        obj = bm.publish_price_version(db, d); db.commit()
        return {"id": obj.id, "version_no": obj.version_no}
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.get("/price-versions")
def list_price_versions(db: Session = Depends(get_db)):
    out = []
    for p in db.scalars(select(PriceVersion).order_by(PriceVersion.version_no)):
        out.append({
            "id": p.id, "version_no": p.version_no, "code": p.code, "name": p.name,
            "plot_code": p.plot_code,
            "approved_at": p.approved_at.isoformat(),
            "effective_from": p.effective_from.isoformat(),
            "effective_to": p.effective_to.isoformat() if p.effective_to else None,
            "status": p.status, "season_note": p.season_note,
            "tiers": [{"tier_no": t.tier_no, "volume_from": t.volume_from,
                       "volume_to": t.volume_to, "unit_price": t.unit_price,
                       "season_tag": t.season_tag} for t in p.tiers],
            "relief_rules": [{"relief_code": r.relief_code, "name": r.name,
                              "relief_kind": r.relief_kind, "rate": r.rate,
                              "amount": r.amount, "condition_json": r.condition_json}
                             for r in p.relief_rules]})
    return out


# ---------------- 外部账务事实 ----------------
@router.post("/external-entries", status_code=201)
def add_external_entry(d: ExternalBillingEntryIn, db: Session = Depends(get_db)):
    r = ingest_external_entry(db, d)
    db.commit()
    return {"accepted": r.accepted, "stored_uid": r.stored_uid,
            "fingerprint": r.fingerprint, "message": r.message}


@router.get("/external-entries")
def list_external_entries(plot_code: str | None = None,
                          period_start: datetime | None = None,
                          period_end: datetime | None = None,
                          db: Session = Depends(get_db)):
    stmt = select(ExternalBillingEntry).order_by(
        ExternalBillingEntry.received_at, ExternalBillingEntry.id)
    if plot_code:
        stmt = stmt.where(ExternalBillingEntry.plot_code == plot_code)
    if period_start:
        stmt = stmt.where(ExternalBillingEntry.period_end > period_start)
    if period_end:
        stmt = stmt.where(ExternalBillingEntry.period_start < period_end)
    return [_external_dict(x) for x in db.scalars(stmt)]


# ---------------- 区间分摊编译 ----------------
@router.post("/compile", status_code=201)
def post_compile(period_start: datetime, period_end: datetime,
                 plot_codes: str | None = None, db: Session = Depends(get_db)):
    codes = [c.strip() for c in plot_codes.split(",")] if plot_codes else None
    r = compile_billing(db, period_start, period_end, codes)
    db.commit()
    return r


@router.get("/fragments")
def list_fragments(period_key: str | None = None, plot_code: str | None = None,
                   status: str | None = None, db: Session = Depends(get_db)):
    dead = superseded_fragment_ids(db)
    stmt = select(BillingFragment).order_by(BillingFragment.interval_start,
                                            BillingFragment.id)
    rows = [f for f in db.scalars(stmt) if f.id not in dead]
    if period_key:
        rows = [f for f in rows if f.period_key == period_key]
    if plot_code:
        rows = [f for f in rows if f.plot_code == plot_code]
    if status:
        rows = [f for f in rows if f.status == status]
    return [_fragment_dict(f) for f in rows]


# ---------------- 账期快照 ----------------
@router.post("/snapshots", status_code=201)
def post_snapshot(d: SnapshotIn, db: Session = Depends(get_db)):
    snap = create_snapshot(db, d.period_start, d.period_end,
                           trigger=d.trigger, note=d.note,
                           plot_codes=d.plot_codes)
    db.commit()
    return _snapshot_dict(snap)


@router.get("/snapshots")
def list_snapshots(db: Session = Depends(get_db)):
    return [_snapshot_dict(s) for s in db.scalars(
        select(BillingSnapshot).order_by(BillingSnapshot.id.desc()))]


@router.post("/snapshots/{snapshot_id}/confirm")
def post_confirm_snapshot(snapshot_id: int, d: SnapshotConfirmIn,
                          db: Session = Depends(get_db)):
    try:
        snap = confirm_snapshot(db, snapshot_id, d.operator, d.note)
        db.commit()
        return _snapshot_dict(snap)
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.get("/snapshots/{snapshot_id}/items")
def get_snapshot_items(snapshot_id: int, item_kind: str | None = None,
                       db: Session = Depends(get_db)):
    if not db.get(BillingSnapshot, snapshot_id):
        raise HTTPException(404, "快照不存在")
    return [{"id": i.id, "item_kind": i.item_kind, "ref_id": i.ref_id,
             "ref_uid": i.ref_uid, "content": i.content}
            for i in snapshot_items(db, snapshot_id, item_kind)]


# ---------------- 回执匹配 ----------------
@router.post("/matching", status_code=201)
def post_matching(period_start: datetime, period_end: datetime,
                  snapshot_id: int | None = None, db: Session = Depends(get_db)):
    snap = db.get(BillingSnapshot, snapshot_id) if snapshot_id else None
    if snapshot_id and not snap:
        raise HTTPException(404, "快照不存在")
    r = run_matching(db, period_start, period_end, snap)
    db.commit()
    return r


@router.get("/receipt-matches")
def list_matches(period_key: str | None = None, plot_code: str | None = None,
                 db: Session = Depends(get_db)):
    stmt = select(ReceiptMatch).order_by(ReceiptMatch.id.desc())
    if period_key:
        stmt = stmt.where(ReceiptMatch.period_key == period_key)
    if plot_code:
        stmt = stmt.where(ReceiptMatch.plot_code == plot_code)
    return [_match_dict(m) for m in db.scalars(stmt)]


# ---------------- 差异桥与调整候选 ----------------
@router.post("/bridge", status_code=201)
def post_bridge(period_start: datetime, period_end: datetime,
                snapshot_id: int | None = None, db: Session = Depends(get_db)):
    snap = db.get(BillingSnapshot, snapshot_id) if snapshot_id else None
    if snapshot_id and not snap:
        raise HTTPException(404, "快照不存在")
    r = build_bridge(db, period_start, period_end, snap)
    db.commit()
    return r


@router.get("/bridge")
def get_bridge(period_key: str | None = None, plot_code: str | None = None,
               column_kind: str | None = None, db: Session = Depends(get_db)):
    stmt = select(DifferenceBridgeEntry).order_by(
        DifferenceBridgeEntry.plot_code, DifferenceBridgeEntry.id)
    if period_key:
        stmt = stmt.where(DifferenceBridgeEntry.period_key == period_key)
    if plot_code:
        stmt = stmt.where(DifferenceBridgeEntry.plot_code == plot_code)
    if column_kind:
        stmt = stmt.where(DifferenceBridgeEntry.column_kind == column_kind)
    return [{"id": e.id, "bridge_uid": e.bridge_uid, "period_key": e.period_key,
             "plot_code": e.plot_code, "column_kind": e.column_kind,
             "source_kind": e.source_kind, "source_ref": e.source_ref,
             "volume": e.volume, "amount": e.amount, "diff_reason": e.diff_reason,
             "narration": e.narration, "snapshot_id": e.snapshot_id}
            for e in db.scalars(stmt)]


@router.post("/adjustments/scan", status_code=201)
def post_scan_adjustments(period_start: datetime, period_end: datetime,
                          db: Session = Depends(get_db)):
    cands = scan_post_freeze_revisions(db, period_start, period_end)
    db.commit()
    return {"candidates": len(cands),
            "candidate_ids": [c.id for c in cands]}


@router.get("/adjustments")
def list_adjustments(period_key: str | None = None, db: Session = Depends(get_db)):
    stmt = select(AdjustmentCandidate).order_by(AdjustmentCandidate.id.desc())
    if period_key:
        stmt = stmt.where(AdjustmentCandidate.period_key == period_key)
    return [_adjustment_dict(c) for c in db.scalars(stmt)]


@router.post("/adjustments/{candidate_id}/decide")
def post_decide_adjustment(candidate_id: int, d: AdjustmentDecisionIn,
                           db: Session = Depends(get_db)):
    try:
        c = decide_adjustment(db, candidate_id, d.decision, d.rationale, d.operator)
        db.commit()
        return _adjustment_dict(c)
    except ValueError as e:
        raise HTTPException(409, str(e))


# ---------------- 归属冲突裁定 ----------------
@router.get("/attribution-conflicts")
def list_attribution_conflicts(period_key: str | None = None,
                               db: Session = Depends(get_db)):
    stmt = select(AttributionConflict).order_by(AttributionConflict.id.desc())
    if period_key:
        stmt = stmt.where(AttributionConflict.period_key == period_key)
    return [_conflict_dict(c) for c in db.scalars(stmt)]


@router.post("/attribution-conflicts/{conflict_id}/decide")
def post_decide_conflict(conflict_id: int, d: AttributionDecisionIn,
                         db: Session = Depends(get_db)):
    try:
        c = decide_attribution_conflict(db, conflict_id, d.decision,
                                        d.rationale, d.operator)
        db.commit()
        return _conflict_dict(c)
    except ValueError as e:
        raise HTTPException(409, str(e))


# ---------------- 审阅投影与差额回查 ----------------
@router.get("/review")
def get_review(period_start: datetime, period_end: datetime,
               plot_code: str | None = None, db: Session = Depends(get_db)):
    return review_projection(db, period_start, period_end, plot_code)


@router.get("/trace")
def get_trace(fragment_id: int | None = None, bridge_entry_id: int | None = None,
              match_id: int | None = None, db: Session = Depends(get_db)):
    if not (fragment_id or bridge_entry_id or match_id):
        raise HTTPException(400, "需提供 fragment_id / bridge_entry_id / match_id 之一")
    return trace_difference(db, fragment_id=fragment_id,
                            bridge_entry_id=bridge_entry_id, match_id=match_id)


# ---------------- 串行器 ----------------
def _external_dict(x: ExternalBillingEntry) -> dict:
    return {"id": x.id, "entry_uid": x.entry_uid, "source_system": x.source_system,
            "entry_kind": x.entry_kind, "receipt_no": x.receipt_no,
            "bill_line_no": x.bill_line_no,
            "bill_line_fingerprint": x.bill_line_fingerprint,
            "plot_code": x.plot_code,
            "period_start": x.period_start.isoformat(),
            "period_end": x.period_end.isoformat(),
            "amount": x.amount, "direction": x.direction, "status": x.status,
            "external_ref": x.external_ref,
            "event_time": x.event_time.isoformat(),
            "received_at": x.received_at.isoformat()}


def _fragment_dict(f: BillingFragment) -> dict:
    return {"id": f.id, "fragment_uid": f.fragment_uid, "period_key": f.period_key,
            "fragment_kind": f.fragment_kind, "status": f.status,
            "supply_fact_id": f.supply_fact_id, "device_id": f.device_id,
            "meter_point_code": f.meter_point_code, "plot_code": f.plot_code,
            "interval_start": f.interval_start.isoformat(),
            "interval_end": f.interval_end.isoformat(),
            "volume": f.volume, "split_basis": f.split_basis,
            "blocking_reason": f.blocking_reason,
            "tenure_id": f.tenure_id, "tenure_version": f.tenure_version,
            "operator_code": f.operator_code, "lease_code": f.lease_code,
            "price_version_id": f.price_version_id,
            "price_version_no": f.price_version_no, "tier_no": f.tier_no,
            "unit_price": f.unit_price, "base_amount": f.base_amount,
            "relief_amount": f.relief_amount, "amount": f.amount,
            "conflict_group_uid": f.conflict_group_uid,
            "evidence_message_ids": f.evidence_message_ids,
            "evidence_manual_ids": f.evidence_manual_ids, "detail": f.detail,
            "supersedes_fragment_id": f.supersedes_fragment_id,
            "replaces_fragment_ids": f.replaces_fragment_ids,
            "revision_no": f.revision_no}


def _snapshot_dict(s: BillingSnapshot) -> dict:
    return {"id": s.id, "snapshot_uid": s.snapshot_uid, "period_key": s.period_key,
            "period_start": s.period_start.isoformat(),
            "period_end": s.period_end.isoformat(), "status": s.status,
            "blockers": s.blockers, "tenure_version_nos": s.tenure_version_nos,
            "price_version_nos": s.price_version_nos,
            "mapping_version_nos": s.mapping_version_nos, "summary": s.summary,
            "trigger": s.trigger, "operator": s.operator, "note": s.note,
            "frozen_at": s.frozen_at.isoformat()}


def _match_dict(m: ReceiptMatch) -> dict:
    return {"id": m.id, "match_uid": m.match_uid, "period_key": m.period_key,
            "plot_code": m.plot_code, "receipt_no": m.receipt_no,
            "match_status": m.match_status, "entry_ids": m.entry_ids,
            "fragment_ids": m.fragment_ids,
            "bill_line_fingerprint": m.bill_line_fingerprint,
            "direction": m.direction, "amount": m.amount, "detail": m.detail,
            "snapshot_id": m.snapshot_id}


def _adjustment_dict(c: AdjustmentCandidate) -> dict:
    return {"id": c.id, "candidate_uid": c.candidate_uid, "period_key": c.period_key,
            "plot_code": c.plot_code, "original_snapshot_id": c.original_snapshot_id,
            "trigger_kind": c.trigger_kind, "status": c.status,
            "impact_volume": c.impact_volume, "impact_amount": c.impact_amount,
            "evidence_refs": c.evidence_refs, "detail": c.detail,
            "decision_operator": c.decision_operator, "rationale": c.rationale,
            "decided_at": c.decided_at.isoformat() if c.decided_at else None}


def _conflict_dict(c: AttributionConflict) -> dict:
    return {"id": c.id, "conflict_uid": c.conflict_uid,
            "conflict_group_uid": c.conflict_group_uid,
            "period_key": c.period_key, "plot_code": c.plot_code,
            "interval_start": c.interval_start.isoformat(),
            "interval_end": c.interval_end.isoformat(),
            "tenure_operator_code": c.tenure_operator_code,
            "claimed_operator_code": c.claimed_operator_code,
            "amount_lower": c.amount_lower, "amount_upper": c.amount_upper,
            "status": c.status, "evidence_refs": c.evidence_refs,
            "decision_operator": c.decision_operator, "rationale": c.rationale,
            "decided_at": c.decided_at.isoformat() if c.decided_at else None}
