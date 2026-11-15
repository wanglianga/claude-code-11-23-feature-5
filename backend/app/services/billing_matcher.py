"""回执匹配器：以外部回执号、账单行指纹和金额方向识别重复、缺失与冲突。

规则（题面硬约束）：
- 回执号重复且账单行指纹与金额方向都相同 => duplicate_idempotent，幂等重传，
  仅首条（canonical）参与金额核对，其余登记为重复证据；
- 回执号重复但金额方向或账单行指纹不同 => conflict，两条记录并列标为冲突，
  绝不把后一条当作幂等重传；冲突组整体不参与净额核对，避免重复制造差额噪声；
- 有应计片段无外部账务 => missing_external；有外部行无应计依据 => missing_internal；
- 同地块同方向、净额一致 => matched。
匹配结果为派生工作记录：重跑替换未绑定确认快照的部分，冻结快照绑定结果保留。
"""
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models_billing import (
    BillingFragment, BillingSnapshot, ExternalBillingEntry, ReceiptMatch,
    MATCH_CONFLICT, MATCH_DUPLICATE, MATCH_MATCHED, MATCH_MISSING_EXTERNAL,
    MATCH_MISSING_INTERNAL, SNAPSHOT_CONFIRMED, FRAG_CONFIRMED,
)
from .billing_common import money, uid
from .billing_ledger import active_fragments

AMOUNT_EPS = 0.01


def _frag_amounts(fragments: list[BillingFragment]) -> dict[str, float]:
    """地块 -> 应计金额（仅确认片段；候选/阻断不参与金额核对）。"""
    out: dict[str, float] = defaultdict(float)
    for f in fragments:
        if f.status == FRAG_CONFIRMED and f.amount is not None:
            out[f.plot_code] += f.amount
    return {k: money(v) for k, v in out.items()}


def run_matching(db: Session, period_start, period_end,
                 snapshot: BillingSnapshot | None = None) -> dict:
    pkey = f"{period_start.year:04d}-{period_start.month:02d}"
    # 派生工作结果：重跑替换未冻结部分；已确认快照绑定的匹配保留以支持复演
    confirmed_snap_ids = {s.id for s in db.scalars(select(BillingSnapshot).where(
        BillingSnapshot.period_key == pkey,
        BillingSnapshot.status == SNAPSHOT_CONFIRMED)).all()}
    for m in db.scalars(select(ReceiptMatch).where(
            ReceiptMatch.period_key == pkey)).all():
        if not m.snapshot_id or m.snapshot_id not in confirmed_snap_ids:
            db.delete(m)
    db.flush()

    entries = [x for x in db.scalars(select(ExternalBillingEntry).where(
        ExternalBillingEntry.period_start < period_end,
        ExternalBillingEntry.period_end > period_start)
        .order_by(ExternalBillingEntry.received_at, ExternalBillingEntry.id)).all()]
    fragments = [f for f in active_fragments(db, pkey)
                 if f.interval_start < period_end and f.interval_end > period_start]
    accrual = _frag_amounts(fragments)
    snap_id = snapshot.id if snapshot else None
    results: list[ReceiptMatch] = []

    # ---- 1. 按回执号分组 ----
    groups: dict[str, list[ExternalBillingEntry]] = defaultdict(list)
    no_receipt: list[ExternalBillingEntry] = []
    # 幂等重传首条仍参与净额核对，但不再单独生成匹配行（已有 duplicate 行代表）
    canonical_dup_ids: set[int] = set()
    for x in entries:
        (groups[x.receipt_no].append(x) if x.receipt_no else no_receipt.append(x))

    for receipt_no, rows in groups.items():
        if len(rows) > 1:
            fps = {r.bill_line_fingerprint for r in rows}
            dirs = {r.direction for r in rows}
            if len(fps) == 1 and len(dirs) == 1:
                # 幂等重传：仅首条参与后续净额核对
                canonical = rows[0]
                canonical_dup_ids.add(canonical.id)
                no_receipt.append(canonical)
                results.append(ReceiptMatch(
                    match_uid=uid("mtch"), period_key=pkey,
                    plot_code=rows[0].plot_code, receipt_no=receipt_no,
                    match_status=MATCH_DUPLICATE,
                    entry_ids=[r.id for r in rows],
                    bill_line_fingerprint=rows[0].bill_line_fingerprint,
                    direction=rows[0].direction,
                    amount=money(canonical.amount),
                    detail={"canonical_entry_uid": canonical.entry_uid,
                            "duplicate_entry_uids": [r.entry_uid for r in rows[1:]],
                            "reason": "同回执号同指纹同方向，判定为幂等重传，仅首条参与对账"},
                    snapshot_id=snap_id))
            else:
                # 同号冲突：并列留痕，冲突组整体不参与净额核对
                results.append(ReceiptMatch(
                    match_uid=uid("mtch"), period_key=pkey,
                    plot_code=rows[0].plot_code, receipt_no=receipt_no,
                    match_status=MATCH_CONFLICT, entry_ids=[r.id for r in rows],
                    bill_line_fingerprint=rows[0].bill_line_fingerprint,
                    direction=rows[0].direction,
                    amount=money(sum(r.amount for r in rows)),
                    detail={
                        "reason": "同回执号但账单行指纹或金额方向不同，并列标为冲突，"
                                  "不得按幂等重传处理",
                        "entries": [{"entry_uid": r.entry_uid, "direction": r.direction,
                                     "amount": r.amount,
                                     "fingerprint": r.bill_line_fingerprint,
                                     "entry_kind": r.entry_kind,
                                     "received_at": r.received_at.isoformat()}
                                    for r in rows]},
                    snapshot_id=snap_id))
        else:
            no_receipt.append(rows[0])

    # ---- 2. 单条外部行与应计口径核对（幂等组首条已由 duplicate 行代表，跳过发单条行）----
    external_by_plot: dict[str, float] = defaultdict(float)
    for x in no_receipt:
        external_by_plot[x.plot_code or ""] += (
            x.amount if x.direction == "debit" else -x.amount)
        if x.id in canonical_dup_ids:
            continue
        plot_frags = [f for f in fragments
                      if f.plot_code == x.plot_code and f.status == FRAG_CONFIRMED]
        has_basis = bool(plot_frags)
        results.append(ReceiptMatch(
            match_uid=uid("mtch"), period_key=pkey, plot_code=x.plot_code,
            receipt_no=x.receipt_no,
            match_status=MATCH_MATCHED if has_basis else MATCH_MISSING_INTERNAL,
            entry_ids=[x.id], fragment_ids=[f.id for f in plot_frags],
            bill_line_fingerprint=x.bill_line_fingerprint,
            direction=x.direction, amount=money(x.amount),
            detail={"entry_uid": x.entry_uid, "entry_kind": x.entry_kind,
                    "status": x.status,
                    "reason": None if has_basis else "外部账务行缺少内部应计片段依据"},
            snapshot_id=snap_id))

    # ---- 3. 有应计无外部账务 ----
    covered_plots = {x.plot_code for x in entries if x.plot_code}
    for plot, amount in sorted(accrual.items()):
        if plot not in covered_plots:
            plot_frags = [f for f in fragments if f.plot_code == plot]
            results.append(ReceiptMatch(
                match_uid=uid("mtch"), period_key=pkey, plot_code=plot,
                receipt_no=None, match_status=MATCH_MISSING_EXTERNAL,
                entry_ids=[], fragment_ids=[f.id for f in plot_frags],
                direction="debit", amount=amount,
                detail={"reason": "存在应计金额但账期内无任何外部账务行/回执"},
                snapshot_id=snap_id))

    # ---- 4. 参与核对的净额与应计不一致 => 冲突线索 ----
    for plot, ext_net in sorted(external_by_plot.items()):
        if plot in accrual and abs(money(ext_net) - accrual[plot]) > AMOUNT_EPS:
            results.append(ReceiptMatch(
                match_uid=uid("mtch"), period_key=pkey, plot_code=plot,
                receipt_no=None, match_status=MATCH_CONFLICT,
                entry_ids=[x.id for x in no_receipt if x.plot_code == plot],
                fragment_ids=[f.id for f in fragments
                              if f.plot_code == plot and f.status == FRAG_CONFIRMED],
                direction="debit",
                amount=money(abs(money(ext_net) - accrual[plot])),
                detail={"reason": "应计金额与外部账务净额不一致",
                        "internal_accrual": accrual[plot],
                        "external_net": money(ext_net)},
                snapshot_id=snap_id))

    db.add_all(results)
    db.flush()
    counts: dict[str, int] = defaultdict(int)
    for r in results:
        counts[r.match_status] += 1
    return {"matches": len(results), "status_counts": dict(counts),
            "match_ids": [r.id for r in results]}
