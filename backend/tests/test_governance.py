"""治理硬规则测试：倒退、换表缺证截断、证据冲突、守恒冻结、版本差异。"""
from datetime import datetime, timedelta, timezone

from app.models import (
    Adjudication, QualityEvent, ReconciliationTask, SupplyFact,
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
from app.services.workflow import decide_adjudication

UTC = timezone.utc
T0 = datetime(2026, 11, 10, tzinfo=UTC)


def t(h, m=0):
    return T0 + timedelta(hours=h, minutes=m)


def _setup_topology(db):
    register_contract(db, ContractIn(
        code="CT-CUM", name="累计", source_kind="device", reading_type="cumulative",
        unit="m3", device_id_pattern=r"D-.*", collection_interval_sec=3600,
        payload_schema={"reading": {"required": True}, "unit": {"required": True}},
        source_version="v1", active_from=T0))
    register_contract(db, ContractIn(
        code="CT-MAN", name="人工", source_kind="manual", reading_type="cumulative",
        unit="m3", device_id_pattern=r"D-.*", collection_interval_sec=3600,
        payload_schema={"value": {"required": True}}, source_version="m1",
        active_from=T0))
    md.create_channel(db, ChannelIn(code="MAIN", name="干渠", level=1))
    md.create_channel(db, ChannelIn(code="B1", name="支渠", level=2, parent_code="MAIN"))
    md.create_meter_point(db, MeterPointIn(
        code="MP-HEAD", name="渠首", channel_code="MAIN", point_role="head"))
    md.create_meter_point(db, MeterPointIn(
        code="MP-B1", name="支渠点", channel_code="B1", point_role="branch"))
    md.create_plot(db, PlotIn(code="P1", name="地块1", area_mu=100, channel_code="B1"))
    md.create_batch(db, BatchIn(code="BATCH1", name="批次", planned_start=t(0), planned_end=t(4)))
    v = md.publish_mapping_version(db, "init")
    md.upsert_device_mapping(db, DeviceMappingIn(
        device_id="D-HEAD", meter_point_code="MP-HEAD", effective_from=t(0)), v)
    md.upsert_device_mapping(db, DeviceMappingIn(
        device_id="D-B1", meter_point_code="MP-B1", effective_from=t(0)), v)
    md.upsert_allocation(db, AllocationIn(
        plot_code="P1", batch_code="BATCH1", planned_volume=1000,
        effective_from=t(0)), v)
    db.commit()


def _raw(db, dev, h, reading, uid=None, unit="m3", contract="CT-CUM", **kw):
    return ingest_raw(db, RawMessageIn(
        message_uid=uid or f"{dev}-{h}", contract_code=contract, device_id=dev,
        payload={"reading": reading, "unit": unit}, event_time=t(h),
        received_at=kw.pop("received_at", t(h) + timedelta(minutes=5)),
        source_version="v1", **kw), batch="test")


def test_reading_reversal_never_negative(db):
    _setup_topology(db)
    _raw(db, "D-B1", 0, 100)
    _raw(db, "D-B1", 1, 200)
    _raw(db, "D-B1", 2, 150)  # 倒退
    compile_device(db, "D-B1", "cumulative")
    db.commit()
    facts = db.query(SupplyFact).filter(SupplyFact.device_id == "D-B1").all()
    values = [f.value for f in facts]
    assert all(v >= 0 for v in values)  # 永不出现负供水
    reversal = [f for f in facts if f.quality_flag == "READING_REVERSAL"]
    assert reversal and reversal[0].status == "pending_verification"
    assert db.query(QualityEvent).filter_by(rule_code="READING_REVERSAL").count() == 1


def test_meter_change_without_voucher_truncates(db):
    _setup_topology(db)
    md.register_meter_change(db, MeterChangeIn(
        device_id="D-B1", meter_point_code="MP-B1", change_time=t(1, 30),
        old_last_reading=200, new_first_reading=5, voucher_present=False))
    db.commit()
    _raw(db, "D-B1", 0, 100)
    _raw(db, "D-B1", 1, 200)
    _raw(db, "D-B1", 2, 5)    # 新表首读数（表面倒退）
    _raw(db, "D-B1", 3, 60)
    compile_device(db, "D-B1", "cumulative")
    db.commit()
    truncated = db.query(SupplyFact).filter_by(
        device_id="D-B1", truncated=True).all()
    assert len(truncated) == 2
    assert all(f.status == "pending_verification" for f in truncated)
    assert all(f.value >= 0 for f in truncated)
    assert db.query(QualityEvent).filter_by(
        rule_code="METER_CHANGE_TRUNCATION").count() == 1


def test_evidence_conflict_candidates_not_in_metrics_until_adjudicated(db):
    _setup_topology(db)
    _raw(db, "D-B1", 0, 100)
    _raw(db, "D-B1", 1, 200)
    _raw(db, "D-B1", 2, 300)  # 01-02 为无冲突正常区间
    ingest_manual(db, ManualRevisionIn(
        revision_uid="MAN-1", contract_code="CT-MAN", device_id="D-B1",
        event_time=t(0, 30), value=130.0, unit="m3",
        basis="便携式流量计比对", operator="张工", received_at=t(2)), batch="test")
    compile_device(db, "D-B1", "cumulative")
    run_all_rules(db)
    db.commit()
    candidates = db.query(SupplyFact).filter_by(status="candidate").all()
    assert len(candidates) == 2  # 设备与人工各一份候选
    adj = db.query(Adjudication).filter_by(decision="pending").one()

    publish_metrics(db, as_of=t(5))
    db.commit()
    # 未裁定：确认量不含候选
    from app.models import MetricVersion
    mv = db.query(MetricVersion).filter(
        MetricVersion.grain_type == "plot", MetricVersion.superseded_by.is_(None)).one()
    assert mv.confirmed_volume == 100.0  # 仅 01-02 无冲突区间；00-01 为候选未计入

    # 裁定采信设备后重编译，候选转确认
    decide_adjudication(db, adj.id, "accept_device", "设备证据可信度更高", "admin")
    db.commit()
    compile_device(db, "D-B1", "cumulative")
    db.commit()
    # 活动事实（排除被取代的历史行）中不再有候选，冲突区间转为已确认
    from app.services.ledger import superseded_fact_ids
    dead = superseded_fact_ids(db)
    active = [f for f in db.query(SupplyFact).filter_by(device_id="D-B1").all()
              if f.id not in dead]
    assert all(f.status != "candidate" for f in active)
    assert any(f.status == "confirmed" for f in active)


def test_channel_imbalance_freezes_loss_and_no_plan_fill(db):
    _setup_topology(db)
    # 渠首 400，支渠仅 100 -> 严重失衡
    for h, r in [(0, 0), (1, 100), (2, 200), (3, 300), (4, 400)]:
        _raw(db, "D-HEAD", h, r)
    _raw(db, "D-B1", 0, 0)
    _raw(db, "D-B1", 1, 40)
    _raw(db, "D-B1", 2, 70)
    _raw(db, "D-B1", 3, 90)
    _raw(db, "D-B1", 4, 100)
    compile_device(db, "D-HEAD", "cumulative")
    compile_device(db, "D-B1", "cumulative")
    compile_plan_facts(db)
    run_all_rules(db)
    db.commit()
    task = db.query(ReconciliationTask).filter_by(status="frozen").one()
    assert task.upstream_value == 400 and task.downstream_explained == 100

    publish_metrics(db, as_of=t(6))
    db.commit()
    from app.models import MetricVersion
    main_mv = db.query(MetricVersion).filter(
        MetricVersion.grain_type == "channel",
        MetricVersion.grain_key == "MAIN",
        MetricVersion.superseded_by.is_(None)).one()
    assert main_mv.status == "frozen"
    assert main_mv.loss_lower is None and main_mv.loss_upper is None  # 冻结且未用计划补平


def test_late_backfill_creates_version_with_diff_note(db):
    _setup_topology(db)
    for h, r in [(0, 0), (1, 100), (2, 200)]:
        _raw(db, "D-B1", h, r)
    compile_device(db, "D-B1", "cumulative")
    db.commit()
    publish_metrics(db, trigger="initial", as_of=t(3))
    db.commit()
    # 迟到补传修正末日读数
    _raw(db, "D-B1", 2, 240, uid="D-B1-2-REV", supersedes_uid="D-B1-2",
         received_at=t(20))
    run_all_rules(db)
    compile_device(db, "D-B1", "cumulative")
    db.commit()
    publish_metrics(db, trigger="late_backfill", as_of=t(21))
    db.commit()
    from app.models import MetricVersion
    versions = db.query(MetricVersion).filter(
        MetricVersion.grain_type == "plot").order_by(MetricVersion.version_no).all()
    assert len(versions) == 2
    assert versions[0].superseded_by == versions[1].id
    assert versions[1].confirmed_volume == 240
    assert "确认实灌量" in versions[1].diff_note
    # 旧报文仍保留，未被覆盖
    assert db.query(SupplyFact).count() >= 3
