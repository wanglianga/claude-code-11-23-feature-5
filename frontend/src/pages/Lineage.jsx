import React, { useCallback, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { get } from "../api";
import { Card, Badge, useFetch, Loading } from "../components";

// 分层：来源证据(0) -> 事实(1) -> 规则/裁定/对账/重算(2) -> 发布指标(3)
const LAYER = {
  contract: 0, raw: 0, manual: 0, mapping: 0, device_mapping: 0, meter_change: 0,
  fact: 1,
  adjudication: 2, quality: 2, reconciliation: 2, recompute: 2,
  metric: 3,
};
const KIND_NAME = {
  metric: "发布指标", fact: "供水事实", raw: "原始报文", manual: "人工证据",
  contract: "数据契约", mapping: "映射版本", device_mapping: "设备映射",
  meter_change: "换表记录", quality: "质量事件", adjudication: "人工裁定",
  reconciliation: "对账任务", recompute: "重算任务",
};
const COL_X = [40, 360, 700, 1040];
const NODE_W = 210, NODE_H = 64, ROW_GAP = 18;

export default function Lineage() {
  const { id } = useParams();
  const [selected, setSelected] = useState(null);
  const load = useCallback(() => get(`/metrics/${id}/lineage`), [id]);
  const { data, error } = useFetch(load);

  if (!data && !error) return <Loading />;
  if (error) return <div className="empty">血缘加载失败：{error}</div>;

  // 分层布局
  const layers = [[], [], [], []];
  data.nodes.forEach((n) => layers[LAYER[n.kind] ?? 1].push(n));
  const pos = {};
  layers.forEach((nodes, li) => {
    nodes.forEach((n, idx) => {
      pos[n.id] = { x: COL_X[li], y: 24 + idx * (NODE_H + ROW_GAP) };
    });
  });
  const height = Math.max(...layers.map((l) => l.length)) * (NODE_H + ROW_GAP) + 60;
  const width = COL_X[3] + NODE_W + 60;
  const nodeById = Object.fromEntries(data.nodes.map((n) => [n.id, n]));

  const edgePath = (e) => {
    const s = pos[e.source], t = pos[e.target];
    if (!s || !t) return null;
    const x1 = s.x + NODE_W, y1 = s.y + NODE_H / 2;
    const x2 = t.x, y2 = t.y + NODE_H / 2;
    const dx = Math.max((x2 - x1) / 2, 40);
    return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
  };

  return (
    <div>
      <div className="page-head flex">
        <div style={{ flex: 1 }}>
          <h1>血缘浏览器</h1>
          <p>从地块日指标回溯到原始报文、换表记录、映射版本、人工裁定与质量规则——每个发布数字均可追溯到输入与规则</p>
        </div>
        <Link to="/metrics"><button className="ghost">返回指标版本</button></Link>
      </div>

      <div className="legend">
        {Object.entries(KIND_NAME).map(([k, name]) => (
          <span key={k}><i className={`lg-${k}`}
            style={{ background: getComputedStyle(document.documentElement)
              .getPropertyValue("--surface"), borderColor: "var(--border)" }} />{name}</span>
        ))}
      </div>

      <div className="lineage-canvas" style={{ position: "relative", height, width }}>
        <svg width={width} height={height} style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
          <defs>
            <marker id="arrow" markerWidth="9" markerHeight="9" refX="8" refY="3"
              orient="auto" markerUnits="strokeWidth">
              <path d="M0,0 L8,3 L0,6 Z" fill="#8aa0b2" />
            </marker>
          </defs>
          {data.edges.map((e, i) => {
            const d = edgePath(e);
            return d ? <path key={i} d={d} fill="none" stroke="#8aa0b2" strokeWidth={1.4}
              markerEnd="url(#arrow)" opacity={0.75} /> : null;
          })}
        </svg>
        {data.nodes.map((n) => {
          const p = pos[n.id];
          return (
            <div key={n.id} className={`lineage-node kind-${n.kind}`}
              style={{ left: p.x, top: p.y, width: NODE_W, minHeight: NODE_H }}
              onClick={() => setSelected(n)}>
              <div className="ln-title">{n.label}</div>
              <div className="ln-sub">{KIND_NAME[n.kind]}</div>
            </div>
          );
        })}
      </div>

      <div className="grid cols-2 mt-16">
        <Card title="关系说明（边）">
          <div className="flex wrap gap-6">
            {[...new Set(data.edges.map((e) => e.label))].map((l) => (
              <Badge key={l} tone="neutral">{l}</Badge>
            ))}
          </div>
          <p className="muted mt-12" style={{ fontSize: 12 }}>
            共 {data.nodes.length} 个节点、{data.edges.length} 条血缘关系。点击任意节点查看其完整留痕字段。
          </p>
        </Card>
        <Card title={selected ? `节点详情 · ${KIND_NAME[selected.kind]}` : "节点详情"}>
          {!selected ? <div className="empty">点击图中节点查看详情</div> : (
            <div className="detail-panel">
              <div className="flex gap-6">
                <Badge tone="neutral">{KIND_NAME[selected.kind]}</Badge>
                <b>{selected.label}</b>
              </div>
              <pre>{JSON.stringify(selected.detail, null, 2)}</pre>
            </div>
          )}
        </Card>
      </div>

      <Card title="本指标涉及的全部上游事实" className="mt-16">
        <table className="data">
          <thead><tr><th>事实</th><th>类型/状态</th><th>入边来源</th></tr></thead>
          <tbody>
            {data.nodes.filter((n) => n.kind === "fact").map((n) => {
              const incoming = data.edges.filter((e) => e.target === n.id)
                .map((e) => `${nodeById[e.source]?.label || e.source}（${e.label}）`);
              return (
                <tr key={n.id}>
                  <td className="mono" style={{ fontSize: 12 }}>{n.label}</td>
                  <td><Badge tone="neutral">{n.detail.method}</Badge></td>
                  <td style={{ fontSize: 12 }} className="soft">{incoming.join("；")}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
