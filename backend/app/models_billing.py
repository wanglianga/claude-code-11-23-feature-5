"""用水计费双时间对账 ORM 模型。

四本账 + 三类派生记录，全部沿用基线的追加式 / SCD2 纪律：

1. 归属沿革账 tenure_records：经营户-地块-租约的有效区间（业务时间）
   与登记时间（系统时间）双时间保存，转包只新开区间、不改历史行；
2. 价目版本账 price_versions / price_tiers / price_relief_rules：
   阶梯、季节价与减免规则按批准版本保存；
3. 外部账务事实账 external_billing_entries：只接入账单行/冲补行/回执号/状态，
   原样留痕、幂等接入，不调用任何支付接口；
4. 分摊事实账 billing_fragments：计量区间经归属/价目生效点拆分后的可解释片段，
   跨价目缺中间读数只发 blocked_pending 待证片段，重算以版本链取代旧片段；
5. 账期快照 billing_snapshots(+items) 冻结某次对账所见版本，冻结后不可变；
   回执匹配 receipt_matches 识别重复/缺失/冲突；
   差异桥 difference_bridge_entries 分三列列示口径；
   调整候选 adjustment_candidates 只引用原快照，永不回改快照。
"""
from datetime import datetime

from sqlalchemy import (
    DateTime, Float, ForeignKey, Integer, String, Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# ---- 片段类型 / 状态 ----
FRAG_NORMAL = "normal"                       # 常规计量片段
FRAG_TIER_SPLIT = "tier_split"               # 同一价目内跨阶梯拆分
FRAG_CROSS_TENURE = "cross_tenure_split"     # 跨租约/转包生效点拆分
FRAG_CROSS_PRICE_PENDING = "cross_price_pending"   # 跨价目待证（禁止猜测拆分）
FRAG_ATTRIBUTION_DISPUTED = "attribution_disputed"  # 归属双候选（凭证裁定前）

FRAG_CONFIRMED = "confirmed"
FRAG_BLOCKED = "blocked_pending"   # 阻断进入确认对账
FRAG_CANDIDATE = "candidate"

# ---- 外部账务行类型 / 金额方向 ----
ENTRY_BILL = "bill_line"           # 账单行
ENTRY_ADJUSTMENT = "adjustment_line"  # 冲补行
ENTRY_RECEIPT = "receipt"          # 回执

DIR_DEBIT = "debit"                # 应收方向（正方向）
DIR_CREDIT = "credit"              # 冲减方向

# ---- 回执匹配结论 ----
MATCH_MATCHED = "matched"
MATCH_DUPLICATE = "duplicate_idempotent"   # 同号同指纹同方向：幂等重传
MATCH_CONFLICT = "conflict"                # 同号但方向/指纹不同：并列冲突
MATCH_MISSING_EXTERNAL = "missing_external"   # 有应计无外部账务
MATCH_MISSING_INTERNAL = "missing_internal"   # 有外部行无应计依据

# ---- 差异桥三列 ----
BRIDGE_METERING = "metering_accrual"   # 应计量口径
BRIDGE_EXTERNAL = "external_fact"      # 外部账务事实
BRIDGE_LATE = "late_revision"          # 后补修订影响

# ---- 快照状态 ----
SNAPSHOT_READY = "ready"          # 无阻断项，可进入确认对账
SNAPSHOT_BLOCKED = "blocked"      # 含待证/冲突，禁止确认对账
SNAPSHOT_CONFIRMED = "confirmed"  # 已作为确认对账依据冻结

# ---- 调整候选状态 ----
ADJ_PROPOSED = "proposed"
ADJ_ACCEPTED = "accepted"
ADJ_REJECTED = "rejected"


class BillingOperator(Base):
    """经营户（用水归属主体）。审阅投影仅在必要时输出编码，不暴露无关户信息。"""
    __tablename__ = "billing_operators"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/closed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class TenureRecord(Base):
    """归属沿革账：经营户-地块-租约的 SCD2 双时间记录。

    effective_from/effective_to 为业务有效区间（半开区间），
    recorded_at 为该行登记入系统的时间；转包生效日早于登记日时，
    历史区间照样追加，旧行不被覆盖。
    """
    __tablename__ = "tenure_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenure_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    tenure_version: Mapped[int] = mapped_column(Integer, index=True)
    plot_code: Mapped[str] = mapped_column(String(64), index=True)
    operator_code: Mapped[str] = mapped_column(String(64), index=True)
    lease_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    change_reason: Mapped[str] = mapped_column(String(32), default="initial")
    # initial / transfer 转包 / lease_end / correction
    evidence_ref: Mapped[str | None] = mapped_column(String(96), nullable=True)  # 协议/凭证号
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class PriceVersion(Base):
    """价目版本账：每次批准生成新版本，旧版本保留可回放。"""
    __tablename__ = "price_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_no: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    # 作用域：空=全局价目；非空=仅该地块适用
    plot_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="approved")  # approved/superseded
    # 季节价说明（如旺季 6-10 月），结构化条件存 JSON
    season_note: Mapped[str] = mapped_column(String(255), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    tiers: Mapped[list["PriceTier"]] = relationship(
        back_populates="price_version", cascade="all, delete-orphan")
    relief_rules: Mapped[list["PriceReliefRule"]] = relationship(
        back_populates="price_version", cascade="all, delete-orphan")


class PriceTier(Base):
    """阶梯价目行：按账期累计水量落在的边际阶梯计价。"""
    __tablename__ = "price_tiers"
    id: Mapped[int] = mapped_column(primary_key=True)
    price_version_id: Mapped[int] = mapped_column(
        ForeignKey("price_versions.id"), index=True)
    tier_no: Mapped[int] = mapped_column(Integer)
    volume_from: Mapped[float] = mapped_column(Float)       # 含下限 m3
    volume_to: Mapped[float | None] = mapped_column(Float, nullable=True)  # 空=敞开阶梯
    unit_price: Mapped[float] = mapped_column(Float)        # 元/m3
    season_tag: Mapped[str | None] = mapped_column(String(32), nullable=True)

    price_version: Mapped["PriceVersion"] = relationship(back_populates="tiers")


class PriceReliefRule(Base):
    """减免规则（批准版本内生效）：比例减免或定额减免，可限定地块。"""
    __tablename__ = "price_relief_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    price_version_id: Mapped[int] = mapped_column(
        ForeignKey("price_versions.id"), index=True)
    relief_code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    relief_kind: Mapped[str] = mapped_column(String(16))  # rate / fixed
    rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 适用条件，如 {"plot_codes": ["PLOT-T1"]}；空对象=全部适用
    condition_json: Mapped[dict] = mapped_column(JSONB, default=dict)

    price_version: Mapped["PriceVersion"] = relationship(back_populates="relief_rules")


class ExternalBillingEntry(Base):
    """外部账务事实账：只存账单行、冲补行、回执号与状态，不做任何资金动作。

    entry_uid 幂等；bill_line_fingerprint 为去除回执号后的业务指纹，
    用于区分「同号幂等重传」与「同号内容冲突」。
    """
    __tablename__ = "external_billing_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    entry_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    source_system: Mapped[str] = mapped_column(String(64), index=True)
    entry_kind: Mapped[str] = mapped_column(String(16), index=True)
    receipt_no: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    bill_line_no: Mapped[str | None] = mapped_column(String(96), nullable=True)
    bill_line_fingerprint: Mapped[str] = mapped_column(String(96), index=True)
    plot_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    amount: Mapped[float] = mapped_column(Float)               # 恒为非负，方向看 direction
    direction: Mapped[str] = mapped_column(String(16))         # debit / credit
    status: Mapped[str] = mapped_column(String(24), default="issued")
    external_ref: Mapped[str | None] = mapped_column(String(96), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)  # 原样留痕
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class BillingFragment(Base):
    """分摊事实片段：计量区间 × 归属版本 × 价目版本 的最小可解释计价单元。

    追加式：重编译以 supersedes_fragment_id / replaces_fragment_ids 取代旧片段，
    旧片段原样保留，快照可按冻结时的活动集复演。
    """
    __tablename__ = "billing_fragments"
    id: Mapped[int] = mapped_column(primary_key=True)
    fragment_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    period_key: Mapped[str] = mapped_column(String(16), index=True)   # 如 2026-11
    fragment_kind: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    # 计量回查链
    supply_fact_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    meter_point_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plot_code: Mapped[str] = mapped_column(String(64), index=True)
    interval_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    split_basis: Mapped[str] = mapped_column(String(32), default="reading_anchored")
    # reading_anchored / time_proportional / pending_evidence
    blocking_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 归属版本（固化）
    tenure_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenure_records.id"), nullable=True)
    tenure_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    operator_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lease_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 价目版本（固化）
    price_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("price_versions.id"), nullable=True)
    price_version_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tier_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    relief_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("price_relief_rules.id"), nullable=True)
    base_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    relief_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 双归属候选归组；证据链与待证说明
    conflict_group_uid: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    evidence_message_ids: Mapped[list] = mapped_column(JSONB, default=list)
    evidence_manual_ids: Mapped[list] = mapped_column(JSONB, default=list)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 追加式版本链
    supersedes_fragment_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    replaces_fragment_ids: Mapped[list] = mapped_column(JSONB, default=list)
    revision_no: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    tenure: Mapped["TenureRecord | None"] = relationship()
    price_version: Mapped["PriceVersion | None"] = relationship()


class BillingSnapshot(Base):
    """账期快照：冻结某次对账所见的计量、归属与价目版本，一经冻结不可变。"""
    __tablename__ = "billing_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    period_key: Mapped[str] = mapped_column(String(16), index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), index=True, default=SNAPSHOT_BLOCKED)
    blockers: Mapped[list] = mapped_column(JSONB, default=list)
    # 冻结时刻的版本指针，保证不依赖后续主数据状态
    tenure_version_nos: Mapped[list] = mapped_column(JSONB, default=list)
    price_version_nos: Mapped[list] = mapped_column(JSONB, default=list)
    mapping_version_nos: Mapped[list] = mapped_column(JSONB, default=list)
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    trigger: Mapped[str] = mapped_column(String(32), default="manual")
    operator: Mapped[str] = mapped_column(String(64), default="admin")
    note: Mapped[str] = mapped_column(Text, default="")
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    items: Mapped[list["BillingSnapshotItem"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan")


class BillingSnapshotItem(Base):
    """快照项：对冻结行的完整 JSON 副本，后续修订不影响快照复演。"""
    __tablename__ = "billing_snapshot_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("billing_snapshots.id"), index=True)
    item_kind: Mapped[str] = mapped_column(String(32), index=True)
    # fragment / tenure / price_version / supply_fact / external_entry
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ref_uid: Mapped[str | None] = mapped_column(String(96), nullable=True)
    content: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    snapshot: Mapped["BillingSnapshot"] = relationship(back_populates="items")


class ReceiptMatch(Base):
    """回执匹配结果：重复（幂等）、冲突（并列）、缺失均留痕，追加式。"""
    __tablename__ = "receipt_matches"
    id: Mapped[int] = mapped_column(primary_key=True)
    match_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    period_key: Mapped[str] = mapped_column(String(16), index=True)
    plot_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    receipt_no: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    match_status: Mapped[str] = mapped_column(String(32), index=True)
    entry_ids: Mapped[list] = mapped_column(JSONB, default=list)
    fragment_ids: Mapped[list] = mapped_column(JSONB, default=list)
    bill_line_fingerprint: Mapped[str | None] = mapped_column(String(96), nullable=True)
    direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("billing_snapshots.id"), nullable=True)
    supersedes_match_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class DifferenceBridgeEntry(Base):
    """差异桥：应计量口径 / 外部账务事实 / 后补修订影响 三列分别列示。"""
    __tablename__ = "difference_bridge_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    bridge_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    period_key: Mapped[str] = mapped_column(String(16), index=True)
    plot_code: Mapped[str] = mapped_column(String(64), index=True)
    column_kind: Mapped[str] = mapped_column(String(24), index=True)
    source_kind: Mapped[str] = mapped_column(String(32))
    # fragment / external_entry / adjustment_candidate / match
    source_ref: Mapped[dict] = mapped_column(JSONB, default=dict)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    diff_reason: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    narration: Mapped[str] = mapped_column(Text, default="")
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("billing_snapshots.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class AdjustmentCandidate(Base):
    """冻结后修订的调整候选：只引用原快照，原快照保持可复演。"""
    __tablename__ = "adjustment_candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    period_key: Mapped[str] = mapped_column(String(16), index=True)
    plot_code: Mapped[str] = mapped_column(String(64), index=True)
    original_snapshot_id: Mapped[int] = mapped_column(ForeignKey("billing_snapshots.id"))
    trigger_kind: Mapped[str] = mapped_column(String(32))
    # manual_reading_correction / late_backfill / tenure_correction / price_correction
    status: Mapped[str] = mapped_column(String(16), default=ADJ_PROPOSED, index=True)
    impact_volume: Mapped[float] = mapped_column(Float, default=0.0)
    impact_amount: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_refs: Mapped[dict] = mapped_column(JSONB, default=dict)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    decision_operator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    original_snapshot: Mapped["BillingSnapshot"] = relationship()


class AttributionConflict(Base):
    """归属冲突工作项：转包生效日与人工补录区间矛盾时登记，等待凭证裁定。"""
    __tablename__ = "attribution_conflicts"
    id: Mapped[int] = mapped_column(primary_key=True)
    conflict_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    conflict_group_uid: Mapped[str] = mapped_column(String(96), index=True)
    period_key: Mapped[str] = mapped_column(String(16), index=True)
    plot_code: Mapped[str] = mapped_column(String(64), index=True)
    interval_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tenure_operator_code: Mapped[str] = mapped_column(String(64))
    claimed_operator_code: Mapped[str] = mapped_column(String(64))
    amount_lower: Mapped[float] = mapped_column(Float)
    amount_upper: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending / adjudicated
    evidence_refs: Mapped[dict] = mapped_column(JSONB, default=dict)
    decision_operator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
