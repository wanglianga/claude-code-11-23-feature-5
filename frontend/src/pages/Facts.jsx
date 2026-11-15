import React, { useCallback, useState } from "react";
import { get, post } from "../api";
import {
  Card, Badge, FactStatusBadge, Drawer, useFetch, useToast,
  fmt, dayOf, timeOf, Loading, Empty,
} from "../components";

const KIND_LABEL = { supply: "供水", transfer: "交接", plan: "计划目标" };

export default function Facts() {
  const toast = useToast();
  const [status, setStatus] = useState("all");
  const [grain, setGrain] = useState("all");
  const [detail, setDetail] = useState(null);
  const load = useCallback(() => get("/facts"), []);
  const { data, reload } = useFetch(load);

  const recompile = async () => {
    try {
      const r = await post("/compile");
      toast(`编译完成：供水事实 ${r.supply_facts_written} 条，计划事实 ${r.plan_facts_written} 条`, "ok");
      reload();
    } catch (e) { toast(e.message, "err"); }
  };

  if (!data) return <Loading />;
  let rows = data;
  if (status !== "all") rows = rows.filter((x) => x.status === status);
  if (grain !== "all") rows = rows.filter((x) => x[grain === "plot" ? "plot_code" : "channel_code"]);

  return (
    <div>
      <div className="page-head flex">
        <div style={{ flex: 1 }}>
          <h1>供水事实账（追加式）</h1>
          <p>相邻有效累计读数差量 / 瞬时流量积分 / 交接计量 / 计划目标；重算以新行取代旧行，历史行永不删除</p>
        </div>
        <button onClick={recompile}>重新编译事实</button>
      </div>

      <div className="toolbar">
        <div className="pill-tabs" style={{ margin: 0 }}>
          {["all", "confirmed", "candidate", "pending_verification", "target"].map((s) => (
            <button key={s} className={status === s ? "active" : ""} onClick={() => setStatus(s)}>
              {s === "all" ? "全部状态" : { confirmed: "已确认", candidate: "候选",
                pending_verification: "待核实", target: "计划目标" }[s]}
            </button>
          ))}
        </div>
        <div className="spacer" />
        <select value={grain} onChange={(e) => setGrain(e.target.value)} style={{ width: 150 }}>
          <option value="all">全部粒度</option>
          <option value="channel">渠道事实</option>
          <option value="plot">地块事实</option>
        </select>
      </div>

      <Card>
        {rows.length === 0 ? <Empty /> : (
          <div className="table-wrap">
            <table className="data">
              <thead><tr>
                <th>类型</th><th>状态</th><th>设备</th><th>计量点/渠道</th><th>地块/批次</th>
                <th>区间</th><th className="num">读数(起→止)</th><th className="num">水量(m³)</th>
                <th>方法</th><th>标记</th><th></th>
              </tr></thead>
              <tbody>
                {rows.map((f) => (
                  <tr key={f.id}>
                    <td><Badge tone="neutral">{KIND_LABEL[f.fact_kind]}</Badge></td>
                    <td><FactStatusBadge status={f.status} /></td>
                    <td className="mono">{f.device_id || "—"}</td>
                    <td className="mono" style={{ fontSize: 11.5 }}>
                      {f.meter_point_code || "—"}<br /><span className="muted">{f.channel_code}</span>
                    </td>
                    <td className="mono" style={{ fontSize: 11.5 }}>
                      {f.plot_code || "—"}<br /><span className="muted">{f.batch_code}</span>
                    </td>
                    <td className="mono" style={{ fontSize: 11.5 }}>
                      {dayOf(f.interval_start)}<br />
                      {timeOf(f.interval_start)}–{timeOf(f.interval_end)}
                    </td>
                    <td className="num mono">{fmt(f.reading_start, 1)} → {fmt(f.reading_end, 1)}</td>
                    <td className="num" style={{ fontWeight: 700 }}>{fmt(f.value)}</td>
                    <td className="muted" style={{ fontSize: 11.5 }}>{f.method}</td>
                    <td className="flex wrap gap-6">
                      {f.truncated && <Badge tone="critical">截断</Badge>}
                      {f.is_late && <Badge tone="warning">迟到</Badge>}
                      {f.quality_flag && <Badge tone="candidate">{f.quality_flag}</Badge>}
                    </td>
                    <td><button className="ghost sm" onClick={() => setDetail(f)}>证据</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {detail && (
        <Drawer title={`事实证据链 · ${detail.fact_uid}`} onClose={() => setDetail(null)}>
          <div className="detail-panel">
            <b>事实完整字段</b>
            <pre>{JSON.stringify(detail, null, 2)}</pre>
          </div>
          <p className="mt-12 soft">
            证据报文 ID：<span className="mono">{detail.evidence_message_ids.join(", ") || "无"}</span><br />
            人工证据 ID：<span className="mono">{detail.evidence_manual_ids.join(", ") || "无"}</span><br />
            映射版本：<span className="mono">v{detail.mapping_version ?? "—"}</span>　·
            修订代次：<span className="mono">rev{detail.revision_no}</span>
          </p>
          <p className="soft">可在「接入暂存区」按报文 ID 查看原始报文与被补传的历史版本。</p>
        </Drawer>
      )}
    </div>
  );
}
