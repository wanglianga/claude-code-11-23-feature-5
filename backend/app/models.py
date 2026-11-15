"""ORM 模型：全部核心账本采用追加式（append-only）或 SCD2 生效区间结构。

设计要点：
- 暂存区(raw_messages/manual_revisions)永不更新，补传以新行 + supersedes_uid 留痕；
- 主数据映射带 effective_from/effective_to 与 mapping_version；
- 事实账(supply_facts)带状态、证据指针、版本链；
- 指标(metric_versions)每次发布/重算生成新版本，旧版本保留并附差异说明。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# ---- 枚举常量（字符串存储，避免数据库枚举迁移成本） ----
SRC_DEVICE = "device"
SRC_MANUAL = "manual"
SRC_PLAN = "plan"
SRC_SYSTEM = "system"

READ_CUMULATIVE = "cumulative"      # 累计读数
READ_INSTANT = "instantaneous"      # 瞬时流量
READ_PLAN = "plan"                  # 计划目标

FACT_SUPPLY = "supply"              # 供水事实（实灌候选）
FACT_TRANSFER = "transfer"          # 交接计量
FACT_PLAN = "plan"                  # 计划目标事实（只作比对，不作实灌替身）

ST_CANDIDATE = "candidate"          # 候选（证据冲突未裁定）
ST_CONFIRMED = "confirmed"          # 已确认，可进入正式指标
ST_PENDING = "pending_verification"  # 待核实（如换表缺证截断）
ST_REJECTED = "rejected"
ST_TARGET = "target"

METRIC_TENTATIVE = "tentative"
METRIC_STABLE = "stable"
METRIC_FROZEN = "frozen"


class DataContract(Base):
    __tablename__ = "data_contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    source_kind: Mapped[str] = mapped_column(String(16))          # device/manual/plan/system
    reading_type: Mapped[str] = mapped_column(String(16))         # cumulative/instantaneous/plan
    unit: Mapped[str] = mapped_column(String(16))                 # m3 / m3_h
    device_id_pattern: Mapped[str] = mapped_column(String(128))   # 设备编号约束（前缀正则）
    collection_interval_sec: Mapped[int] = mapped_column(Integer)  # 采集区间
    payload_schema: Mapped[dict] = mapped_column(JSONB)           # 必填字段与取值规则
    source_version: Mapped[str] = mapped_column(String(32))
    active_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    active_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class RawMessage(Base):
    """设备报文暂存区：原样保存，补传不覆盖旧行。"""
    __tablename__ = "raw_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    message_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    contract_code: Mapped[str] = mapped_column(String(64), index=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source_version: Mapped[str] = mapped_column(String(32))
    ingestion_batch: Mapped[str] = mapped_column(String(64), index=True)
    supersedes_uid: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    revision_no: Mapped[int] = mapped_column(Integer, default=1)
    is_late: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class ManualRevision(Base):
    """人工修订/补录暂存区：原样保存，必须带依据。"""
    __tablename__ = "manual_revisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    revision_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    contract_code: Mapped[str] = mapped_column(String(64), index=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(16))
    basis: Mapped[str] = mapped_column(Text, default="")          # 补录依据
    operator: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    supersedes_uid: Mapped[str | None] = mapped_column(String(96), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    parent_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    level: Mapped[int] = mapped_column(Integer, default=1)        # 1=干渠/渠首 2=支渠
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class MeterPoint(Base):
    __tablename__ = "meter_points"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    channel_code: Mapped[str] = mapped_column(String(64), index=True)
    point_role: Mapped[str] = mapped_column(String(24))  # head 渠首 / branch 支渠 / field 田间 / transfer 交接
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Plot(Base):
    __tablename__ = "plots"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    area_mu: Mapped[float] = mapped_column(Float)
    channel_code: Mapped[str] = mapped_column(String(64), index=True)  # 供水支渠
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class RotationBatch(Base):
    __tablename__ = "rotation_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    planned_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    planned_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class MappingVersion(Base):
    """映射版本登记：每次主数据/换表关系发布生成一个版本。"""
    __tablename__ = "mapping_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_no: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    note: Mapped[str] = mapped_column(Text, default="")
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)


class DeviceMapping(Base):
    """设备 -> 计量点 的 SCD2 映射，含换表关系与生效区间。"""
    __tablename__ = "device_mappings"
    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    meter_point_code: Mapped[str] = mapped_column(String(64), index=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mapping_version: Mapped[int] = mapped_column(Integer, index=True)
    change_reason: Mapped[str] = mapped_column(String(128), default="initial")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class PlotBatchAllocation(Base):
    """地块参与轮灌批次的生效区间映射。"""
    __tablename__ = "plot_batch_allocations"
    id: Mapped[int] = mapped_column(primary_key=True)
    plot_code: Mapped[str] = mapped_column(String(64), index=True)
    batch_code: Mapped[str] = mapped_column(String(64), index=True)
    planned_volume: Mapped[float] = mapped_column(Float)  # 计划水量 m3（目标，非实灌）
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mapping_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class MeterChange(Base):
    """换表记录：voucher_present=False 即换表缺证。"""
    __tablename__ = "meter_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    meter_point_code: Mapped[str] = mapped_column(String(64), index=True)
    change_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    old_last_reading: Mapped[float | None] = mapped_column(Float, nullable=True)
    new_first_reading: Mapped[float | None] = mapped_column(Float, nullable=True)
    voucher_present: Mapped[bool] = mapped_column(Boolean, default=False)
    voucher_ref: Mapped[str | None] = mapped_column(String(96), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="missing_voucher")  # certified / missing_voucher
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class SupplyFact(Base):
    """供水事实账（追加式）。计划事实 status=target，永不充当实灌。"""
    __tablename__ = "supply_facts"
    id: Mapped[int] = mapped_column(primary_key=True)
    fact_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    fact_kind: Mapped[str] = mapped_column(String(16), index=True)   # supply/transfer/plan
    status: Mapped[str] = mapped_column(String(24), index=True)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    meter_point_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    channel_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    plot_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    batch_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    interval_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reading_start: Mapped[float | None] = mapped_column(Float, nullable=True)
    reading_end: Mapped[float | None] = mapped_column(Float, nullable=True)
    value: Mapped[float] = mapped_column(Float)                      # 水量 m3
    unit: Mapped[str] = mapped_column(String(16), default="m3")
    method: Mapped[str] = mapped_column(String(32))
    # cumulative_delta / flow_integration / manual / handover / plan
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)  # 换表缺证截断
    is_late: Mapped[bool] = mapped_column(Boolean, default=False)
    quality_flag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_message_ids: Mapped[list] = mapped_column(JSONB, default=list)
    evidence_manual_ids: Mapped[list] = mapped_column(JSONB, default=list)
    mapping_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contract_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    meter_change_id: Mapped[int | None] = mapped_column(ForeignKey("meter_changes.id"), nullable=True)
    adjudication_id: Mapped[int | None] = mapped_column(ForeignKey("adjudications.id"), nullable=True)
    supersedes_fact_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 一个新事实可能取代多条旧事实（如证据冲突的双候选），用列表完整记录取代链
    replaces_fact_ids: Mapped[list] = mapped_column(JSONB, default=list)
    revision_no: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    meter_change: Mapped["MeterChange"] = relationship()


class QualityEvent(Base):
    __tablename__ = "quality_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    rule_code: Mapped[str] = mapped_column(String(48), index=True)
    severity: Mapped[str] = mapped_column(String(16))               # info/warning/critical
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    meter_point_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    interval_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    interval_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)  # open/adjudicated/frozen/closed
    related_fact_ids: Mapped[list] = mapped_column(JSONB, default=list)
    reconciliation_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_tasks.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Adjudication(Base):
    """人工裁定：证据冲突时选择采信哪一份证据。"""
    __tablename__ = "adjudications"
    id: Mapped[int] = mapped_column(primary_key=True)
    conflict_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    interval_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decision: Mapped[str] = mapped_column(String(24), default="pending")
    # pending / accept_device / accept_manual / reject_both
    selected_evidence: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    operator: Mapped[str] = mapped_column(String(64), default="")
    quality_event_id: Mapped[int | None] = mapped_column(ForeignKey("quality_events.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReconciliationTask(Base):
    """渠道守恒失衡对账任务；冻结期间损耗指标不得用计划量补平。"""
    __tablename__ = "reconciliation_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    channel_code: Mapped[str] = mapped_column(String(64), index=True)
    interval_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    upstream_value: Mapped[float] = mapped_column(Float)
    downstream_explained: Mapped[float] = mapped_column(Float)
    diff: Mapped[float] = mapped_column(Float)
    diff_ratio: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)  # open/frozen/resolved
    frozen_metric_keys: Mapped[list] = mapped_column(JSONB, default=list)
    resolution: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RecomputeRun(Base):
    """重算任务登记：每次迟到数据/换表/裁定/对账触发。"""
    __tablename__ = "recompute_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    trigger: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running/done/failed
    affected_keys: Mapped[list] = mapped_column(JSONB, default=list)
    note: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MetricVersion(Base):
    """版本化指标：同一(日期,粒度,键)每次发布追加新版本，可回放。"""
    __tablename__ = "metric_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_no: Mapped[int] = mapped_column(Integer, index=True)
    metric_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    grain_type: Mapped[str] = mapped_column(String(16), index=True)   # plot/channel/batch
    grain_key: Mapped[str] = mapped_column(String(64), index=True)
    batch_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    planned_volume: Mapped[float] = mapped_column(Float, default=0.0)
    confirmed_volume: Mapped[float] = mapped_column(Float, default=0.0)
    pending_volume: Mapped[float] = mapped_column(Float, default=0.0)
    loss_lower: Mapped[float | None] = mapped_column(Float, nullable=True)
    loss_upper: Mapped[float | None] = mapped_column(Float, nullable=True)
    missing_rate: Mapped[float] = mapped_column(Float, default=0.0)
    over_allocation_risk: Mapped[float] = mapped_column(Float, default=0.0)
    plan_completion_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)       # tentative/stable/frozen
    watermark: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stability_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    diff_note: Mapped[str] = mapped_column(Text, default="")
    input_fact_ids: Mapped[list] = mapped_column(JSONB, default=list)
    recompute_run_id: Mapped[int | None] = mapped_column(ForeignKey("recompute_runs.id"), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
