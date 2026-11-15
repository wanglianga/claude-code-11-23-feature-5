"""计费双时间对账公共工具：UID、账期键、指纹、金额/时间工具。"""
import hashlib
import json
import uuid
from datetime import datetime, timezone


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def now() -> datetime:
    return datetime.now(timezone.utc)


def period_key_of(start: datetime) -> str:
    return f"{start.year:04d}-{start.month:02d}"


def bill_line_fingerprint(source_system: str, entry_kind: str, bill_line_no: str | None,
                          plot_code: str | None, period_start: datetime,
                          period_end: datetime, amount: float, direction: str,
                          status: str, external_ref: str | None) -> str:
    """业务指纹刻意排除回执号/接入 UID/接收时间：

    同一账单行的幂等重传指纹相同；金额或方向变化即产生不同指纹，
    供匹配器区分「幂等重传」与「同号冲突」。
    """
    canonical = json.dumps({
        "source_system": source_system, "entry_kind": entry_kind,
        "bill_line_no": bill_line_no, "plot_code": plot_code,
        "period_start": period_start.isoformat(), "period_end": period_end.isoformat(),
        "amount": round(float(amount), 2), "direction": direction,
        "status": status, "external_ref": external_ref,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:24]


def money(v: float | None) -> float | None:
    if v is None:
        return None
    return round(float(v) + 1e-9, 2)


def volume(v: float | None) -> float | None:
    if v is None:
        return None
    return round(float(v) + 1e-9, 3)


def overlap(a_start: datetime, a_end: datetime,
            b_start: datetime, b_end: datetime) -> tuple[datetime, datetime] | None:
    s, e = max(a_start, b_start), min(a_end, b_end)
    return (s, e) if s < e else None
