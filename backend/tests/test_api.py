"""API 端到端：登录、契约、接入、编译、发布、血缘链路可达。"""
import os

os.environ.setdefault(
    "IRR_DATABASE_URL",
    "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/irrigation_test")

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import Base, engine, SessionLocal
from app.main import app

client = TestClient(app)


def setup_module(module):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _reset():
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE TABLE adjustment_candidates, attribution_conflicts, "
            "difference_bridge_entries, receipt_matches, billing_snapshot_items, "
            "billing_snapshots, billing_fragments, external_billing_entries, "
            "price_relief_rules, price_tiers, price_versions, tenure_records, "
            "billing_operators, metric_versions, recompute_runs, "
            "reconciliation_tasks, adjudications, quality_events, supply_facts, "
            "meter_changes, plot_batch_allocations, device_mappings, "
            "mapping_versions, rotation_batches, plots, meter_points, channels, "
            "manual_revisions, raw_messages, data_contracts "
            "RESTART IDENTITY CASCADE"))


def test_full_api_flow():
    _reset()
    # 登录
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    bad = client.post("/api/auth/login", json={"username": "admin", "password": "x"})
    assert bad.status_code == 401

    # 契约登记
    r = client.post("/api/contracts", json={
        "code": "CT-CUM", "name": "累计契约", "source_kind": "device",
        "reading_type": "cumulative", "unit": "m3",
        "device_id_pattern": "D-.*", "collection_interval_sec": 3600,
        "payload_schema": {"reading": {"required": True}},
        "source_version": "v1",
        "active_from": "2026-11-10T00:00:00+00:00"})
    assert r.status_code == 201

    # 主数据
    client.post("/api/master/channels", json={"code": "MAIN", "name": "干渠", "level": 1})
    client.post("/api/master/channels",
                json={"code": "B1", "name": "支渠", "level": 2, "parent_code": "MAIN"})
    client.post("/api/master/meter-points",
                json={"code": "MP-HEAD", "name": "渠首", "channel_code": "MAIN",
                      "point_role": "head"})
    client.post("/api/master/meter-points",
                json={"code": "MP-B1", "name": "支渠点", "channel_code": "B1",
                      "point_role": "branch"})
    client.post("/api/master/plots",
                json={"code": "P1", "name": "地块", "area_mu": 100, "channel_code": "B1"})
    client.post("/api/master/batches",
                json={"code": "B1B", "name": "批次",
                      "planned_start": "2026-11-10T00:00:00+00:00",
                      "planned_end": "2026-11-10T06:00:00+00:00"})
    r = client.post("/api/master/mapping/publish", params={"note": "init"})
    vno = r.json()["mapping_version"]
    client.post("/api/master/device-mappings", json={
        "device_id": "D-HEAD", "meter_point_code": "MP-HEAD",
        "effective_from": "2026-11-10T00:00:00+00:00"})
    client.post("/api/master/device-mappings", json={
        "device_id": "D-B1", "meter_point_code": "MP-B1",
        "effective_from": "2026-11-10T00:00:00+00:00"})
    client.post("/api/master/allocations", json={
        "plot_code": "P1", "batch_code": "B1B", "planned_volume": 500,
        "effective_from": "2026-11-10T00:00:00+00:00"})

    # 报文（含倒退）
    for h, reading in [(0, 0), (1, 100), (2, 200), (3, 180)]:
        r = client.post("/api/ingestion/raw", json={
            "message_uid": f"M-{h}", "contract_code": "CT-CUM", "device_id": "D-B1",
            "payload": {"reading": reading, "unit": "m3"},
            "event_time": f"2026-11-10T0{h}:00:00+00:00",
            "received_at": f"2026-11-10T0{h}:05:00+00:00",
            "source_version": "v1"})
        assert r.status_code == 201
    # 幂等：重复 uid 被忽略
    dup = client.post("/api/ingestion/raw", json={
        "message_uid": "M-0", "contract_code": "CT-CUM", "device_id": "D-B1",
        "payload": {"reading": 0, "unit": "m3"},
        "event_time": "2026-11-10T00:00:00+00:00",
        "received_at": "2026-11-10T00:05:00+00:00", "source_version": "v1"})
    assert dup.json()["accepted"] is False

    # 编译 + 发布
    r = client.post("/api/compile")
    assert r.status_code == 201 and r.json()["supply_facts_written"] >= 1
    r = client.post("/api/metrics/publish", params={"trigger": "test"})
    assert r.status_code == 201 and r.json()["published"] >= 1

    # 指标、事实、质量事件可查
    metrics = client.get("/api/metrics").json()
    assert any(m["grain_type"] == "plot" for m in metrics)
    facts = client.get("/api/facts").json()
    assert any(f["quality_flag"] == "READING_REVERSAL" for f in facts)
    events = client.get("/api/quality/events").json()
    assert any(e["rule_code"] == "READING_REVERSAL" for e in events)

    # 血缘：任一指标都能回溯到节点
    metric_id = metrics[0]["id"]
    lineage = client.get(f"/api/metrics/{metric_id}/lineage").json()
    assert lineage["nodes"] and lineage["edges"]
    kinds = {n["kind"] for n in lineage["nodes"]}
    assert "metric" in kinds and "fact" in kinds

    # 版本链可回放
    versions = client.get(f"/api/metrics/{metric_id}/versions").json()
    assert len(versions) >= 1


def test_billing_reconciliation_api_flow():
    """计费双时间对账 API 端到端：三本账接入 -> 编译 -> 快照 -> 匹配 -> 审阅回查。"""
    _reset()
    from urllib.parse import quote
    ps, pe = "2026-11-01T00:00:00+00:00", "2026-12-01T00:00:00+00:00"
    qps, qpe = quote(ps, safe=""), quote(pe, safe="")
    # 基线主数据（渠道/计量点/地块/设备映射/契约/报文）
    client.post("/api/master/channels", json={"code": "BC1", "name": "计费渠", "level": 2,
                                              "parent_code": "MAIN"})
    client.post("/api/master/meter-points",
                json={"code": "BC-MP1", "name": "点", "channel_code": "BC1",
                      "point_role": "field"})
    client.post("/api/master/plots",
                json={"code": "BC-P1", "name": "地块", "area_mu": 60, "channel_code": "BC1"})
    client.post("/api/master/mapping/publish", params={"note": "init"})
    client.post("/api/master/device-mappings", json={
        "device_id": "D-BC-1", "meter_point_code": "BC-MP1",
        "effective_from": "2026-11-01T00:00:00+00:00"})
    client.post("/api/contracts", json={
        "code": "CT-BC", "name": "计费契约", "source_kind": "device",
        "reading_type": "cumulative", "unit": "m3", "device_id_pattern": "D-BC-1",
        "collection_interval_sec": 7200,
        "payload_schema": {"reading": {"required": True}},
        "source_version": "v1", "active_from": ps})
    for h, reading in [(0, 0), (2, 120), (4, 260)]:
        r = client.post("/api/ingestion/raw", json={
            "message_uid": f"BC-M-{h}", "contract_code": "CT-BC", "device_id": "D-BC-1",
            "payload": {"reading": reading, "unit": "m3"},
            "event_time": f"2026-11-10T0{h}:00:00+00:00",
            "received_at": f"2026-11-10T0{h}:05:00+00:00", "source_version": "v1"})
        assert r.status_code == 201
    client.post("/api/compile")

    # 计费三本账：经营户/归属/价目/外部账务事实
    assert client.post("/api/billing/operators",
                       json={"code": "OA", "name": "经营户甲"}).status_code == 201
    assert client.post("/api/billing/tenures", json={
        "plot_code": "BC-P1", "operator_code": "OA",
        "effective_from": ps, "recorded_at": ps}).status_code == 201
    assert client.post("/api/billing/price-versions", json={
        "code": "G", "name": "全局价", "approved_at": ps, "effective_from": ps,
        "tiers": [
            {"tier_no": 1, "volume_from": 0, "volume_to": 100, "unit_price": 0.5},
            {"tier_no": 2, "volume_from": 100, "volume_to": 200, "unit_price": 0.8},
            {"tier_no": 3, "volume_from": 200, "volume_to": None, "unit_price": 1.2}],
        "relief_rules": []}).status_code == 201
    ext = client.post("/api/billing/external-entries", json={
        "entry_uid": "BC-E1", "source_system": "ERP", "entry_kind": "bill_line",
        "receipt_no": "BC-R1", "bill_line_no": "BC-BL1", "plot_code": "BC-P1",
        "period_start": ps, "period_end": pe, "amount": 202.0, "direction": "debit",
        "event_time": "2026-11-10T06:00:00+00:00",
        "received_at": "2026-11-10T06:00:00+00:00"})
    assert ext.status_code == 201 and ext.json()["fingerprint"]

    # 分摊编译：跨三档 202 元
    r = client.post(f"/api/billing/compile?period_start={qps}&period_end={qpe}")
    assert r.status_code == 201 and r.json()["confirmed"] >= 3
    frags = client.get("/api/billing/fragments").json()
    assert round(sum(f["amount"] for f in frags), 2) == 202.0

    # 快照可确认并冻结
    snap = client.post("/api/billing/snapshots", json={
        "period_start": ps, "period_end": pe}).json()
    assert snap["status"] == "ready"
    assert client.post(f"/api/billing/snapshots/{snap['id']}/confirm",
                       json={"operator": "admin"}).status_code == 200
    items = client.get(f"/api/billing/snapshots/{snap['id']}/items").json()
    assert any(i["item_kind"] == "fragment" for i in items)

    # 回执匹配 + 审阅投影 + 回查链
    assert client.post(
        f"/api/billing/matching?period_start={qps}&period_end={qpe}").status_code == 201
    review = client.get(f"/api/billing/review?period_start={qps}&period_end={qpe}").json()
    row = next(x for x in review if x["plot_code"] == "BC-P1")
    assert row["accrual_amount"] == 202.0 and "OA" not in str(row)  # 最小披露
    fid = frags[0]["id"]
    chain = client.get(f"/api/billing/trace?fragment_id={fid}").json()
    assert chain["metering"] and chain["price_versions"] and chain["snapshots"]

    # 平台不存在任何资金动作接口
    paths = {r.path for r in app.routes}
    assert not any(k in p for p in paths for k in
                   ("/payment", "/refund", "/settle", "/fund"))
