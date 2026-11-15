"""外部账务事实账接入服务。

只接入账单行 / 冲补行 / 回执号 / 状态，原样留痕；entry_uid 幂等。
本模块不调用、也不提供任何支付/退款/资金状态迁移接口。
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models_billing import ExternalBillingEntry
from ..schemas_billing import ExternalBillingEntryIn
from .billing_common import bill_line_fingerprint


def ingest_external_entry(db: Session, d: ExternalBillingEntryIn):
    existing = db.scalar(select(ExternalBillingEntry).where(
        ExternalBillingEntry.entry_uid == d.entry_uid))
    if existing:
        return type("R", (), {
            "accepted": False, "stored_uid": d.entry_uid,
            "fingerprint": existing.bill_line_fingerprint,
            "message": "重复外部账务行已幂等忽略"})()

    fp = bill_line_fingerprint(
        d.source_system, d.entry_kind, d.bill_line_no, d.plot_code,
        d.period_start, d.period_end, d.amount, d.direction, d.status,
        d.external_ref)
    entry = ExternalBillingEntry(
        entry_uid=d.entry_uid, source_system=d.source_system,
        entry_kind=d.entry_kind, receipt_no=d.receipt_no,
        bill_line_no=d.bill_line_no, bill_line_fingerprint=fp,
        plot_code=d.plot_code, period_start=d.period_start,
        period_end=d.period_end, amount=d.amount, direction=d.direction,
        status=d.status, external_ref=d.external_ref, payload=d.payload,
        event_time=d.event_time, received_at=d.received_at)
    db.add(entry); db.flush()
    return type("R", (), {"accepted": True, "stored_uid": entry.entry_uid,
                          "fingerprint": fp, "message": "外部账务事实已原样入账"})()


def entries_for_period(db: Session, period_start, period_end,
                       plot_code: str | None = None) -> list[ExternalBillingEntry]:
    stmt = select(ExternalBillingEntry).where(
        ExternalBillingEntry.period_start < period_end,
        ExternalBillingEntry.period_end > period_start)
    if plot_code:
        stmt = stmt.where(ExternalBillingEntry.plot_code == plot_code)
    return list(db.scalars(stmt.order_by(ExternalBillingEntry.received_at,
                                         ExternalBillingEntry.id)).all())
