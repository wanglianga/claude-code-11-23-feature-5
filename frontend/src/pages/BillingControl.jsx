import React, { useCallback, useState } from "react";
import { get, post } from "../api";
import {
  Card, Badge, Drawer, useFetch, useToast, fmt, dayOf, Loading, Empty,
} from "../components";

const PERIOD_START = "2026-11-01T00:00:00+00:00";
const PERIOD_END = "2026-12-01T00:00:00+00:00";
const Q = `period_start=${encodeURIComponent(PERIOD_START)}&period_end=${encodeURIComponent(PERIOD_END)}`;

const MATCH_TONE = {
  matched: "confirmed", duplicate_idempotent: "target", conflict: "critical",
  missing_external: "pending_verification", missing_internal: "candidate",
};
const MATCH_LABEL = {
  matched: "匹配", duplicate_idempotent: "幂等重传", conflict: "冲突并列",
  missing_external: "缺外部账务", missing_internal: "缺应计依据",
};

export default function BillingControl() {
  const toast = useToast();
  const [snapDetail, setSnapDetail] = useState(null);
  const [busy, setBusy] = useState("");
  const loadSnaps = useCallback(() => get("/billing/snapshots"), []);
  const loadMatches = useCallback(() => get("/billing/receipt-matches"), []);
  const loadAdj = useCallback(() => get("/billing/adjustments"), []);
  const loadConflicts = useCallback(() => get("/billing/attribution-conflicts"), []);
  const snaps = useFetch(loadSnaps);
  const matches = useFetch(loadMatches);
  const adjusts = useFetch(loadAdj);
  const conflicts = useFetch(loadConflicts);

  const run = async (key, fn, okMsg) => {
    setBusy(key);
    try { await fn(); if (okMsg) toast(okMsg, "ok"); }
    catch (e) { toast(e.message, "err"); }
    finally { setBusy(""); }
  };

  const createSnapshot = () => run("snap", async () => {
    const s = await post("/billing/snapshots", {
      period_start: PERIOD_START, period_end: PERIOD_END, trigger: "manual", note: "前端手工冻结" });
    snaps.reload();
    return s;
  }, "账期快照已生成（含阻断项时状态为 blocked，禁止确认）");

  const confirmSnap = (id) => run(`confirm-${id}`, async () => {
    await post(`/billing/snapshots/${id}/confirm`, { operator: "admin", note: "审阅通过后确认冻结" });
    snaps.reload();
  }, "快照已确认冻结；后续修订只生成调整候选，快照保持可复演");

  const doMatch = () => run("match", async () => {
    await post(`/billing/matching?${Q}`); matches.reload();
  }, "回执匹配完成");

  const doBridge = () => run("bridge", async () => {
    const r = await post(`/billing/bridge?${Q}`);
    toast(`差异桥已重建：${r.bridge_entries} 行，覆盖地块 ${Object.keys(r.plot_summary).length} 个`, "ok");
  }, "");

  const scanAdjust = () => run("scan", async () => {
    const r = await post(`/billing/adjustments/scan?${Q}`);
    adjusts.reload();
    return r;
  }, "冻结后修订扫描完成，调整候选已登记（原快照未改动）");

  const decideAdjust = (id, decision) => run(`adj-${id}`, async () => {
    await post(`/billing/adjustments/${id}/decide`,
      { decision, rationale: "前端审阅留痕", operator: "admin" });
    adjusts.reload();
  }, "调整候选已留痕（不触发任何资金动作）");

  const decideConflict = (id, decision) => run(`cf-${id}`, async () => {
    await post(`/billing/attribution-conflicts/${id}/decide`,
      { decision, rationale: "凭证裁定留痕", operator: "admin" });
    conflicts.reload();
    toast("裁定已登记，请回到「分摊事实片段」重新编译使候选转确认", "ok");
  }, "");

  const openSnapshot = async (id) => {
    try { setSnapDetail(await get(`/billing/snapshots/${id}/items`)); }
    catch (e) { toast(e.message, "err"); }
  };

  if (!snaps.data || !matches.data || !adjusts.data || !conflicts.data) return <Loading />;

  return (
    <div>
      <div className="page-head">
        <h1>账期快照 · 回执匹配 · 差异桥与冻结后调整</h1>
        <p>快照冻结计量/归属/价目版本；同号异指纹并列冲突；冻结后人工更正只产生引用原快照的调整候选。全程无收款、退款、抵扣或资金状态迁移</p>
      </div>

      <div className="toolbar">
        <button onClick={createSnapshot} disabled={!!busy}>① 生成账期快照</button>
        <button className="ghost" onClick={doMatch} disabled={!!busy}>② 回执匹配</button>
        <button className="ghost" onClick={doBridge} disabled={!!busy}>③ 重建差异桥</button>
        <button className="ghost" onClick={scanAdjust} disabled={!!busy}>④ 扫描冻结后修订</button>
      </div>

      <h2 className="section-title">账期快照（冻结后不可变）</h2>
      <Card>
        {snaps.data.length === 0 ? <Empty text="尚未生成快照" /> : (
          <div className="table-wrap">
            <table className="data">
              <thead><tr>
                <th>快照</th><th>账期</th><th>状态</th><th className="num">确认水量</th>
                <th className="num">确认金额</th><th className="num">外部借/贷</th>
                <th>阻断项</th><th>版本指针</th><th>操作</th>
              </tr></thead>
              <tbody>
                {snaps.data.map((s) => (
                  <tr key={s.id}>
                    <td className="mono">#{s.id} {s.snapshot_uid.slice(0, 14)}…</td>
                    <td className="mono">{s.period_key}</td>
                    <td><Badge tone={s.status === "confirmed" ? "confirmed"
                      : s.status === "ready" ? "stable" : "critical"}>
                      { { ready: "可确认", blocked: "阻断中", confirmed: "已冻结" }[s.status] || s.status}
                    </Badge></td>
                    <td className="num">{fmt(s.summary.confirmed_volume)}</td>
                    <td className="num">{fmt(s.summary.confirmed_amount, 2)}</td>
                    <td className="num mono" style={{ fontSize: 11.5 }}>
                      {fmt(s.summary.external_debit, 2)} / {fmt(s.summary.external_credit, 2)}
                    </td>
                    <td style={{ fontSize: 11.5 }}>
                      {s.blockers.length === 0 ? <span className="muted">无</span> :
                        s.blockers.map((b, i) => (
                          <div key={i}><Badge tone="critical">{b.kind}</Badge>
                            <span className="mono muted"> {b.reason || b.conflict_group_uid}</span></div>
                        ))}
                    </td>
                    <td className="mono muted" style={{ fontSize: 10.5 }}>
                      归属{s.tenure_version_nos.join(",") || "—"}<br />
                      价目{s.price_version_nos.join(",") || "—"}
                    </td>
                    <td className="flex wrap gap-6">
                      <button className="ghost sm" onClick={() => openSnapshot(s.id)}>冻结明细</button>
                      {s.status === "ready" &&
                        <button className="sm" onClick={() => confirmSnap(s.id)}>确认冻结</button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <h2 className="section-title">回执匹配结果（重复 / 冲突 / 缺失）</h2>
      <Card>
        {matches.data.length === 0 ? <Empty text="尚未运行匹配" /> : (
          <div className="table-wrap">
            <table className="data">
              <thead><tr>
                <th>结论</th><th>地块</th><th>回执号</th><th>方向</th>
                <th className="num">金额</th><th>说明</th>
              </tr></thead>
              <tbody>
                {matches.data.map((m) => (
                  <tr key={m.id}>
                    <td><Badge tone={MATCH_TONE[m.match_status]}>{MATCH_LABEL[m.match_status] || m.match_status}</Badge></td>
                    <td>{m.plot_code || "—"}</td>
                    <td className="mono">{m.receipt_no || "—"}</td>
                    <td className="mono">{m.direction || "—"}</td>
                    <td className="num">{fmt(m.amount, 2)}</td>
                    <td className="soft" style={{ fontSize: 12 }}>{m.detail.reason || JSON.stringify(m.detail.entries || {})}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <h2 className="section-title">归属冲突（双候选与金额范围，等待凭证裁定）</h2>
      <Card>
        {conflicts.data.length === 0 ? <Empty text="无归属冲突" /> : (
          <div className="table-wrap">
            <table className="data">
              <thead><tr>
                <th>地块</th><th>冲突区间</th><th>沿革口径</th><th>人工主张</th>
                <th className="num">金额范围(元)</th><th>状态</th><th>裁定</th>
              </tr></thead>
              <tbody>
                {conflicts.data.map((c) => (
                  <tr key={c.id}>
                    <td><b>{c.plot_code}</b></td>
                    <td className="mono" style={{ fontSize: 11.5 }}>
                      {dayOf(c.interval_start)} {c.interval_start.slice(11, 16)}~{c.interval_end.slice(11, 16)}
                    </td>
                    <td className="mono" style={{ fontSize: 11.5 }}>{c.tenure_operator_code}</td>
                    <td className="mono" style={{ fontSize: 11.5 }}>{c.claimed_operator_code}</td>
                    <td className="num">{fmt(c.amount_lower, 2)} ~ {fmt(c.amount_upper, 2)}</td>
                    <td><Badge tone={c.status === "pending" ? "candidate" : "adjudicated"}>
                      {c.status === "pending" ? "待裁定" : "已裁定"}</Badge></td>
                    <td className="flex wrap gap-6">
                      {c.status === "pending" ? <>
                        <button className="ghost sm" onClick={() => decideConflict(c.id, "accept_tenure")}>采信沿革</button>
                        <button className="ghost sm" onClick={() => decideConflict(c.id, "accept_claim")}>采信主张</button>
                      </> : <span className="muted" style={{ fontSize: 12 }}>{c.evidence_refs.decision}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <h2 className="section-title">冻结后调整候选（只引用原快照）</h2>
      <Card>
        {adjusts.data.length === 0 ? <Empty text="冻结后暂无修订到达" /> : (
          <div className="table-wrap">
            <table className="data">
              <thead><tr>
                <th>地块</th><th>触发类型</th><th className="num">影响水量</th>
                <th className="num">影响金额</th><th>引用快照</th><th>状态</th><th>处置</th>
              </tr></thead>
              <tbody>
                {adjusts.data.map((c) => (
                  <tr key={c.id}>
                    <td><b>{c.plot_code}</b></td>
                    <td><Badge tone="warning">
                      { { manual_reading_correction: "人工读数更正", late_backfill: "迟到补传",
                          tenure_correction: "归属更正", price_correction: "价目更正" }[c.trigger_kind] || c.trigger_kind}
                    </Badge></td>
                    <td className="num">{fmt(c.impact_volume)}</td>
                    <td className="num" style={{ color: "var(--accent)", fontWeight: 700 }}>{fmt(c.impact_amount, 2)}</td>
                    <td className="mono">#{c.original_snapshot_id}</td>
                    <td><Badge tone={c.status === "proposed" ? "pending_verification"
                      : c.status === "accepted" ? "confirmed" : "rejected"}>
                      { { proposed: "待处置", accepted: "已接受", rejected: "已拒绝" }[c.status] || c.status}
                    </Badge></td>
                    <td className="flex gap-6">
                      {c.status === "proposed" ? <>
                        <button className="ghost sm" onClick={() => decideAdjust(c.id, "accepted")}>接受</button>
                        <button className="ghost sm" onClick={() => decideAdjust(c.id, "rejected")}>拒绝</button>
                      </> : <span className="muted" style={{ fontSize: 12 }}>{c.decision_operator}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {snapDetail && (
        <Drawer title={`快照冻结明细（${snapDetail.length} 项副本）`} wide onClose={() => setSnapDetail(null)}>
          <div className="detail-panel">
            <pre>{JSON.stringify(snapDetail.map((i) => ({ item_kind: i.item_kind, ref_uid: i.ref_uid, content: i.content })), null, 2)}</pre>
          </div>
        </Drawer>
      )}
    </div>
  );
}
