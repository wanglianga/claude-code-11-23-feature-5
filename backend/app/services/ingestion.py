"""数据契约登记 + 接入暂存服务。

关键纪律：
- 报文原样落 raw_messages，重复 message_uid 幂等拒绝；
- 补传（supersedes_uid）写入新行并标记 revision_no/is_late，绝不覆盖历史行；
- 人工补录落 manual_revisions，依据(basis)缺失会被契约校验记录违规但仍留痕。
"""
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    DataContract, ManualRevision, RawMessage, QualityEvent,
)
from ..schemas import ContractIn, ManualRevisionIn, RawMessageIn

RULE_PREFIX = "CONTRACT"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def register_contract(db: Session, data: ContractIn) -> DataContract:
    existing = db.scalar(select(DataContract).where(DataContract.code == data.code))
    if existing:
        raise ValueError(f"契约 {data.code} 已登记，契约不可原地修改，请登记新版本")
    contract = DataContract(**data.model_dump())
    db.add(contract)
    db.flush()
    return contract


def get_contract(db: Session, code: str) -> DataContract | None:
    return db.scalar(select(DataContract).where(DataContract.code == code))


def _raise_quality(db: Session, rule: str, severity: str, detail: dict,
                   device_id: str | None = None) -> None:
    db.add(QualityEvent(
        event_uid=f"qe_{uuid.uuid4().hex[:16]}",
        rule_code=rule, severity=severity, device_id=device_id, detail=detail,
    ))


def validate_against_contract(contract: DataContract, device_id: str,
                              payload: dict, unit: str | None = None) -> list[str]:
    violations: list[str] = []
    if not re.fullmatch(contract.device_id_pattern, device_id):
        violations.append(
            f"设备编号 {device_id} 不匹配契约 {contract.code} 的模式 {contract.device_id_pattern}")
    for field, rule in (contract.payload_schema or {}).items():
        if rule.get("required") and field not in payload:
            violations.append(f"缺少契约必填字段 {field}")
    if unit and unit != contract.unit:
        violations.append(f"计量单位 {unit} 与契约单位 {contract.unit} 不一致")
    return violations


def ingest_raw(db: Session, data: RawMessageIn, batch: str) -> object:
    # 幂等：同一 message_uid 已存在则拒绝，不做覆盖
    if db.scalar(select(RawMessage.id).where(RawMessage.message_uid == data.message_uid)):
        return type("R", (), {"accepted": False, "stored_uid": data.message_uid,
                              "is_late": False, "revision_no": 0,
                              "violations": [], "message": "重复报文已幂等忽略"})()

    contract = get_contract(db, data.contract_code)
    violations: list[str] = []
    if not contract:
        violations.append(f"契约 {data.contract_code} 未登记")
    else:
        unit = data.payload.get("unit")
        violations = validate_against_contract(contract, data.device_id, data.payload, unit)

    # 迟到判定：接收时间相对事件时间超过允许迟到窗口 => 迟到补传
    lateness = data.received_at - data.event_time
    is_late = lateness > timedelta(minutes=settings.allowed_lateness_minutes)

    revision_no = 1
    if data.supersedes_uid:
        prev = db.scalar(select(RawMessage).where(
            RawMessage.message_uid == data.supersedes_uid))
        revision_no = (prev.revision_no + 1) if prev else 1

    msg = RawMessage(
        message_uid=data.message_uid,
        contract_code=data.contract_code,
        device_id=data.device_id,
        payload=data.payload,
        event_time=data.event_time,
        received_at=data.received_at,
        source_version=data.source_version,
        ingestion_batch=batch,
        supersedes_uid=data.supersedes_uid,
        revision_no=revision_no,
        is_late=is_late,
    )
    db.add(msg)
    db.flush()

    if violations:
        _raise_quality(db, f"{RULE_PREFIX}_VIOLATION", "critical",
                       {"message_uid": data.message_uid, "violations": violations},
                       device_id=data.device_id)
    if is_late:
        _raise_quality(db, "LATE_ARRIVAL", "warning",
                       {"message_uid": data.message_uid,
                        "event_time": data.event_time.isoformat(),
                        "received_at": data.received_at.isoformat(),
                        "lateness_minutes": round(lateness.total_seconds() / 60, 1)},
                       device_id=data.device_id)
    return type("R", (), {"accepted": True, "stored_uid": msg.message_uid,
                          "is_late": is_late, "revision_no": revision_no,
                          "violations": violations, "message": "已原样入暂存区"})()


def ingest_manual(db: Session, data: ManualRevisionIn, batch: str) -> object:
    if db.scalar(select(ManualRevision.id).where(
            ManualRevision.revision_uid == data.revision_uid)):
        return type("R", (), {"accepted": False, "stored_uid": data.revision_uid,
                              "is_late": False, "revision_no": 0,
                              "violations": [], "message": "重复人工修订已幂等忽略"})()

    contract = get_contract(db, data.contract_code)
    violations: list[str] = []
    if contract:
        violations = validate_against_contract(
            contract, data.device_id, {"value": data.value, "unit": data.unit}, data.unit)
    else:
        violations.append(f"契约 {data.contract_code} 未登记")

    if not (data.basis or "").strip():
        violations.append("人工补录缺少依据(basis)")
        _raise_quality(db, "MANUAL_WITHOUT_BASIS", "critical",
                       {"revision_uid": data.revision_uid,
                        "device_id": data.device_id,
                        "event_time": data.event_time.isoformat()},
                       device_id=data.device_id)

    rev = ManualRevision(
        revision_uid=data.revision_uid,
        contract_code=data.contract_code,
        device_id=data.device_id,
        event_time=data.event_time,
        value=data.value,
        unit=data.unit,
        basis=data.basis,
        operator=data.operator,
        payload={"value": data.value, "unit": data.unit, "basis": data.basis,
                 **({"claimed_operator_code": data.claimed_operator_code}
                    if data.claimed_operator_code else {})},
        received_at=data.received_at,
        supersedes_uid=data.supersedes_uid,
    )
    db.add(rev)
    db.flush()
    is_late = (data.received_at - data.event_time) > timedelta(
        minutes=settings.allowed_lateness_minutes)
    return type("R", (), {"accepted": True, "stored_uid": rev.revision_uid,
                          "is_late": is_late, "revision_no": 1,
                          "violations": violations, "message": "人工修订已原样留痕"})()
