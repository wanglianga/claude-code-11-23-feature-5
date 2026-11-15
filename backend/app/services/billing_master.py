"""计费主数据服务：经营户、归属沿革（双时间 SCD2）、价目版本。

纪律：
- 归属沿革只追加：新转包/更正关闭旧开放区间并新开一行，历史行永不修改；
- 价目按批准版本整体保存，新版本生效时旧版本置 superseded 但保留行；
- 所有点查询严格按业务时间（effective_from <= at < effective_to）。
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models_billing import (
    BillingOperator, PriceReliefRule, PriceTier, PriceVersion, TenureRecord,
)
from ..schemas_billing import OperatorIn, PriceVersionIn, TenureIn
from .billing_common import uid


# ---------------- 经营户 ----------------
def create_operator(db: Session, d: OperatorIn) -> BillingOperator:
    if db.scalar(select(BillingOperator).where(BillingOperator.code == d.code)):
        raise ValueError(f"经营户 {d.code} 已存在")
    obj = BillingOperator(**d.model_dump())
    db.add(obj); db.flush()
    return obj


# ---------------- 归属沿革 ----------------
def next_tenure_version(db: Session) -> int:
    last = db.scalar(select(TenureRecord.tenure_version).order_by(
        TenureRecord.tenure_version.desc()).limit(1))
    return (last or 0) + 1


def register_tenure(db: Session, d: TenureIn) -> TenureRecord:
    """登记一条归属沿革；同一地块存在开放区间时先关闭（含回溯登记的转包）。

    回溯场景：effective_from 早于既有开放区间起点时，旧开放区间从新行生效点关闭，
    已冻结快照引用的旧行不受影响（快照保存内容副本）。
    """
    if d.effective_to is not None and d.effective_to <= d.effective_from:
        raise ValueError("归属生效结束时间必须晚于开始时间")
    open_rows = db.scalars(select(TenureRecord).where(
        TenureRecord.plot_code == d.plot_code,
        TenureRecord.effective_to.is_(None))).all()
    vno = next_tenure_version(db)
    for row in open_rows:
        if d.effective_from > row.effective_from:
            row.effective_to = d.effective_from
        elif d.effective_from == row.effective_from and \
                row.operator_code == d.operator_code:
            raise ValueError("该地块同一生效点已存在相同经营户的开放归属")
        else:
            # 回溯登记且生效点不晚于旧行起点：旧开放区间整体让位于新行
            row.effective_to = d.effective_from
    record = TenureRecord(
        tenure_uid=uid("ten"), tenure_version=vno, plot_code=d.plot_code,
        operator_code=d.operator_code, lease_code=d.lease_code,
        effective_from=d.effective_from, effective_to=d.effective_to,
        change_reason=d.change_reason, evidence_ref=d.evidence_ref,
        recorded_at=d.recorded_at)
    db.add(record); db.flush()
    return record


def tenure_at(db: Session, plot_code: str, at) -> TenureRecord | None:
    """业务时间点查询：at 时刻地块的有效归属。"""
    return db.scalar(select(TenureRecord).where(
        TenureRecord.plot_code == plot_code,
        TenureRecord.effective_from <= at,
        (TenureRecord.effective_to.is_(None)) | (TenureRecord.effective_to > at)))


def tenures_overlapping(db: Session, plot_code: str, start, end) -> list[TenureRecord]:
    """与 [start, end) 相交的全部归属沿革行（含历史行）。"""
    rows = db.scalars(select(TenureRecord).where(
        TenureRecord.plot_code == plot_code,
        TenureRecord.effective_from < end,
        (TenureRecord.effective_to.is_(None)) | (TenureRecord.effective_to > start)
    ).order_by(TenureRecord.effective_from)).all()
    return list(rows)


# ---------------- 价目版本 ----------------
def next_price_version_no(db: Session) -> int:
    last = db.scalar(select(PriceVersion.version_no).order_by(
        PriceVersion.version_no.desc()).limit(1))
    return (last or 0) + 1


def publish_price_version(db: Session, d: PriceVersionIn) -> PriceVersion:
    if not d.tiers:
        raise ValueError("价目版本至少包含一个阶梯")
    vno = next_price_version_no(db)
    # 关闭同作用域内与新生效区间重叠的旧版本（全局新版本不关闭地块专用版本）
    old_rows = db.scalars(select(PriceVersion).where(
        PriceVersion.status == "approved",
        PriceVersion.plot_code.is_(None) if d.plot_code is None
        else PriceVersion.plot_code == d.plot_code,
        PriceVersion.effective_from < (d.effective_to or d.effective_from),
        (PriceVersion.effective_to.is_(None)) |
        (PriceVersion.effective_to > d.effective_from))).all()
    for old in old_rows:
        old.status = "superseded"
        if old.effective_to is None or old.effective_to > d.effective_from:
            old.effective_to = d.effective_from
    pv = PriceVersion(
        version_no=vno, code=d.code, name=d.name, plot_code=d.plot_code,
        approved_at=d.approved_at, effective_from=d.effective_from,
        effective_to=d.effective_to,
        season_note=d.season_note, note=d.note)
    for t in d.tiers:
        pv.tiers.append(PriceTier(
            tier_no=t.tier_no, volume_from=t.volume_from, volume_to=t.volume_to,
            unit_price=t.unit_price, season_tag=t.season_tag))
    for r in d.relief_rules:
        pv.relief_rules.append(PriceReliefRule(
            relief_code=r.relief_code, name=r.name, relief_kind=r.relief_kind,
            rate=r.rate, amount=r.amount, condition_json=r.condition_json))
    db.add(pv); db.flush()
    return pv


def price_version_at(db: Session, at, plot_code: str | None = None
                     ) -> PriceVersion | None:
    """业务时间点查询：优先地块专用价目，其次全局价目。"""
    if plot_code:
        scoped = db.scalar(select(PriceVersion).where(
            PriceVersion.plot_code == plot_code,
            PriceVersion.effective_from <= at,
            (PriceVersion.effective_to.is_(None)) |
            (PriceVersion.effective_to > at)))
        if scoped:
            return scoped
    return db.scalar(select(PriceVersion).where(
        PriceVersion.plot_code.is_(None),
        PriceVersion.effective_from <= at,
        (PriceVersion.effective_to.is_(None)) | (PriceVersion.effective_to > at)))


def price_versions_overlapping(db: Session, start, end,
                               plot_code: str | None = None) -> list[PriceVersion]:
    """与 [start,end) 相交且适用于地块的价目。

    若该地块存在专用价目版本，只采用专用版本（全局价目不再并行生效）；
    否则回退到全局价目。按生效时间排序。
    """
    base_where = [
        PriceVersion.effective_from < end,
        (PriceVersion.effective_to.is_(None)) |
        (PriceVersion.effective_to > start)]
    if plot_code:
        scoped = list(db.scalars(select(PriceVersion).where(
            *base_where, PriceVersion.plot_code == plot_code)).all())
        if scoped:
            return sorted(scoped, key=lambda p: p.effective_from)
    return list(db.scalars(select(PriceVersion).where(
        *base_where, PriceVersion.plot_code.is_(None)).order_by(
        PriceVersion.effective_from)).all())


def applicable_relief(pv: PriceVersion, plot_code: str,
                      operator_code: str | None = None) -> PriceReliefRule | None:
    """减免匹配：condition_json 可限定 plot_codes / operator_codes；空条件=全部适用。"""
    for rule in pv.relief_rules:
        cond = rule.condition_json or {}
        plots = cond.get("plot_codes")
        operators = cond.get("operator_codes")
        if plots and plot_code not in plots:
            continue
        if operators and (not operator_code or operator_code not in operators):
            continue
        return rule
    return None
