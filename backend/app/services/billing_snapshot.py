"""账期快照器：冻结某次对账所见的计量、归属与价目版本。

- 快照保存每个引用行的完整 JSON 副本（billing_snapshot_items），
  后续迟到数据/转包更正/价目批准均不影响快照复演；
- 快照含 blocked_pending 片段或未裁定归属候选时状态为 blocked，
  禁止进入确认对账；全部可解释才允许 confirm；
- 冻结后不可变：confirm 只改快照自身状态，items 永不更新；
  冻结后的修订走 adjustment_candidates，原快照保持原样。
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import SupplyFact
from ..models_billing import (
    BillingFragment, BillingSnapshot, BillingSnapshotItem, ExternalBillingEntry,
    PriceVersion, SNAPSHOT_BLOCKED, SNAPSHOT_CONFIRMED, SNAPSHOT_READY,
    TenureRecord, FRAG_BLOCKED, FRAG_CANDIDATE,
)
from .billing_common import money, period_key_of, uid, volume
from .billing_ledger import active_fragments


def _fragment_content(f: BillingFragment) -> dict:
    return {
        "id": f.id, "fragment_uid": f.fragment_uid, "period_key": f.period_key,
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
        "revision_no": f.revision_no,
    }


def _tenure_content(t: TenureRecord) -> dict:
    return {
        "id": t.id, "tenure_uid": t.tenure_uid, "tenure_version": t.tenure_version,
        "plot_code": t.plot_code, "operator_code": t.operator_code,
        "lease_code": t.lease_code,
        "effective_from": t.effective_from.isoformat(),
        "effective_to": t.effective_to.isoformat() if t.effective_to else None,
        "change_reason": t.change_reason, "evidence_ref": t.evidence_ref,
        "recorded_at": t.recorded_at.isoformat(),
    }


def _price_content(p: PriceVersion) -> dict:
    return {
        "id": p.id, "version_no": p.version_no, "code": p.code, "name": p.name,
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
                         for r in p.relief_rules],
    }


def _fact_content(f: SupplyFact) -> dict:
    return {
        "id": f.id, "fact_uid": f.fact_uid, "fact_kind": f.fact_kind,
        "status": f.status, "device_id": f.device_id,
        "meter_point_code": f.meter_point_code, "channel_code": f.channel_code,
        "plot_code": f.plot_code,
        "interval_start": f.interval_start.isoformat(),
        "interval_end": f.interval_end.isoformat(),
        "reading_start": f.reading_start, "reading_end": f.reading_end,
        "value": f.value, "method": f.method, "truncated": f.truncated,
        "is_late": f.is_late, "quality_flag": f.quality_flag,
        "evidence_message_ids": f.evidence_message_ids,
        "evidence_manual_ids": f.evidence_manual_ids,
        "mapping_version": f.mapping_version, "revision_no": f.revision_no,
    }


def _external_content(x: ExternalBillingEntry) -> dict:
    return {
        "id": x.id, "entry_uid": x.entry_uid, "source_system": x.source_system,
        "entry_kind": x.entry_kind, "receipt_no": x.receipt_no,
        "bill_line_no": x.bill_line_no,
        "bill_line_fingerprint": x.bill_line_fingerprint,
        "plot_code": x.plot_code,
        "period_start": x.period_start.isoformat(),
        "period_end": x.period_end.isoformat(),
        "amount": x.amount, "direction": x.direction, "status": x.status,
        "external_ref": x.external_ref,
        "event_time": x.event_time.isoformat(),
        "received_at": x.received_at.isoformat(),
    }


def create_snapshot(db: Session, period_start, period_end,
                    trigger: str = "manual", operator: str = "admin",
                    note: str = "", plot_codes: list[str] | None = None,
                    frozen_at_override=None) -> BillingSnapshot:
    pkey = period_key_of(period_start)
    fragments = [f for f in active_fragments(db, pkey)
                 if f.interval_start < period_end and f.interval_end > period_start
                 and (not plot_codes or f.plot_code in plot_codes)]
    blockers = []
    for f in fragments:
        if f.status == FRAG_BLOCKED:
            blockers.append({"kind": "blocked_fragment", "fragment_id": f.id,
                             "reason": f.blocking_reason, "plot_code": f.plot_code,
                             "interval": [f.interval_start.isoformat(),
                                          f.interval_end.isoformat()]})
        elif f.status == FRAG_CANDIDATE:
            blockers.append({"kind": "attribution_conflict",
                             "conflict_group_uid": f.conflict_group_uid,
                             "plot_code": f.plot_code})
    # 候选去重
    seen, uniq = set(), []
    for b in blockers:
        k = b.get("conflict_group_uid") or b.get("fragment_id")
        if k not in seen:
            seen.add(k); uniq.append(b)
    blockers = uniq

    tenure_ids = {f.tenure_id for f in fragments if f.tenure_id}
    price_ids = {f.price_version_id for f in fragments if f.price_version_id}
    fact_ids = {f.supply_fact_id for f in fragments if f.supply_fact_id}
    tenures = [db.get(TenureRecord, i) for i in sorted(x for x in tenure_ids if x)]
    prices = [db.get(PriceVersion, i) for i in sorted(x for x in price_ids if x)]
    facts = [db.get(SupplyFact, i) for i in sorted(x for x in fact_ids if x)]
    externals = [x for x in db.scalars(select(ExternalBillingEntry).where(
        ExternalBillingEntry.period_start < period_end,
        ExternalBillingEntry.period_end > period_start)).all()
        if not plot_codes or x.plot_code in plot_codes]
    mapping_nos = sorted({f.mapping_version for f in facts if f.mapping_version})

    confirmed = [f for f in fragments if f.status == "confirmed"]
    summary = {
        "plot_scope": plot_codes or [],
        "fragment_count": len(fragments),
        "confirmed_volume": volume(sum(f.volume or 0.0 for f in confirmed)),
        "confirmed_amount": money(sum(f.amount or 0.0 for f in confirmed)),
        "blocked_count": sum(1 for f in fragments if f.status == FRAG_BLOCKED),
        "candidate_count": sum(1 for f in fragments if f.status == FRAG_CANDIDATE),
        "external_debit": money(sum(x.amount for x in externals
                                    if x.direction == "debit")),
        "external_credit": money(sum(x.amount for x in externals
                                     if x.direction == "credit")),
    }

    snap = BillingSnapshot(
        snapshot_uid=uid("snap"), period_key=pkey,
        period_start=period_start, period_end=period_end,
        status=SNAPSHOT_BLOCKED if blockers else SNAPSHOT_READY,
        blockers=blockers,
        tenure_version_nos=sorted({t.tenure_version for t in tenures}),
        price_version_nos=sorted({p.version_no for p in prices}),
        mapping_version_nos=mapping_nos, summary=summary,
        trigger=trigger, operator=operator, note=note,
        frozen_at=frozen_at_override or datetime.utcnow())
    db.add(snap); db.flush()

    items = []
    for f in fragments:
        items.append(BillingSnapshotItem(
            snapshot_id=snap.id, item_kind="fragment", ref_id=f.id,
            ref_uid=f.fragment_uid, content=_fragment_content(f)))
    for t in tenures:
        items.append(BillingSnapshotItem(
            snapshot_id=snap.id, item_kind="tenure", ref_id=t.id,
            ref_uid=t.tenure_uid, content=_tenure_content(t)))
    for p in prices:
        items.append(BillingSnapshotItem(
            snapshot_id=snap.id, item_kind="price_version", ref_id=p.id,
            ref_uid=f"price-v{p.version_no}", content=_price_content(p)))
    for f in facts:
        items.append(BillingSnapshotItem(
            snapshot_id=snap.id, item_kind="supply_fact", ref_id=f.id,
            ref_uid=f.fact_uid, content=_fact_content(f)))
    for x in externals:
        items.append(BillingSnapshotItem(
            snapshot_id=snap.id, item_kind="external_entry", ref_id=x.id,
            ref_uid=x.entry_uid, content=_external_content(x)))
    db.add_all(items)
    db.flush()
    return snap


def confirm_snapshot(db: Session, snapshot_id: int, operator: str, note: str
                     ) -> BillingSnapshot:
    snap = db.get(BillingSnapshot, snapshot_id)
    if not snap:
        raise ValueError("账期快照不存在")
    if snap.status == SNAPSHOT_CONFIRMED:
        raise ValueError("快照已确认冻结，不可重复确认")
    if snap.status == SNAPSHOT_BLOCKED:
        raise ValueError("快照含待证片段或未裁定归属冲突，阻断确认对账；"
                         "请先补齐中间读数或完成凭证裁定")
    snap.status = SNAPSHOT_CONFIRMED
    snap.operator = operator
    snap.note = (snap.note + f" | [{operator}] 确认冻结: {note}").strip(" |")
    db.flush()
    return snap


def snapshot_items(db: Session, snapshot_id: int, item_kind: str | None = None
                   ) -> list[BillingSnapshotItem]:
    stmt = select(BillingSnapshotItem).where(
        BillingSnapshotItem.snapshot_id == snapshot_id)
    if item_kind:
        stmt = stmt.where(BillingSnapshotItem.item_kind == item_kind)
    return list(db.scalars(stmt.order_by(BillingSnapshotItem.id)).all())
