"""血缘浏览器：从发布版本指标回溯到全部输入与规则。

路径：MetricVersion
  -> SupplyFact（input_fact_ids）
      -> RawMessage / ManualRevision（证据）
          -> DataContract（契约与来源版本）
      -> DeviceMapping / MappingVersion（映射版本）
      -> MeterChange（换表记录与凭证状态）
      -> Adjudication（人工裁定）
  -> QualityEvent（命中规则）
  -> ReconciliationTask（对账冻结）
  -> RecomputeRun（重算任务）
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Adjudication, DataContract, DeviceMapping, ManualRevision, MeterChange,
    MappingVersion, MetricVersion, QualityEvent, RawMessage,
    ReconciliationTask, RecomputeRun, SupplyFact,
)


def build_lineage(db: Session, metric_version_id: int) -> dict:
    mv = db.get(MetricVersion, metric_version_id)
    if not mv:
        return {"nodes": [], "edges": []}

    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add(kind, key, label, detail=None):
        nid = f"{kind}:{key}"
        if nid not in nodes:
            nodes[nid] = {"kind": kind, "id": nid, "label": label, "detail": detail or {}}
        return nid

    metric_id = add("metric", mv.id,
                    f"指标v{mv.version_no} {mv.grain_type}:{mv.grain_key} "
                    f"{mv.metric_date.date()} [{mv.status}]",
                    {"planned": mv.planned_volume, "confirmed": mv.confirmed_volume,
                     "pending": mv.pending_volume, "missing_rate": mv.missing_rate,
                     "loss": [mv.loss_lower, mv.loss_upper],
                     "diff_note": mv.diff_note, "watermark": mv.watermark.isoformat()})

    if mv.recompute_run_id:
        run = db.get(RecomputeRun, mv.recompute_run_id)
        rid = add("recompute", run.id, f"重算任务 {run.trigger} [{run.status}]",
                  {"run_uid": run.run_uid, "note": run.note})
        edges.append({"source": rid, "target": metric_id, "label": "发布"})

    for fid in mv.input_fact_ids:
        f = db.get(SupplyFact, fid)
        if not f:
            continue
        fid_n = add("fact", f.id,
                    f"事实 {f.fact_kind}/{f.status} {f.value}{f.unit}",
                    {"fact_uid": f.fact_uid, "method": f.method,
                     "interval": [f.interval_start.isoformat(), f.interval_end.isoformat()],
                     "readings": [f.reading_start, f.reading_end],
                     "confidence": f.confidence, "truncated": f.truncated,
                     "is_late": f.is_late, "quality_flag": f.quality_flag,
                     "revision_no": f.revision_no,
                     "mapping_version": f.mapping_version,
                     "source_version": f.source_version})
        edges.append({"source": fid_n, "target": metric_id, "label": "计入"})

        # 证据：原始报文（含被补传取代的历史行）
        for mid in f.evidence_message_ids:
            m = db.get(RawMessage, mid)
            if not m:
                continue
            mid_n = add("raw", m.id,
                        f"报文 {m.message_uid} rev{m.revision_no}"
                        f"{' [迟到补传]' if m.is_late else ''}",
                        {"device_id": m.device_id, "payload": m.payload,
                         "event_time": m.event_time.isoformat(),
                         "received_at": m.received_at.isoformat(),
                         "source_version": m.source_version,
                         "supersedes_uid": m.supersedes_uid,
                         "batch": m.ingestion_batch})
            edges.append({"source": mid_n, "target": fid_n, "label": "报文证据"})
            c = db.scalar(select(DataContract).where(
                DataContract.code == m.contract_code))
            if c:
                cid_n = add("contract", c.id,
                            f"契约 {c.code} v{c.source_version}",
                            {"reading_type": c.reading_type, "unit": c.unit,
                             "interval_sec": c.collection_interval_sec,
                             "device_pattern": c.device_id_pattern,
                             "schema": c.payload_schema})
                edges.append({"source": cid_n, "target": mid_n, "label": "约束"})

        for rid in f.evidence_manual_ids:
            r = db.get(ManualRevision, rid)
            if not r:
                continue
            rid_n = add("manual", r.id,
                        f"人工补录 {r.revision_uid} ({r.operator})",
                        {"value": r.value, "unit": r.unit, "basis": r.basis,
                         "event_time": r.event_time.isoformat(),
                         "received_at": r.received_at.isoformat()})
            edges.append({"source": rid_n, "target": fid_n, "label": "人工证据"})

        # 映射版本
        if f.mapping_version:
            mpv = db.scalar(select(MappingVersion).where(
                MappingVersion.version_no == f.mapping_version))
            if mpv:
                vid_n = add("mapping", mpv.id,
                            f"映射版本 v{mpv.version_no}",
                            {"note": mpv.note, "published_at": mpv.published_at.isoformat()})
                edges.append({"source": vid_n, "target": fid_n, "label": "生效映射"})
            dms = db.scalars(select(DeviceMapping).where(
                DeviceMapping.device_id == f.device_id,
                DeviceMapping.mapping_version == f.mapping_version)).all()
            for dm in dms:
                dm_n = add("device_mapping", dm.id,
                           f"设备映射 {dm.device_id}->{dm.meter_point_code}",
                           {"effective_from": dm.effective_from.isoformat(),
                            "effective_to": dm.effective_to.isoformat()
                            if dm.effective_to else None,
                            "change_reason": dm.change_reason})
                edges.append({"source": dm_n, "target": fid_n, "label": "归属解析"})

        # 换表记录
        if f.meter_change_id:
            mc = db.get(MeterChange, f.meter_change_id)
            mc_n = add("meter_change", mc.id,
                       f"换表 {mc.device_id} [{mc.status}]",
                       {"change_time": mc.change_time.isoformat(),
                        "voucher_present": mc.voucher_present,
                        "voucher_ref": mc.voucher_ref,
                        "old_last": mc.old_last_reading,
                        "new_first": mc.new_first_reading, "note": mc.note})
            edges.append({"source": mc_n, "target": fid_n, "label": "换表关系"})

        # 人工裁定
        if f.adjudication_id:
            adj = db.get(Adjudication, f.adjudication_id)
            adj_n = add("adjudication", adj.id,
                        f"裁定 {adj.decision} ({adj.operator})",
                        {"conflict_key": adj.conflict_key,
                         "selected_evidence": adj.selected_evidence,
                         "rationale": adj.rationale,
                         "decided_at": adj.decided_at.isoformat()
                         if adj.decided_at else None})
            edges.append({"source": adj_n, "target": fid_n, "label": "裁定结果"})

    # 质量事件与对账
    for q in db.scalars(select(QualityEvent)).all():
        if set(q.related_fact_ids or []) & set(mv.input_fact_ids):
            qid = add("quality", q.id, f"质量事件 {q.rule_code} [{q.severity}]",
                      {"status": q.status, "detail": q.detail})
            target_fact = next((n for n in nodes if n.startswith("fact:")
                                and int(n.split(":")[1]) in (q.related_fact_ids or [])),
                               metric_id)
            edges.append({"source": qid, "target": target_fact, "label": "规则命中"})
        if q.channel_code and mv.grain_type == "channel" and q.channel_code == mv.grain_key:
            qid = add("quality", q.id, f"质量事件 {q.rule_code} [{q.severity}]",
                      {"status": q.status, "detail": q.detail})
            edges.append({"source": qid, "target": metric_id, "label": "渠道规则"})

    for t in db.scalars(select(ReconciliationTask).where(
            ReconciliationTask.channel_code == mv.grain_key)).all():
        tid = add("reconciliation", t.id,
                  f"对账任务 {t.channel_code} [{t.status}]",
                  {"upstream": t.upstream_value, "downstream": t.downstream_explained,
                   "diff": t.diff, "diff_ratio": t.diff_ratio,
                   "frozen_metric_keys": t.frozen_metric_keys,
                   "resolution": t.resolution})
        edges.append({"source": tid, "target": metric_id, "label": "冻结/对账"})

    return {"nodes": list(nodes.values()), "edges": edges}
