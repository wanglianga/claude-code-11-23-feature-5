"""分摊片段账的活动视图与版本链工具（与基线 ledger 同构）。

片段行永不物理删除；重编译以新片段取代旧片段，
活动判定同时考虑 supersedes_fragment_id 与 replaces_fragment_ids。
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models_billing import BillingFragment


def superseded_fragment_ids(db: Session) -> set[int]:
    dead: set[int] = set()
    for (sid,) in db.execute(
            select(BillingFragment.supersedes_fragment_id).where(
                BillingFragment.supersedes_fragment_id.is_not(None))).all():
        if sid is not None:
            dead.add(sid)
    for f in db.scalars(select(BillingFragment).where(
            BillingFragment.replaces_fragment_ids.is_not(None))).all():
        dead.update(f.replaces_fragment_ids or [])
    return dead


def active_fragments(db: Session, period_key: str | None = None) -> list[BillingFragment]:
    dead = superseded_fragment_ids(db)
    stmt = select(BillingFragment)
    if period_key:
        stmt = stmt.where(BillingFragment.period_key == period_key)
    return [f for f in db.scalars(stmt).all() if f.id not in dead]


def supersede(new: BillingFragment, old: BillingFragment) -> None:
    new.supersedes_fragment_id = old.id
    ids = set(new.replaces_fragment_ids or [])
    ids.add(old.id)
    new.replaces_fragment_ids = sorted(ids)
    new.revision_no = max(new.revision_no, old.revision_no + 1)
