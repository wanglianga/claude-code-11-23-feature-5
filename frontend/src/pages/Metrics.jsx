import React, { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { get, post } from "../api";
import {
  Card, Badge, MetricStatusBadge, Drawer, useFetch, useToast,
  fmt, pct, dayOf, Loading, Empty,
} from "../components";

const GRAIN_LABEL = { plot: "地块", channel: "渠道", batch: "轮灌批次" };

export default function Metrics() {
  const toast = useToast();
  const [grain, setGrain] = useState("all");
  const [drawer, setDrawer] = useState(null);
  const load = useCallback(() => get("/metrics?latest_only=true"), []);
  const { data, reload } = useFetch(load);

  const publish = async () => {
    try {
      const r = await post("/metrics/publish?trigger=manual&note=前端触发发布");
      toast(`已发布 ${r.published} 个指标新版本`, "ok");
      reload();
    } catch (e) { toast(e.message, "err"); }
  };

  const openVersions = async (m) => {
    const versions = await get(`/metrics/${m.id}/versions`);
    setDrawer({ metric: m, versions });
  };

  if (!data) return <Loading />;
  const rows = grain === "all" ? data : data.filter((x) => x.grain_type === grain);

  return (
    <div>
      <div className="page-head flex">
        <div style={{ flex: 1 }}>
          <h1>指标版本服务</h1>
          <p>按事件时间 / 水位线 / 发布稳定期计算；每次更正追加新版本，旧版本保留并附差异说明，可回放</p>
        </div>
        <button onClick={publish}>发布新版本</button>
      </div>

      <div className="pill-tabs">
        {["all", "plot", "channel", "batch"].map((g) => (
          <button key={g} className={grain === g ? "active" : ""} onClick={() => setGrain(g)}>
            {g === "all" ? "全部粒度" : GRAIN_LABEL[g]}
          </button>
        ))}
      </div>

      <Card>
        {rows.length === 0 ? <Empty /> : (
          <div className="table-wrap">
            <table className="data">
              <thead><tr>
                <th>日期</th><th>粒度</th><th>对象</th><th>批次</th>
                <th className="num">计划水量</th><th className="num">确认实灌</th>
                <th className="num">待核实</th><th>损耗区间(m³)</th>
                <th className="num">缺测率</th><th className="num">完成率</th>
                <th className="num">超配风险</th><th>状态</th><th>操作</th>
              </tr></thead>
              <tbody>
                {rows.map((x) => (
                  <tr key={x.id}>
                    <td className="mono">{dayOf(x.metric_date)}</td>
                    <td><Badge tone="neutral">{GRAIN_LABEL[x.grain_type]}</Badge></td>
                    <td><b>{x.grain_key}</b></td>
                    <td className="muted">{x.batch_code || "—"}</td>
                    <td className="num">{fmt(x.planned_volume)}</td>
                    <td className="num" style={{ color: "var(--ok)", fontWeight: 700 }}>{fmt(x.confirmed_volume)}</td>
                    <td className="num" style={{ color: x.pending_volume ? "var(--warn)" : undefined }}>
                      {fmt(x.pending_volume)}</td>
                    <td className="num mono">
                      {x.loss_lower === null ? <span className="muted">冻结/不适用</span>
                        : `[${fmt(x.loss_lower)}, ${fmt(x.loss_upper)}]`}
                    </td>
                    <td className="num">{pct(x.missing_rate)}</td>
                    <td className="num">{pct(x.plan_completion_rate)}</td>
                    <td className="num" style={{ color: x.over_allocation_risk > 0 ? "var(--danger)" : undefined }}>
                      {pct(x.over_allocation_risk)}</td>
                    <td><MetricStatusBadge status={x.status} /></td>
                    <td className="flex gap-6">
                      <button className="ghost sm" onClick={() => openVersions(x)}>版本/差异</button>
                      <Link className="btn ghost sm" to={`/metrics/${x.id}/lineage`}>血缘</Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {drawer && (
        <Drawer title={`版本回放 · ${GRAIN_LABEL[drawer.metric.grain_type]} ${drawer.metric.grain_key}`}
          onClose={() => setDrawer(null)} wide>
          <p className="soft">同一指标的每次发布都保留为独立版本，新版本通过 superseded_by 链接旧版本。</p>
          <table className="data">
            <thead><tr><th>版本</th><th className="num">计划</th><th className="num">确认</th>
              <th className="num">待核实</th><th>状态</th><th>差异说明</th><th>发布时间</th></tr></thead>
            <tbody>
              {drawer.versions.map((v) => (
                <tr key={v.id} style={v.superseded_by ? { color: "var(--text-muted)" } : undefined}>
                  <td className="mono">v{v.version_no}{v.superseded_by ? " (历史)" : " (当前)"}</td>
                  <td className="num">{fmt(v.planned_volume)}</td>
                  <td className="num">{fmt(v.confirmed_volume)}</td>
                  <td className="num">{fmt(v.pending_volume)}</td>
                  <td><MetricStatusBadge status={v.status} /></td>
                  <td><div className="diff-line">{v.diff_note}</div></td>
                  <td className="mono muted">{v.published_at.slice(0, 16).replace("T", " ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-12 detail-panel">
            <b>当前版本水位线 / 稳定期</b>
            <pre>{JSON.stringify({
              watermark: drawer.metric.watermark,
              stability_deadline: drawer.metric.stability_deadline,
              input_fact_ids: drawer.metric.input_fact_ids,
              recompute_run_id: drawer.metric.recompute_run_id,
            }, null, 2)}</pre>
          </div>
        </Drawer>
      )}
    </div>
  );
}
