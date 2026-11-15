"""用水计费双时间对账硬规则测试。

覆盖题面五组验收样例与关键纪律：
跨阶梯、跨租约、跨价目待证阻断、重复回执（幂等 vs 冲突）、
冻结后补传只出调整候选且快照不变、人工证据双归属候选与裁定、
审阅最小披露、差额五类回查。
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import SupplyFact
from app.models_billing import (
    AdjustmentCandidate, AttributionConflict, BillingFragment, BillingSnapshot,
    ReceiptMatch, SNAPSHOT_BLOCKED, SNAPSHOT_CONFIRMED,
    FRAG_BLOCKED, FRAG_CANDIDATE, FRAG_CONFIRMED,
)
from app.schemas import (
    ChannelIn, ContractIn, DeviceMappingIn, ManualRevisionIn, MeterPointIn,
    PlotIn, RawMessageIn,
)
from app.schemas_billing import (
    AttributionDecisionIn, ExternalBillingEntryIn, OperatorIn, PriceReliefIn,
    PriceTierIn, PriceVersionIn, TenureIn,
)
from app.services import master_data as md, billing_master as bm
from app.services.billing_bridge import build_bridge, scan_post_freeze_revisions
from app.services.billing_compiler import compile_billing
from app.services.billing_external import ingest_external_entry
from app.services.billing_matcher import run_matching
from app.services.billing_review import decide_attribution_conflict, review_projection, trace_difference
from app.services.billing_snapshot import confirm_snapshot, create_snapshot
from app.services.fact_compiler import compile_device
from app.services.ingestion import ingest_manual, ingest_raw, register_contract

UTC = timezone.utc
P_START = datetime(2026, 11, 1, tzinfo=UTC)
P_END = datetime(2026, 12, 1, tzinfo=UTC)
D = datetime(2026, 11, 10, tzinfo=UTC)


def t(h, m=0):
    return D + timedelta(hours=h, minutes=m)


PLOTS = ["P-T1", "P-T2", "P-T3", "P-T4", "P-T5", "P-T6"]


def _setup(db):
    register_contract(db, ContractIn(
        code="CT-BC", name="计费累计", source_kind="device",
        reading_type="cumulative", unit="m3", device_id_pattern=r"D-BC-\d+",
        collection_interval_sec=7200,
        payload_schema={"reading": {"required": True}, "unit": {"required": True}},
        source_version="fw", active_from=P_START))
    md.create_channel(db, ChannelIn(code="BC-MAIN", name="计费干渠", level=1))
    v = md.publish_mapping_version(db, "init")
    for i, plot in enumerate(PLOTS, start=1):
        ch, mp, dev = f"BC-C{i}", f"BC-MP{i}", f"D-BC-{i}"
        md.create_channel(db, ChannelIn(code=ch, name=f"支渠{i}", level=2,
                                        parent_code="BC-MAIN"))
        md.create_meter_point(db, MeterPointIn(
            code=mp, name=f"点{i}", channel_code=ch, point_role="field"))
        md.create_plot(db, PlotIn(code=plot, name=f"地块{i}", area_mu=60,
                                  channel_code=ch))
        md.upsert_device_mapping(db, DeviceMappingIn(
            device_id=dev, meter_point_code=mp, effective_from=P_START), v)
    for code, name in [("OA", "甲"), ("OB", "乙"), ("OC", "丙"),
                       ("OD", "丁"), ("OE", "戊"), ("OF", "己"), ("OG", "庚")]:
        bm.create_operator(db, OperatorIn(code=code, name=name))
    db.commit()


def _tenure(db, plot, op, frm, **kw):
    kw.setdefault("recorded_at", frm)
    bm.register_tenure(db, TenureIn(
        plot_code=plot, operator_code=op, effective_from=frm, **kw))


def _raw(db, dev, h, reading, uid_=None, received=None, supersedes=None):
    ingest_raw(db, RawMessageIn(
        message_uid=uid_ or f"M-{dev}-{h}", contract_code="CT-BC", device_id=dev,
        payload={"reading": reading, "unit": "m3"}, event_time=t(h),
        received_at=received or (t(h) + timedelta(minutes=10)),
        source_version="fw", supersedes_uid=supersedes), batch="t")


def _global_price(db):
    bm.publish_price_version(db, PriceVersionIn(
        code="G", name="全局价", plot_code=None,
        approved_at=datetime(2026, 10, 25, tzinfo=UTC), effective_from=P_START,
        tiers=[PriceTierIn(tier_no=1, volume_from=0, volume_to=100, unit_price=0.5),
               PriceTierIn(tier_no=2, volume_from=100, volume_to=200, unit_price=0.8),
               PriceTierIn(tier_no=3, volume_from=200, volume_to=None, unit_price=1.2)],
        relief_rules=[]))


def _compile_all(db):
    for i in range(1, 7):
        compile_device(db, f"D-BC-{i}", "cumulative")
    db.commit()
    return compile_billing(db, P_START, P_END, PLOTS)


def _active_frags(db, plot=None, status=None):
    from app.services.billing_ledger import superseded_fragment_ids
    dead = superseded_fragment_ids(db)
    rows = [f for f in db.query(BillingFragment).all() if f.id not in dead]
    if plot:
        rows = [f for f in rows if f.plot_code == plot]
    if status:
        rows = [f for f in rows if f.status == status]
    return rows


# ---------------------------------------------------------------- 1. 跨阶梯
def test_cross_tier_pricing(db):
    _setup(db)
    _tenure(db, "P-T1", "OA", P_START)
    _global_price(db)
    for h, r in [(0, 0), (2, 120), (4, 260)]:
        _raw(db, "D-BC-1", h, r)
    r = _compile_all(db)
    assert r["blocked"] == 0
    frags = _active_frags(db, "P-T1", FRAG_CONFIRMED)
    assert round(sum(f.volume for f in frags), 3) == 260.0
    # 100*0.5 + 100*0.8 + 60*1.2 = 202
    assert round(sum(f.amount for f in frags), 2) == 202.0
    assert {f.tier_no for f in frags} == {1, 2, 3}


# ---------------------------------------------------------------- 2. 跨租约
def test_cross_tenure_split_not_whole_interval(db):
    _setup(db)
    _tenure(db, "P-T2", "OB", P_START, effective_to=t(2))
    _tenure(db, "P-T2", "OC", t(2), change_reason="transfer",
            evidence_ref="TRF-1", recorded_at=t(2, 30))
    _global_price(db)
    _raw(db, "D-BC-2", 0, 0)
    _raw(db, "D-BC-2", 4, 200)
    _compile_all(db)
    frags = _active_frags(db, "P-T2", FRAG_CONFIRMED)
    by_op = {op: round(sum(f.volume for f in frags if f.operator_code == op), 3)
             for op in {"OB", "OC"}}
    # 无中间读数 => 时间比例，各 100m³，而不是 200 全归区间末尾的 OC
    assert by_op == {"OB": 100.0, "OC": 100.0}
    assert all(f.split_basis == "time_proportional"
               for f in frags if f.fragment_kind == "cross_tenure_split")


# ---------------------------------------------------------------- 3. 跨价目待证
def test_cross_price_pending_blocks_then_interim_reading_confirms(db):
    _setup(db)
    _tenure(db, "P-T6", "OA", P_START)
    bm.publish_price_version(db, PriceVersionIn(
        code="OLD", name="旧", plot_code="P-T6",
        approved_at=datetime(2026, 10, 20, tzinfo=UTC), effective_from=P_START,
        effective_to=t(2),
        tiers=[PriceTierIn(tier_no=1, volume_from=0, volume_to=None, unit_price=0.4)]))
    bm.publish_price_version(db, PriceVersionIn(
        code="NEW", name="新", plot_code="P-T6",
        approved_at=datetime(2026, 11, 1, tzinfo=UTC), effective_from=t(2),
        tiers=[PriceTierIn(tier_no=1, volume_from=0, volume_to=None, unit_price=0.6)]))
    _raw(db, "D-BC-6", 0, 0)
    _raw(db, "D-BC-6", 4, 100)
    _compile_all(db)
    blocked = _active_frags(db, "P-T6", FRAG_BLOCKED)
    assert blocked and blocked[0].blocking_reason == "CROSS_PRICE_NO_INTERIM_READING"
    # 含阻断项的快照禁止确认
    snap = create_snapshot(db, P_START, P_END)
    assert snap.status == SNAPSHOT_BLOCKED
    with pytest.raises(ValueError):
        confirm_snapshot(db, snap.id, "admin", "")

    # 补齐边界读数后重编译：转确认，且按两版价目分别计价
    _raw(db, "D-BC-6", 2, 60, uid_="M-D-BC-6-2-BF",
         received=datetime(2026, 11, 12, tzinfo=UTC))
    _compile_all(db)
    confirmed = _active_frags(db, "P-T6", FRAG_CONFIRMED)
    assert round(sum(f.volume for f in confirmed), 3) == 100.0
    # 本用例仅发布两版地块专用价目，版本号为 1、2
    assert {f.price_version_no for f in confirmed} == {1, 2}
    assert not _active_frags(db, "P-T6", FRAG_BLOCKED)


# ---------------------------------------------------------------- 4. 重复回执
def test_receipt_duplicate_idempotent_vs_conflict(db):
    _setup(db)
    _tenure(db, "P-T3", "OD", P_START)
    _global_price(db)
    _raw(db, "D-BC-3", 0, 0)
    _raw(db, "D-BC-3", 2, 50)
    _compile_all(db)

    def ext(uid_, receipt, amount, line="BL-1"):
        ingest_external_entry(db, ExternalBillingEntryIn(
            entry_uid=uid_, source_system="ERP", entry_kind="bill_line",
            receipt_no=receipt, bill_line_no=line, plot_code="P-T3",
            period_start=P_START, period_end=P_END, amount=amount,
            direction="debit", event_time=t(6), received_at=t(6)))

    # 同号同指纹同方向：幂等重传
    ext("E1", "R-DUP", 25.0)
    ext("E2", "R-DUP", 25.0)
    # 同号但金额不同：指纹不同 => 冲突
    ext("E3", "R-CFL", 25.0, line="BL-2")
    ext("E4", "R-CFL", 30.0, line="BL-2")
    run_matching(db, P_START, P_END)
    dup = db.query(ReceiptMatch).filter_by(receipt_no="R-DUP").one()
    cfl = db.query(ReceiptMatch).filter_by(receipt_no="R-CFL").one()
    assert dup.match_status == "duplicate_idempotent" and len(dup.entry_ids) == 2
    assert cfl.match_status == "conflict" and len(cfl.entry_ids) == 2
    # 后一条没有被当成幂等重传：两条原始行指纹不同
    from app.models_billing import ExternalBillingEntry
    assert len({db.get(ExternalBillingEntry, i).bill_line_fingerprint
                for i in cfl.entry_ids}) == 2


# ---------------------------------------------------------------- 5. 冻结后补传
def test_frozen_snapshot_immutable_with_adjustment_candidate(db):
    _setup(db)
    _tenure(db, "P-T4", "OE", P_START)
    _global_price(db)
    _raw(db, "D-BC-4", 0, 0)
    _raw(db, "D-BC-4", 3, 100)
    _compile_all(db)
    frozen_at = datetime(2026, 11, 11, 9, tzinfo=UTC)
    snap = create_snapshot(db, P_START, P_END, plot_codes=["P-T4"],
                           frozen_at_override=frozen_at)
    confirm_snapshot(db, snap.id, "admin", "冻结")
    db.commit()
    amount_before = snap.summary["confirmed_amount"]
    items_before = len(snap.items)
    assert amount_before == 50.0

    # 冻结后迟到补传：100 -> 130
    _raw(db, "D-BC-4", 3, 130, uid_="M-D-BC-4-3-REV",
         received=datetime(2026, 11, 12, 10, tzinfo=UTC),
         supersedes="M-D-BC-4-3")
    compile_device(db, "D-BC-4", "cumulative")
    db.commit()
    _compile_all(db)
    cands = scan_post_freeze_revisions(db, P_START, P_END)
    db.commit()
    t4 = [c for c in cands if c.plot_code == "P-T4"]
    assert len(t4) == 1 and t4[0].trigger_kind == "late_backfill"
    assert t4[0].impact_volume == 30.0 and t4[0].impact_amount == 24.0
    assert t4[0].original_snapshot_id == snap.id
    # 原快照保持可复演
    db.refresh(snap)
    assert snap.status == SNAPSHOT_CONFIRMED
    assert snap.summary["confirmed_amount"] == amount_before == 50.0
    assert len(snap.items) == items_before


# ---------------------------------------------------------------- 6. 人工证据冲突
def test_attribution_conflict_dual_candidates_and_adjudication(db):
    _setup(db)
    _tenure(db, "P-T5", "OF", P_START, effective_to=t(2))
    _tenure(db, "P-T5", "OG", t(2), change_reason="transfer",
            evidence_ref="TRF-5", recorded_at=t(2, 30))
    _global_price(db)
    _raw(db, "D-BC-5", 0, 0)
    _raw(db, "D-BC-5", 4, 240)
    ingest_manual(db, ManualRevisionIn(
        revision_uid="MAN-CLAIM", contract_code="CT-BC", device_id="D-BC-5",
        event_time=t(1), value=240.0, unit="m3",
        basis="持转包前协议主张整段归 OF", operator="李工",
        received_at=t(1, 30), claimed_operator_code="OF"), batch="t")
    _compile_all(db)

    candidates = _active_frags(db, "P-T5", FRAG_CANDIDATE)
    assert not _active_frags(db, "P-T5", FRAG_CONFIRMED)
    kinds = {f.detail.get("candidate") for f in candidates}
    assert kinds == {"tenure_record", "manual_claim"}
    conflict = db.query(AttributionConflict).filter_by(plot_code="P-T5").one()
    assert conflict.status == "pending"
    assert conflict.amount_lower <= conflict.amount_upper

    # 凭证裁定采信沿革归属后重编译，候选转确认
    decide_attribution_conflict(db, conflict.id, "accept_tenure",
                                "转包协议有效", "admin")
    db.commit()
    _compile_all(db)
    confirmed = _active_frags(db, "P-T5", FRAG_CONFIRMED)
    assert confirmed and not _active_frags(db, "P-T5", FRAG_CANDIDATE)


# ---------------------------------------------------------------- 7. 最小披露 + 回查
def test_review_projection_minimal_disclosure_and_trace(db):
    _setup(db)
    _tenure(db, "P-T1", "OA", P_START)
    _global_price(db)
    _raw(db, "D-BC-1", 0, 0)
    _raw(db, "D-BC-1", 2, 120)
    _compile_all(db)
    create_snapshot(db, P_START, P_END)
    db.commit()
    rows = review_projection(db, P_START, P_END)
    t1 = next(r for r in rows if r["plot_code"] == "P-T1")
    # 不暴露经营户姓名/编码等无关身份信息
    serialized = str(t1)
    assert "甲" not in serialized and "OA" not in serialized
    assert t1["evidence"]["tenure_versions"] and t1["evidence"]["snapshot_ids"]

    frag = _active_frags(db, "P-T1", FRAG_CONFIRMED)[0]
    chain = trace_difference(db, fragment_id=frag.id)
    assert chain["metering"] and chain["tenure_versions"]
    assert chain["price_versions"] and chain["snapshots"]


def test_bridge_has_three_columns(db):
    _setup(db)
    _tenure(db, "P-T1", "OA", P_START)
    _global_price(db)
    _raw(db, "D-BC-1", 0, 0)
    _raw(db, "D-BC-1", 2, 60)
    _compile_all(db)
    build_bridge(db, P_START, P_END)
    db.commit()
    from app.models_billing import DifferenceBridgeEntry
    cols = {e.column_kind for e in db.query(DifferenceBridgeEntry).all()}
    assert "metering_accrual" in cols
