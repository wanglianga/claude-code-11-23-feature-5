"""用水计费双时间对账的 Pydantic 入参/出参。"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------- 经营户与归属沿革 ----------------
class OperatorIn(BaseModel):
    code: str
    name: str
    status: str = Field(default="active", pattern="^(active|closed)$")


class TenureIn(BaseModel):
    plot_code: str
    operator_code: str
    lease_code: str | None = None
    effective_from: datetime
    effective_to: datetime | None = None
    change_reason: str = Field(
        default="initial",
        pattern="^(initial|transfer|lease_end|correction)$")
    evidence_ref: str | None = None
    recorded_at: datetime


# ---------------- 价目版本 ----------------
class PriceTierIn(BaseModel):
    tier_no: int = Field(ge=1)
    volume_from: float = Field(ge=0)
    volume_to: float | None = None
    unit_price: float = Field(ge=0)
    season_tag: str | None = None


class PriceReliefIn(BaseModel):
    relief_code: str
    name: str
    relief_kind: str = Field(pattern="^(rate|fixed)$")
    rate: float | None = Field(default=None, ge=0, le=1)
    amount: float | None = Field(default=None, ge=0)
    condition_json: dict[str, Any] = {}


class PriceVersionIn(BaseModel):
    code: str
    name: str
    plot_code: str | None = None
    approved_at: datetime
    effective_from: datetime
    effective_to: datetime | None = None
    season_note: str = ""
    note: str = ""
    tiers: list[PriceTierIn]
    relief_rules: list[PriceReliefIn] = []


# ---------------- 外部账务事实（无任何支付动作） ----------------
class ExternalBillingEntryIn(BaseModel):
    entry_uid: str
    source_system: str
    entry_kind: str = Field(pattern="^(bill_line|adjustment_line|receipt)$")
    receipt_no: str | None = None
    bill_line_no: str | None = None
    plot_code: str | None = None
    period_start: datetime
    period_end: datetime
    amount: float = Field(ge=0)
    direction: str = Field(pattern="^(debit|credit)$")
    status: str = "issued"
    external_ref: str | None = None
    payload: dict[str, Any] = {}
    event_time: datetime
    received_at: datetime


class ExternalIngestResult(BaseModel):
    accepted: bool
    stored_uid: str
    fingerprint: str = ""
    message: str = ""


# ---------------- 账期快照 ----------------
class SnapshotIn(BaseModel):
    period_start: datetime
    period_end: datetime
    plot_codes: list[str] | None = None
    trigger: str = "manual"
    note: str = ""


class SnapshotConfirmIn(BaseModel):
    operator: str = "admin"
    note: str = ""


# ---------------- 调整候选 / 归属冲突裁定 ----------------
class AdjustmentDecisionIn(BaseModel):
    decision: str = Field(pattern="^(accepted|rejected)$")
    rationale: str = ""
    operator: str = "admin"


class AttributionDecisionIn(BaseModel):
    # accept_tenure=采信转包沿革归属；accept_claim=采信人工补录主张归属
    decision: str = Field(pattern="^(accept_tenure|accept_claim)$")
    rationale: str = ""
    operator: str = "admin"
