import React, { useCallback, useState } from "react";
import { get, post } from "../api";
import { Card, Badge, useFetch, useToast, fmt, pct, dayOf, Loading, Empty, Bar } from "../components";

export default function Reconciliation() {
  const toast = useToast();
  const [resolving, setResolving] = useState(null);
  const [resolution, setResolution] = useState("");
  const loadRec = useCallback(() => get("/reconciliations"), []);
  const loadRuns = useCallback(() => get("/recompute-runs"), []);
  const rec = useFetch(loadRec), runs = useFetch(loadRuns);

  const resolve = async (id) => {
    if (!resolution.trim()) { toast("请填写对账处置说明", "err"); return; }
    try {
      await post(`/reconciliations/${id}/resolve`, { resolution, operator: "admin" });
      toast("对账任务已闭环，相关质量事件已关闭", "ok");
      setResolving(null); setResolution("");
      rec.reload(); runs.reload();
    } catch (e) { toast(e.message, "err"); }
  };

  if (!rec.data || !runs.data) return <Loading />;

  return (
    <div>
      <div className="page-head">
        <h1>渠道守恒对账与重算任务</h1>
        <p>上游确认量与下游可解释量超容差即冻结损耗指标并生成对账任务；冻结期间禁止用计划量补平</p>
      </div>

      <Card title="对账任务">
        {rec.data.length === 0 ? <Empty text="暂无对账任务" /> : (
          <div className="table-wrap">
            <table className="data">
              <thead><tr>
                <th>渠道</th><th>区间</th><th className="num">上游确认</th>
                <th className="num">下游可解释</th><th className="num">差异</th>
                <th>差异率(容差5%)</th><th>冻结指标</th><th>状态</th><th>处置</th>
              </tr></thead>
              <tbody>
                {rec.data.map((t) => (
                  <tr key={t.id}>
                    <td><b>{t.channel_code}</b></td>
                    <td className="mono" style={{ fontSize: 11.5 }}>
                      {dayOf(t.interval_start)} ~ {dayOf(t.interval_end)}</td>
                    <td className="num">{fmt(t.upstream_value)}</td>
                    <td className="num">{fmt(t.downstream_explained)}</td>
                    <td className="num" style={{ color: "var(--danger)", fontWeight: 700 }}>{fmt(t.diff)}</td>
                    <td style={{ minWidth: 150 }}>
                      <Bar ratio={t.diff_ratio} tone="danger" />
                      <span className="mono" style={{ fontSize: 11 }}>{pct(t.diff_ratio)}</span>
                    </td>
                    <td className="mono" style={{ fontSize: 11 }}>
                      {(t.frozen_metric_keys || []).map((k) => <div key={k}>{k}</div>)}
                    </td>
                    <td><Badge tone={t.status === "resolved" ? "resolved" : "frozen"}>
                      {t.status === "resolved" ? "已闭环" : "冻结中"}</Badge></td>
                    <td>
                      {t.status === "resolved" ?
                        <span className="muted" style={{ fontSize: 12 }}>{t.resolution}</span> :
                        <button className="ghost sm" onClick={() => setResolving(t.id)}>对账处置</button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {resolving && (
        <Card title={`对账处置说明 · 任务 #${resolving}`} className="mt-16">
          <label className="field">处置结论（将记录操作人与说明，作为解冻依据）
            <textarea rows={3} value={resolution} onChange={(e) => setResolution(e.target.value)}
              placeholder="例如：经现场核查系一支渠换表缺证导致计量缺口，已补办检定凭证并补录……" />
          </label>
          <div className="flex gap-6">
            <button onClick={() => resolve(resolving)}>提交并闭环</button>
            <button className="ghost" onClick={() => { setResolving(null); setResolution(""); }}>取消</button>
          </div>
        </Card>
      )}

      <h2 className="section-title">重算任务登记（可回放）</h2>
      <Card>
        {runs.data.length === 0 ? <Empty text="暂无重算任务" /> : (
          <div className="table-wrap">
            <table className="data">
              <thead><tr><th>任务</th><th>触发原因</th><th>状态</th>
                <th>影响指标键数</th><th>说明</th><th>开始时间</th></tr></thead>
              <tbody>
                {runs.data.map((r) => (
                  <tr key={r.id}>
                    <td className="mono">{r.run_uid}</td>
                    <td><Badge tone="neutral">{r.trigger}</Badge></td>
                    <td><Badge tone={r.status === "done" ? "confirmed" : "pending_verification"}>{r.status}</Badge></td>
                    <td className="num">{(r.affected_keys || []).length}</td>
                    <td className="soft" style={{ fontSize: 12 }}>{r.note}</td>
                    <td className="mono muted" style={{ fontSize: 11.5 }}>{r.started_at.slice(0, 16).replace("T", " ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
