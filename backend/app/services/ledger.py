"""追加式事实账的统一账本期视图。

事实行永不物理删除或原地改写；重算以"新行取代旧行"方式留痕。
一个新事实可能取代多条旧事实（如证据冲突的设备/人工双候选），
因此活动判定必须同时考虑单值列 supersedes_fact_id 与列表 replaces_fact_ids。
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import SupplyFact


def superseded_fact_ids(db: Session) -> set[int]:
    dead: set[int] = set()
    for (sid,) in db.execute(
            select(SupplyFact.supersedes_fact_id).where(
                SupplyFact.supersedes_fact_id.is_not(None))).all():
        if sid is not None:
            dead.add(sid)
    for f in db.scalars(select(SupplyFact).where(
            SupplyFact.replaces_fact_ids.is_not(None))).all():
        dead.update(f.replaces_fact_ids or [])
    return dead


def active_facts(db: Session) -> list[SupplyFact]:
    dead = superseded_fact_ids(db)
    return [f for f in db.scalars(select(SupplyFact)).all() if f.id not in dead]
