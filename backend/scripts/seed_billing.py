"""用水计费双时间对账样例种子（在基线治理种子之上追加，不截断基线表）。

五组验收样例 + 一组硬规则补充，叙事日期 2026-11：
1. 跨阶梯   PLOT-T1：账期累计 260m³ 跨三档阶梯，片段按边际阶梯拆分；
2. 跨租约   PLOT-T2：一条计量区间跨过转包生效点，时间比例拆给两任经营户，
   不按区间末尾当前归属整段计价；
3. 重复回执 PLOT-T3：同号同指纹=幂等重传；同号异指纹=并列冲突；
4. 冻结后补传 PLOT-T4：账期快照确认冻结后，迟到补传改写末日读数，
   只生成引用原快照的调整候选，原快照明细保持可复演；
5. 人工证据冲突 PLOT-T5：转包生效日与人工补录主张矛盾，双归属候选与金额范围并列；
6. 跨价目待证 PLOT-T6（硬规则补充）：累计读数跨两版价目且边界缺读数，
   先发 blocked_pending 待证片段阻断确认；补齐边界读数后重编译转确认。

本脚本只做数据治理与对账，不发起任何收款、退款、抵扣或资金状态迁移。
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.db import SessionLocal
from app.models_billing import (
    AttributionConflict, AdjustmentCandidate, BillingFragment, BillingSnapshot,
    ExternalBillingEntry, ReceiptMatch,
)
from app.schemas import (
    ChannelIn, ContractIn, DeviceMappingIn, ManualRevisionIn, MeterPointIn,
    PlotIn, RawMessageIn,
)
from app.schemas_billing import (
    ExternalBillingEntryIn, OperatorIn, PriceReliefIn, PriceTierIn,
    PriceVersionIn, TenureIn,
)
from app.services import master_data as md
from app.services.billing_bridge import build_bridge, scan_post_freeze_revisions
from app.services.billing_compiler import compile_billing
from app.services.billing_external import ingest_external_entry
from app.services.billing_matcher import run_matching
from app.services.billing_review import review_projection
from app.services.billing_snapshot import confirm_snapshot, create_snapshot
from app.services import billing_master as bm
from app.services.fact_compiler import compile_device
from app.services.ingestion import ingest_manual, ingest_raw, register_contract

UTC = timezone.utc
P_START = datetime(2026, 11, 1, tzinfo=UTC)
P_END = datetime(2026, 12, 1, tzinfo=UTC)
D = datetime(2026, 11, 10, tzinfo=UTC)


def t(h, m=0, day=10):
    return datetime(2026, 11, day, h, m, tzinfo=UTC)


BILL_PLOTS = ["PLOT-T1", "PLOT-T2", "PLOT-T3", "PLOT-T4", "PLOT-T5", "PLOT-T6"]


def reset_billing(db):
    db.rollback()
    db.execute(text(
        "TRUNCATE TABLE adjustment_candidates, attribution_conflicts, "
        "difference_bridge_entries, receipt_matches, billing_snapshot_items, "
        "billing_snapshots, billing_fragments, external_billing_entries, "
        "price_relief_rules, price_tiers, price_versions, tenure_records, "
        "billing_operators RESTART IDENTITY CASCADE"))
    # 同时清理本种子在基线表中创建的示范行，保证脚本可重复执行
    db.execute(text(
        "DELETE FROM quality_events WHERE device_id LIKE 'D-BILL-%'"))
    db.execute(text(
        "DELETE FROM supply_facts WHERE device_id LIKE 'D-BILL-%'"))
    db.execute(text(
        "DELETE FROM manual_revisions WHERE device_id LIKE 'D-BILL-%'"))
    db.execute(text(
        "DELETE FROM raw_messages WHERE device_id LIKE 'D-BILL-%'"))
    db.execute(text(
        "DELETE FROM device_mappings WHERE device_id LIKE 'D-BILL-%'"))
    db.execute(text(
        "DELETE FROM meter_points WHERE code LIKE 'MP-T%'"))
    db.execute(text(
        "DELETE FROM plots WHERE code IN "
        "('PLOT-T1','PLOT-T2','PLOT-T3','PLOT-T4','PLOT-T5','PLOT-T6')"))
    db.execute(text("DELETE FROM channels WHERE code LIKE 'BILL-%'"))
    db.execute(text("DELETE FROM data_contracts WHERE code = 'CT-BILL-CUM'"))
    db.commit()


def _topology(db):
    register_contract(db, ContractIn(
        code="CT-BILL-CUM", name="计费累计表契约", source_kind="device",
        reading_type="cumulative", unit="m3", device_id_pattern=r"D-BILL-\d+",
        collection_interval_sec=7200,
        payload_schema={"reading": {"required": True}, "unit": {"required": True}},
        source_version="fw-2.0", active_from=P_START))
    # 每个计费地块一条独立支渠（基线按渠道解析唯一地块），统一挂在 BILL-MAIN 干渠下
    md.create_channel(db, ChannelIn(code="BILL-MAIN", name="计费示范干渠", level=1))
    v = md.publish_mapping_version(db, "计费示范设备映射")
    for i, plot in enumerate(BILL_PLOTS, start=1):
        ch = f"BILL-C{i}"; mp = f"MP-T{i}"; dev = f"D-BILL-{i}"
        md.create_channel(db, ChannelIn(code=ch, name=f"计费支渠{i}", level=2,
                                        parent_code="BILL-MAIN"))
        md.create_meter_point(db, MeterPointIn(
            code=mp, name=f"计费计量点{i}", channel_code=ch, point_role="field"))
        md.create_plot(db, PlotIn(code=plot, name=f"计费示范地块{i}",
                                  area_mu=50 + i * 10, channel_code=ch))
        md.upsert_device_mapping(db, DeviceMappingIn(
            device_id=dev, meter_point_code=mp, effective_from=P_START), v)
    db.commit()


def _operators_and_tenures(db):
    for code, name in [("OP-A", "经营户甲"), ("OP-B", "经营户乙"), ("OP-C", "经营户丙"),
                       ("OP-D", "经营户丁"), ("OP-E", "经营户戊"),
                       ("OP-F", "经营户己"), ("OP-G", "经营户庚")]:
        bm.create_operator(db, OperatorIn(code=code, name=name))

    def tenure(plot, op, frm, reason="initial", to=None, lease=None, evidence=None,
               recorded=None):
        return bm.register_tenure(db, TenureIn(
            plot_code=plot, operator_code=op, lease_code=lease,
            effective_from=frm, effective_to=to, change_reason=reason,
            evidence_ref=evidence, recorded_at=recorded or frm))

    tenure("PLOT-T1", "OP-A", P_START, lease="LS-T1")
    tenure("PLOT-T3", "OP-D", P_START, lease="LS-T3")
    tenure("PLOT-T4", "OP-E", P_START, lease="LS-T4")
    tenure("PLOT-T6", "OP-A", P_START, lease="LS-T6")
    # T2：11-10 02:00 转包 OP-B -> OP-C
    tenure("PLOT-T2", "OP-B", P_START, to=t(2), lease="LS-T2-OLD")
    tenure("PLOT-T2", "OP-C", t(2), reason="transfer", lease="LS-T2-NEW",
           evidence="TRF-T2-20261110", recorded=t(2, 30))
    # T5：11-10 02:00 转包 OP-F -> OP-G
    tenure("PLOT-T5", "OP-F", P_START, to=t(2), lease="LS-T5-OLD")
    tenure("PLOT-T5", "OP-G", t(2), reason="transfer", lease="LS-T5-NEW",
           evidence="TRF-T5-20261110", recorded=t(2, 30))
    db.commit()


def _prices(db):
    tiers = [PriceTierIn(tier_no=1, volume_from=0, volume_to=100, unit_price=0.5),
             PriceTierIn(tier_no=2, volume_from=100, volume_to=200, unit_price=0.8),
             PriceTierIn(tier_no=3, volume_from=200, volume_to=None, unit_price=1.2)]
    bm.publish_price_version(db, PriceVersionIn(
        code="PRICE-GLOBAL", name="全局阶梯水价 v1", plot_code=None,
        approved_at=datetime(2026, 10, 25, tzinfo=UTC), effective_from=P_START,
        season_note="常规季节价", note="100m³ 内 0.5 元，100-200 段 0.8 元，200 以上 1.2 元",
        tiers=tiers,
        relief_rules=[PriceReliefIn(
            relief_code="REL-DEMO", name="经营户 OP-D 比例减免（作用于 PLOT-T3）",
            relief_kind="rate", rate=0.1,
            condition_json={"plot_codes": ["PLOT-T3"],
                            "operator_codes": ["OP-D"]})]))
    # T6 地块专用两版价目：02:00 切换
    bm.publish_price_version(db, PriceVersionIn(
        code="PRICE-T6-OLD", name="T6 旧价目", plot_code="PLOT-T6",
        approved_at=datetime(2026, 10, 20, tzinfo=UTC), effective_from=P_START,
        effective_to=t(2), note="0.4 元/m³ 单一档",
        tiers=[PriceTierIn(tier_no=1, volume_from=0, volume_to=None, unit_price=0.4)]))
    bm.publish_price_version(db, PriceVersionIn(
        code="PRICE-T6-NEW", name="T6 新价目", plot_code="PLOT-T6",
        approved_at=datetime(2026, 11, 1, tzinfo=UTC), effective_from=t(2),
        note="0.6 元/m³ 单一档",
        tiers=[PriceTierIn(tier_no=1, volume_from=0, volume_to=None, unit_price=0.6)]))
    db.commit()


def _raw(db, dev, h, reading, uid_suffix=None, received=None, supersedes=None):
    return ingest_raw(db, RawMessageIn(
        message_uid=uid_suffix or f"MSG-{dev}-{h:02d}", contract_code="CT-BILL-CUM",
        device_id=dev, payload={"reading": reading, "unit": "m3"},
        event_time=t(h), received_at=received or (t(h) + timedelta(minutes=10)),
        source_version="fw-2.0", supersedes_uid=supersedes), batch="seed-billing")


def _readings(db):
    # T1：120 + 140 = 260，跨三档
    for h, r in [(0, 0), (2, 120), (4, 260)]:
        _raw(db, "D-BILL-1", h, r)
    # T2：单区间 200 跨 02:00 转包点，边界无读数
    for h, r in [(0, 0), (4, 200)]:
        _raw(db, "D-BILL-2", h, r)
    # T3：50
    for h, r in [(0, 0), (2, 50)]:
        _raw(db, "D-BILL-3", h, r)
    # T4：100（冻结后补传为 130）
    for h, r in [(0, 0), (3, 100)]:
        _raw(db, "D-BILL-4", h, r)
    # T5：240 跨 02:00 转包点
    for h, r in [(0, 0), (4, 240)]:
        _raw(db, "D-BILL-5", h, r)
    # T6：100 跨 02:00 价目切换点，先不给边界读数
    for h, r in [(0, 0), (4, 100)]:
        _raw(db, "D-BILL-6", h, r)
    # T5 人工补录：主张整段归 OP-F（与 02:00 转包沿革矛盾）
    ingest_manual(db, ManualRevisionIn(
        revision_uid="MAN-T5-CLAIM", contract_code="CT-MAN-01",
        device_id="D-BILL-5", event_time=t(1), value=240.0, unit="m3",
        basis="管水员持转包前协议主张整段用水仍归 OP-F，待凭证裁定",
        operator="李工", received_at=t(1, 30),
        claimed_operator_code="OP-F"), batch="seed-billing")
    db.commit()


def _external(db):
    def entry(uid_, kind, receipt, line_no, plot, amount, direction,
              received, status="issued", ref=None):
        return ingest_external_entry(db, ExternalBillingEntryIn(
            entry_uid=uid_, source_system="EXT-FIN-ERP", entry_kind=kind,
            receipt_no=receipt, bill_line_no=line_no, plot_code=plot,
            period_start=P_START, period_end=P_END, amount=amount,
            direction=direction, status=status, external_ref=ref,
            payload={"demo": True}, event_time=received, received_at=received))

    # T1 应计 202 元；T2 应计 130 元；T3 应计 25 元（含 10% 减免后 22.5）
    entry("EXT-T1-1", "bill_line", "RCPT-T1-001", "BL-T1-1", "PLOT-T1",
          202.0, "debit", t(6))
    entry("EXT-T2-1", "bill_line", "RCPT-T2-001", "BL-T2-1", "PLOT-T2",
          130.0, "debit", t(6))
    # T3：幂等重传一对（同号同指纹同方向）
    entry("EXT-T3-1", "bill_line", "RCPT-T3-DUP", "BL-T3-1", "PLOT-T3",
          22.5, "debit", t(6))
    entry("EXT-T3-2", "bill_line", "RCPT-T3-DUP", "BL-T3-1", "PLOT-T3",
          22.5, "debit", t(6, 30))
    # T3：同号但金额不同 -> 指纹不同 -> 冲突并列
    entry("EXT-T3-3", "bill_line", "RCPT-T3-CFL", "BL-T3-2", "PLOT-T3",
          22.5, "debit", t(7))
    entry("EXT-T3-4", "bill_line", "RCPT-T3-CFL", "BL-T3-2", "PLOT-T3",
          30.0, "debit", t(7, 30))
    # T3 冲补行（不同回执号）
    entry("EXT-T3-CR", "adjustment_line", "RCPT-T3-CR", "BL-T3-CR", "PLOT-T3",
          2.5, "credit", t(8))
    # T4 冻结时应计 50 元
    entry("EXT-T4-1", "bill_line", "RCPT-T4-001", "BL-T4-1", "PLOT-T4",
          50.0, "debit", t(6))
    db.commit()


def seed_billing():
    db = SessionLocal()
    reset_billing(db)
    _topology(db)
    _operators_and_tenures(db)
    _prices(db)
    _readings(db)

    # ---- 计量事实编译 ----
    for i in range(1, 7):
        compile_device(db, f"D-BILL-{i}", "cumulative")
    db.commit()

    # ---- 第一次计费分摊编译 ----
    r1 = compile_billing(db, P_START, P_END, BILL_PLOTS)
    db.commit()
    print(f"[首次分摊编译] {r1}")

    _external(db)
    run_matching(db, P_START, P_END)
    build_bridge(db, P_START, P_END)
    db.commit()

    # ---- 场景4：T4 账期快照（冻结时刻 11-11 09:00）并确认 ----
    frozen_at = datetime(2026, 11, 11, 9, tzinfo=UTC)
    snap_t4 = create_snapshot(db, P_START, P_END, trigger="period_close",
                              note="PLOT-T4 账期关闭冻结", plot_codes=["PLOT-T4"],
                              frozen_at_override=frozen_at)
    db.commit()
    print(f"[T4 快照] id={snap_t4.id} status={snap_t4.status} "
          f"summary={snap_t4.summary}")
    confirm_snapshot(db, snap_t4.id, "admin", "账期核对无误，确认冻结")
    db.commit()
    t4_items_before = len([i for i in snap_t4.items])
    t4_frozen_amount = snap_t4.summary["confirmed_amount"]

    # ---- 全账期阻断快照（T5 归属冲突 + T6 跨价目待证），确认应被拒绝 ----
    snap_blocked = create_snapshot(db, P_START, P_END, trigger="period_close",
                                   note="全账期快照（预期阻断）")
    db.commit()
    print(f"[全账期快照] id={snap_blocked.id} status={snap_blocked.status} "
          f"blockers={len(snap_blocked.blockers)}")
    try:
        confirm_snapshot(db, snap_blocked.id, "admin", "尝试确认")
        db.commit()
        raise RuntimeError("阻断快照不应被确认")
    except ValueError as exc:
        db.rollback()
        print(f"[阻断确认被拒绝] {exc}")

    # ---- 场景4：冻结后迟到补传（11-12 到达，末日读数 100 -> 130）----
    _raw(db, "D-BILL-4", 3, 130, uid_suffix="MSG-D-BILL-4-03-REV2",
         received=datetime(2026, 11, 12, 10, tzinfo=UTC),
         supersedes="MSG-D-BILL-4-03")
    compile_device(db, "D-BILL-4", "cumulative")
    db.commit()
    r2 = compile_billing(db, P_START, P_END, BILL_PLOTS)
    db.commit()
    print(f"[补传后重编译] {r2}")
    cands = scan_post_freeze_revisions(db, P_START, P_END)
    db.commit()
    print(f"[冻结后调整候选] {len(cands)} 条：" +
          "; ".join(f"{c.plot_code}/{c.trigger_kind}/影响金额{c.impact_amount}"
                    for c in cands))

    # 原快照复演校验：明细行数与冻结金额不变
    db.refresh(snap_t4)
    t4_items_after = len(snap_t4.items)
    assert t4_items_before == t4_items_after, "快照明细被修改，违反冻结纪律"
    assert snap_t4.summary["confirmed_amount"] == t4_frozen_amount == 50.0
    print(f"[快照复演] 明细 {t4_items_before}->{t4_items_after} 行，"
          f"冻结金额仍为 {t4_frozen_amount}")

    # ---- 场景6：补齐 T6 边界读数（02:00=60），重编译转确认 ----
    _raw(db, "D-BILL-6", 2, 60, uid_suffix="MSG-D-BILL-6-02-BACKFILL",
         received=datetime(2026, 11, 12, 11, tzinfo=UTC))
    compile_device(db, "D-BILL-6", "cumulative")
    db.commit()
    r3 = compile_billing(db, P_START, P_END, BILL_PLOTS)
    db.commit()
    print(f"[T6 补边界读数后重编译] {r3}")

    run_matching(db, P_START, P_END)
    build_bridge(db, P_START, P_END)
    db.commit()

    # ==================================================================
    # 五组样例 + 跨价目待证的硬性自检断言
    # ==================================================================
    from app.services.billing_ledger import active_fragments, superseded_fragment_ids

    dead = superseded_fragment_ids(db)
    active = [f for f in db.query(BillingFragment).all() if f.id not in dead]

    def frags(plot, status=None):
        return [f for f in active if f.plot_code == plot
                and (status is None or f.status == status)]

    # ---- 场景1：跨阶梯（260m³ = 100×0.5 + 100×0.8 + 60×1.2 = 202 元）----
    t1 = frags("PLOT-T1", "confirmed")
    t1_amount = round(sum(f.amount or 0 for f in t1), 2)
    t1_vol = round(sum(f.volume or 0 for f in t1), 3)
    assert t1_vol == 260.0, f"跨阶梯水量应为260，实际{t1_vol}"
    assert t1_amount == 202.0, f"跨阶梯金额应为202，实际{t1_amount}"
    assert {f.tier_no for f in t1} == {1, 2, 3}, "应出现三档阶梯片段"
    print(f"[断言1 跨阶梯] PLOT-T1 260m³ 分三档，应计 {t1_amount} 元 ✓")

    # ---- 场景2：跨租约（200m³ 按转包点时间比例 100/100 拆给两任经营户）----
    t2 = frags("PLOT-T2", "confirmed")
    ops = {f.operator_code: round(sum(x.volume or 0 for x in t2
                                      if x.operator_code == f.operator_code), 3)
           for f in t2}
    assert ops == {"OP-B": 100.0, "OP-C": 100.0}, f"跨租约拆分异常: {ops}"
    assert all(f.split_basis == "time_proportional" or f.fragment_kind == "tier_split"
               for f in t2)
    t2_amount = round(sum(f.amount or 0 for f in t2), 2)
    assert t2_amount == 130.0, f"跨租约金额应为130，实际{t2_amount}"
    print(f"[断言2 跨租约] OP-B/OP-C 各100m³，合计应计 {t2_amount} 元 ✓")

    # ---- 场景3：重复回执（幂等 vs 冲突并列）----
    dup = db.query(ReceiptMatch).filter_by(
        receipt_no="RCPT-T3-DUP").one()
    cfl = db.query(ReceiptMatch).filter_by(
        receipt_no="RCPT-T3-CFL").one()
    assert dup.match_status == "duplicate_idempotent"
    assert len(dup.entry_ids) == 2, "幂等重传应保留两条原始行"
    assert cfl.match_status == "conflict"
    assert len(cfl.entry_ids) == 2, "同号异指纹应并列两条冲突记录"
    cfl_fps = {db.get(ExternalBillingEntry, i).bill_line_fingerprint
               for i in cfl.entry_ids}
    assert len(cfl_fps) == 2, "两条冲突记录指纹必须不同"
    cfl_dirs = {db.get(ExternalBillingEntry, i).direction for i in cfl.entry_ids}
    assert len(cfl_fps) >= 1
    print("[断言3 重复回执] 同号同指纹=幂等重传；同号异指纹=并列冲突（未按幂等处理）✓")

    # ---- 场景4：冻结后补传（原快照不变，只出调整候选）----
    db.refresh(snap_t4)
    assert snap_t4.status == "confirmed"
    assert snap_t4.summary["confirmed_amount"] == 50.0, "原快照金额必须保持50"
    t4_adj = [c for c in db.query(AdjustmentCandidate).all()
              if c.plot_code == "PLOT-T4" and c.trigger_kind == "late_backfill"]
    assert len(t4_adj) == 1 and t4_adj[0].original_snapshot_id == snap_t4.id
    assert t4_adj[0].impact_volume == 30.0, f"补传影响水量应为30，实际{t4_adj[0].impact_volume}"
    assert t4_adj[0].impact_amount == 24.0
    # 快照冻结副本里仍是 100m³/50元 的旧片段
    frozen_frag = [i for i in snap_t4.items if i.item_kind == "fragment"]
    assert round(sum(i.content["volume"] for i in frozen_frag), 3) == 100.0
    print("[断言4 冻结后补传] 原快照100m³/50元不变，调整候选+30m³/+24元仅引用原快照 ✓")

    # ---- 场景5：人工证据冲突（双归属候选 + 金额范围，未裁定不进确认）----
    conflict = db.query(AttributionConflict).filter_by(plot_code="PLOT-T5").one()
    assert conflict.status == "pending"
    t5_cand = frags("PLOT-T5", "candidate")
    t5_conf = frags("PLOT-T5", "confirmed")
    assert len(t5_cand) >= 2 and not t5_conf, "未裁定前只能有候选、不能有确认片段"
    cand_kinds = {f.detail.get("candidate") for f in t5_cand}
    assert cand_kinds == {"tenure_record", "manual_claim"}
    assert conflict.amount_lower <= conflict.amount_upper
    print(f"[断言5 人工证据冲突] 双候选并列，金额范围 "
          f"[{conflict.amount_lower},{conflict.amount_upper}]，等待凭证裁定 ✓")

    # ---- 硬规则补充：跨价目待证（缺读数阻断，补读数后转确认）----
    t6_history = db.query(BillingFragment).filter_by(
        plot_code="PLOT-T6").order_by(BillingFragment.id).all()
    blocked_history = [f for f in t6_history
                       if f.blocking_reason == "CROSS_PRICE_NO_INTERIM_READING"]
    assert blocked_history, "缺中间读数时必须出现跨价目待证片段"
    t6_now = frags("PLOT-T6", "confirmed")
    assert t6_now and round(sum(f.volume or 0 for f in t6_now), 3) == 100.0
    assert {f.price_version_no for f in t6_now} == {2, 3}, "补证后按两版价目分别确认"
    print("[断言6 跨价目待证] 缺读数阻断确认；补齐边界读数后按旧/新价目转确认 ✓")

    # ---- 任一差额可回查五类证据 ----
    from app.services.billing_review import trace_difference
    # 从 T4 确认快照的冻结副本取片段，验证可回查计量/归属/价目/快照四类依据
    frozen_frag_id = next(i.ref_id for i in snap_t4.items
                          if i.item_kind == "fragment")
    chain = trace_difference(db, fragment_id=frozen_frag_id)
    for key in ("metering", "tenure_versions", "price_versions", "snapshots"):
        assert chain.get(key), f"回查链缺少 {key}"
    # 经回执匹配入口回查，外部回执证据可达
    t1_match = db.query(ReceiptMatch).filter_by(
        plot_code="PLOT-T1", match_status="matched").first()
    chain2 = trace_difference(db, match_id=t1_match.id)
    assert chain2["external_receipts"] and chain2["metering"], \
        "回执回查链必须同时含外部回执与计量片段"
    print("[断言7 差额回查] 可回查计量区间/归属版本/价目版本/快照/外部回执五类依据 ✓")

    print("\n=== 分摊片段状态分布（活动） ===")
    dist: dict[str, int] = {}
    for f in active:
        dist[f"{f.plot_code}/{f.status}/{f.fragment_kind}"] = \
            dist.get(f"{f.plot_code}/{f.status}/{f.fragment_kind}", 0) + 1
    for k in sorted(dist):
        print(f"  {k}: {dist[k]}")

    print("\n=== 回执匹配结论 ===")
    for m in db.query(ReceiptMatch).order_by(ReceiptMatch.id).all():
        print(f"  {m.plot_code or '-'} 回执{m.receipt_no or '-'} -> "
              f"{m.match_status} 金额{m.amount}")

    print("\n=== 审阅投影（最小披露，不含经营户身份） ===")
    for row in review_projection(db, P_START, P_END):
        print(f"  {row['period_key']} {row['plot_code']} 应计{row['accrual_amount']} "
              f"外部{row['external_net_amount']} 差额{row['diff_amount']} "
              f"来源{row['diff_sources']}")
    db.close()
    print("\n计费对账种子写入完成，全部断言通过。")


if __name__ == "__main__":
    from app.db import Base, engine
    from app import models, models_billing  # noqa: F401  注册计量与计费两套表
    Base.metadata.create_all(bind=engine)
    seed_billing()
