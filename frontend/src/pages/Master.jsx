import React, { useCallback } from "react";
import { get } from "../api";
import { Card, Badge, useFetch, dayOf, Loading, Empty } from "../components";

const ROLE = { head: "渠首", branch: "支渠", field: "田间", transfer: "交接" };

export default function Master() {
  const load = useCallback(() => get("/master/tree"), []);
  const { data } = useFetch(load);
  if (!data) return <Loading />;
  return (
    <div>
      <div className="page-head">
        <h1>水系与地块主数据 · 生效区间映射</h1>
        <p>渠道 / 计量点 / 地块 / 轮灌批次 / 设备换表关系，按 SCD2 生效区间与映射版本组织，历史映射保留可回溯</p>
      </div>

      <div className="grid cols-2">
        <Card title="水系渠道与计量点">
          <table className="data">
            <thead><tr><th>渠道</th><th>层级</th><th>上级</th><th>计量点</th><th>角色</th></tr></thead>
            <tbody>
              {data.channels.map((c) => {
                const points = data.meter_points.filter((p) => p.channel_code === c.code);
                return (
                  <tr key={c.code}>
                    <td><b>{c.name}</b> <span className="mono muted">{c.code}</span></td>
                    <td>L{c.level}</td>
                    <td className="mono muted">{c.parent_code || "—"}</td>
                    <td>{points.map((p) => <div key={p.code} className="mono" style={{ fontSize: 12 }}>{p.code}</div>)}</td>
                    <td>{points.map((p) => <div key={p.code}><Badge tone="neutral">{ROLE[p.point_role]}</Badge></div>)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>

        <Card title="地块与轮灌批次">
          <h4 style={{ fontSize: 13 }}>地块</h4>
          <table className="data">
            <thead><tr><th>地块</th><th className="num">面积(亩)</th><th>供水渠道</th></tr></thead>
            <tbody>
              {data.plots.map((p) => (
                <tr key={p.code}><td>{p.name} <span className="mono muted">{p.code}</span></td>
                  <td className="num">{p.area_mu}</td><td className="mono">{p.channel_code}</td></tr>
              ))}
            </tbody>
          </table>
          <h4 className="mt-12" style={{ fontSize: 13 }}>轮灌批次</h4>
          {data.batches.map((b) => (
            <div key={b.code} className="diff-line">
              <b>{b.name}</b> <span className="mono muted">{b.code}</span><br />
              <span className="mono" style={{ fontSize: 11.5 }}>{b.planned_start} ~ {b.planned_end}</span>
            </div>
          ))}
        </Card>
      </div>

      <Card title="设备 → 计量点 SCD2 映射（含换表生效区间）" className="mt-16">
        <div className="table-wrap">
          <table className="data">
            <thead><tr><th>设备</th><th>计量点</th><th>生效起</th><th>生效止</th>
              <th>映射版本</th><th>变更原因</th></tr></thead>
            <tbody>
              {data.device_mappings.map((m, i) => (
                <tr key={i}>
                  <td className="mono">{m.device_id}</td><td className="mono">{m.meter_point_code}</td>
                  <td className="mono" style={{ fontSize: 11.5 }}>{dayOf(m.effective_from)} {m.effective_from.slice(11, 16)}</td>
                  <td className="mono muted">{m.effective_to ? `${dayOf(m.effective_to)} ${m.effective_to.slice(11, 16)}` : "至今"}</td>
                  <td><Badge tone="target">v{m.mapping_version}</Badge></td>
                  <td className="soft">{m.change_reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="grid cols-2 mt-16">
        <Card title="地块轮灌分配（计划水量为目标量）">
          <table className="data">
            <thead><tr><th>地块</th><th>批次</th><th className="num">计划水量(m³)</th><th>生效</th></tr></thead>
            <tbody>
              {data.allocations.map((a, i) => (
                <tr key={i}><td className="mono">{a.plot_code}</td><td className="mono">{a.batch_code}</td>
                  <td className="num">{a.planned_volume}</td><td className="mono muted">{dayOf(a.effective_from)}</td></tr>
              ))}
            </tbody>
          </table>
        </Card>

        <Card title="换表记录与凭证状态">
          {data.meter_changes.length === 0 ? <Empty text="暂无换表记录" /> : (
            <table className="data">
              <thead><tr><th>设备</th><th>换表时间</th><th>凭证</th><th>状态</th><th>备注</th></tr></thead>
              <tbody>
                {data.meter_changes.map((c) => (
                  <tr key={c.id}>
                    <td className="mono">{c.device_id}</td>
                    <td className="mono" style={{ fontSize: 11.5 }}>{c.change_time.replace("T", " ").slice(0, 16)}</td>
                    <td>{c.voucher_present ? <Badge tone="confirmed">有凭证</Badge> : <Badge tone="critical">缺凭证</Badge>}</td>
                    <td><Badge tone={c.status === "certified" ? "certified" : "missing_voucher"}>{c.status}</Badge></td>
                    <td className="soft" style={{ fontSize: 12 }}>{c.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>
    </div>
  );
}
