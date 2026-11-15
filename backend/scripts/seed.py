"""样例种子：实际触发题面要求的全部治理场景。

场景叙事（2026-11-10，UTC）：
- 干渠 MAIN 渠首 D-HEAD-01（累计表，1h 区间）：00:00-06:00 每小时 +100m3；
  次日设备补传修正 04:00 读数（400 -> 420），旧报文保留、事实与指标产生新版本与差异说明；
  其中 03:00 报文存在时钟漂移（接收滞后 45 分钟）。
- 支渠 B1 D-B1-01（累计表）：02:30 换表但无换表凭证，新表首读数仅 5（相对旧表 320 看似倒退）；
  事实编译器截断区间、关联量列待核实，绝不产生负供水。
- 支渠 B2 D-B2-01（瞬时表）：梯形积分；05:00 缺测（2h 空窗 > 1.5 倍采集区间）；
  一条报文单位错填 L 触发单位不一致；01:00-02:00 区间同时出现设备补传与人工补录，
  两份证据并存生成 candidate，未裁定前不进正式指标。
- 守恒：渠首确认量远大于两支渠可解释量（B1 大量待核实 + B2 候选未裁定），
  触发 CHANNEL_IMBALANCE，冻结 MAIN 损耗指标并生成对账任务，禁止计划量补平。
- 计划量只作为 target 事实与比对口径。
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.db import Base, SessionLocal, engine
from app.models import (
    Adjudication, MetricVersion, QualityEvent, ReconciliationTask, SupplyFact,
)
from app.schemas import (
    AllocationIn, BatchIn, ChannelIn, ContractIn, DeviceMappingIn,
    ManualRevisionIn, MeterChangeIn, MeterPointIn, PlotIn, RawMessageIn,
)
from app.services import master_data as md
from app.services.fact_compiler import compile_device, compile_plan_facts
from app.services.ingestion import ingest_manual, ingest_raw, register_contract
from app.services.metrics import publish_metrics
from app.services.quality import run_all_rules

UTC = timezone.utc
D = datetime(2026, 11, 10, tzinfo=UTC)


def t(hour, minute=0, day=10):
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def reset(db):
    db.rollback()
    db.execute(text(
        "TRUNCATE TABLE metric_versions, recompute_runs, reconciliation_tasks, "
        "adjudications, quality_events, supply_facts, meter_changes, "
        "plot_batch_allocations, device_mappings, mapping_versions, "
        "rotation_batches, plots, meter_points, channels, "
        "manual_revisions, raw_messages, data_contracts RESTART IDENTITY CASCADE"))
    db.commit()


def seed():
    db = SessionLocal()
    reset(db)

    # ---------------- 1. 数据契约 ----------------
    register_contract(db, ContractIn(
        code="CT-CUM-01", name="累计表契约", source_kind="device",
        reading_type="cumulative", unit="m3", device_id_pattern=r"D-[A-Z0-9-]+",
        collection_interval_sec=3600,
        payload_schema={"reading": {"required": True}, "unit": {"required": True}},
        source_version="fw-1.4", active_from=D))
    register_contract(db, ContractIn(
        code="CT-FLOW-01", name="瞬时流量契约", source_kind="device",
        reading_type="instantaneous", unit="m3_h", device_id_pattern=r"D-[A-Z0-9-]+",
        collection_interval_sec=3600,
        payload_schema={"reading": {"required": True}, "unit": {"required": True}},
        source_version="fw-1.4", active_from=D))
    register_contract(db, ContractIn(
        code="CT-MAN-01", name="人工抄表契约", source_kind="manual",
        reading_type="cumulative", unit="m3", device_id_pattern=r"D-[A-Z0-9-]+",
        collection_interval_sec=3600,
        payload_schema={"value": {"required": True}, "unit": {"required": True}},
        source_version="manual-form-v2", active_from=D))
    register_contract(db, ContractIn(
        code="CT-PLAN-01", name="轮灌计划契约", source_kind="plan",
        reading_type="plan", unit="m3", device_id_pattern=r"PLAN-.*",
        collection_interval_sec=86400,
        payload_schema={"planned_volume": {"required": True}},
        source_version="plan-2026.11", active_from=D))

    # ---------------- 2. 水系与地块主数据 ----------------
    md.create_channel(db, ChannelIn(code="MAIN", name="东干渠", level=1))
    md.create_channel(db, ChannelIn(code="B1", name="一支渠", level=2, parent_code="MAIN"))
    md.create_channel(db, ChannelIn(code="B2", name="二支渠", level=2, parent_code="MAIN"))
    md.create_meter_point(db, MeterPointIn(
        code="MP-HEAD", name="渠首计量点", channel_code="MAIN", point_role="head"))
    md.create_meter_point(db, MeterPointIn(
        code="MP-B1", name="一支渠计量点", channel_code="B1", point_role="branch"))
    md.create_meter_point(db, MeterPointIn(
        code="MP-B2", name="二支渠计量点", channel_code="B2", point_role="branch"))
    md.create_plot(db, PlotIn(code="PLOT-B1", name="一号地块", area_mu=120, channel_code="B1"))
    md.create_plot(db, PlotIn(code="PLOT-B2", name="二号地块", area_mu=180, channel_code="B2"))
    md.create_batch(db, BatchIn(
        code="ROT-2026-01", name="11月上旬第一轮灌",
        planned_start=t(0), planned_end=t(6)))

    v1 = md.publish_mapping_version(db, "初始映射版本")
    md.upsert_device_mapping(db, DeviceMappingIn(
        device_id="D-HEAD-01", meter_point_code="MP-HEAD",
        effective_from=t(0)), v1)
    md.upsert_device_mapping(db, DeviceMappingIn(
        device_id="D-B1-01", meter_point_code="MP-B1",
        effective_from=t(0)), v1)
    md.upsert_device_mapping(db, DeviceMappingIn(
        device_id="D-B2-01", meter_point_code="MP-B2",
        effective_from=t(0)), v1)
    md.upsert_allocation(db, AllocationIn(
        plot_code="PLOT-B1", batch_code="ROT-2026-01", planned_volume=500,
        effective_from=t(0)), v1)
    md.upsert_allocation(db, AllocationIn(
        plot_code="PLOT-B2", batch_code="ROT-2026-01", planned_volume=600,
        effective_from=t(0)), v1)
    db.commit()

    # ---------------- 3. 换表缺证（02:30，无凭证） ----------------
    md.register_meter_change(db, MeterChangeIn(
        device_id="D-B1-01", meter_point_code="MP-B1", change_time=t(2, 30),
        old_last_reading=320, new_first_reading=5, voucher_present=False,
        note="巡检口头反映换表，未提供检定凭证"))
    db.commit()

    # ---------------- 4. 第一批设备报文（当日准点接收） ----------------
    # 渠首累计表：每小时 +100
    head_readings = {0: 0, 1: 100, 2: 200, 3: 300, 4: 400, 5: 500, 6: 600}
    for h, r in head_readings.items():
        recv = t(h) + timedelta(minutes=5)
        if h == 3:  # 时钟漂移：设备时钟偏快，事件时间比接收时间还靠前 35 分钟
            recv = t(h) - timedelta(minutes=35)
        ingest_raw(db, RawMessageIn(
            message_uid=f"MSG-HEAD-{h:02d}", contract_code="CT-CUM-01",
            device_id="D-HEAD-01", payload={"reading": r, "unit": "m3"},
            event_time=t(h), received_at=recv, source_version="fw-1.4"),
            batch="seed-day1")

    # 一支渠累计表：02:30 换表无凭证，新表从小读数重新累计（表面倒退）；
    # 换表后增量刻意偏小，与渠首量形成守恒缺口
    b1_series = [(0, 100), (1, 210), (2, 320), (3, 5), (4, 25), (5, 45), (6, 65)]
    for h, r in b1_series:
        ingest_raw(db, RawMessageIn(
            message_uid=f"MSG-B1-{h:02d}", contract_code="CT-CUM-01",
            device_id="D-B1-01", payload={"reading": r, "unit": "m3"},
            event_time=t(h), received_at=t(h) + timedelta(minutes=5),
            source_version="fw-1.4"), batch="seed-day1")

    # 二支渠瞬时表（m3/h）：05:00 缺测；02:00 报文单位错填 L
    b2_series = {0: 40, 1: 44, 2: 48, 3: 50, 4: 46, 6: 42}
    for h, r in b2_series.items():
        unit = "L" if h == 2 else "m3_h"
        ingest_raw(db, RawMessageIn(
            message_uid=f"MSG-B2-{h:02d}", contract_code="CT-FLOW-01",
            device_id="D-B2-01", payload={"reading": r, "unit": unit},
            event_time=t(h), received_at=t(h) + timedelta(minutes=5),
            source_version="fw-1.4"), batch="seed-day1")
    db.commit()

    # ---------------- 5. 首次编译 + 首次发布 ----------------
    compile_device(db, "D-HEAD-01", "cumulative")
    compile_device(db, "D-B1-01", "cumulative")
    compile_device(db, "D-B2-01", "instantaneous")
    compile_plan_facts(db)
    run_all_rules(db)
    db.commit()
    v1_metrics = publish_metrics(db, trigger="initial", note="首次发布", as_of=t(7))
    db.commit()
    print(f"[首次发布] 指标版本数={len(v1_metrics)}")

    # ---------------- 6. 次日：迟到补传 + 人工补录（证据冲突） ----------------
    # 6.1 渠首末日读数被设备补传修正 600 -> 620（旧报文保留不覆盖，累计总量随之变化）
    ingest_raw(db, RawMessageIn(
        message_uid="MSG-HEAD-06-REV2", contract_code="CT-CUM-01",
        device_id="D-HEAD-01", payload={"reading": 620, "unit": "m3"},
        event_time=t(6), received_at=t(10, day=12),
        source_version="fw-1.4", supersedes_uid="MSG-HEAD-06"),
        batch="seed-backfill")

    # 6.2 二支渠 02:00 同时收到设备补传（修正瞬时值）与人工补录（同一区间）
    ingest_raw(db, RawMessageIn(
        message_uid="MSG-B2-02-REV2", contract_code="CT-FLOW-01",
        device_id="D-B2-01", payload={"reading": 47, "unit": "m3_h"},
        event_time=t(2), received_at=t(11, day=12),
        source_version="fw-1.4", supersedes_uid="MSG-B2-02"),
        batch="seed-backfill")
    ingest_manual(db, ManualRevisionIn(
        revision_uid="MAN-B2-0130", contract_code="CT-MAN-01",
        device_id="D-B2-01", event_time=t(1, 30), value=55.0, unit="m3",
        basis="管水员现场比对便携式超声流量计读数", operator="张工",
        received_at=t(11, day=12)), batch="seed-manual")

    # 6.3 一条无依据人工补录（落在读数序列外，只触发 MANUAL_WITHOUT_BASIS，不生成事实）
    ingest_manual(db, ManualRevisionIn(
        revision_uid="MAN-B1-NOBASIS", contract_code="CT-MAN-01",
        device_id="D-B1-01", event_time=t(6, 30), value=30.0, unit="m3",
        basis="", operator="临时工",
        received_at=t(11, day=12)), batch="seed-manual")
    db.commit()

    # ---------------- 7. 重编译 + 重发布（新版本与差异说明） ----------------
    compile_device(db, "D-HEAD-01", "cumulative")
    compile_device(db, "D-B1-01", "cumulative")
    compile_device(db, "D-B2-01", "instantaneous")
    run_all_rules(db)
    db.commit()
    v2_metrics = publish_metrics(
        db, trigger="late_backfill",
        note="迟到补传/换表缺证/证据冲突后的重算发布", as_of=t(12, day=12))
    db.commit()
    print(f"[重算发布] 指标版本数={len(v2_metrics)}")

    # ---------------- 8. 场景自检摘要 ----------------
    rules = {}
    for ev in db.query(QualityEvent).all():
        rules.setdefault(ev.rule_code, []).append(ev)
    print("\n=== 质量事件触发情况 ===")
    for code, evs in sorted(rules.items()):
        print(f"  {code}: {len(evs)} 条")

    print("\n=== 事实账状态分布（活动事实） ===")
    from app.services.ledger import superseded_fact_ids
    dead = superseded_fact_ids(db)
    status_cnt = {}
    for f in db.query(SupplyFact).all():
        if f.id in dead:
            continue
        status_cnt[f"{f.fact_kind}/{f.status}"] = status_cnt.get(
            f"{f.fact_kind}/{f.status}", 0) + 1
    for k, v in sorted(status_cnt.items()):
        print(f"  {k}: {v}")

    print("\n=== 待裁定冲突 ===")
    for a in db.query(Adjudication).all():
        print(f"  #{a.id} {a.conflict_key} -> {a.decision}")

    print("\n=== 对账任务（冻结） ===")
    for r in db.query(ReconciliationTask).all():
        print(f"  #{r.id} {r.channel_code} 上游={r.upstream_value} "
              f"下游可解释={r.downstream_explained} 差异率={r.diff_ratio} [{r.status}]")

    print("\n=== 指标版本（最新） ===")
    latest = [m for m in db.query(MetricVersion).all() if m.superseded_by is None]
    for m in sorted(latest, key=lambda x: (x.metric_date, x.grain_type, x.grain_key)):
        print(f"  v{m.version_no} {m.grain_type}:{m.grain_key} "
              f"计划={m.planned_volume} 确认={m.confirmed_volume} "
              f"待核实={m.pending_volume} 缺测率={m.missing_rate} "
              f"完成率={m.plan_completion_rate} 损耗=[{m.loss_lower},{m.loss_upper}] "
              f"[{m.status}] 差异说明: {m.diff_note}")

    db.close()
    print("\n种子数据写入完成。")


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    seed()
