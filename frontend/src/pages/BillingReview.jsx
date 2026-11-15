import React, { useCallback, useState } from "react";
import { get } from "../api";
import {
  Card, Badge, Drawer, useFetch, useToast, fmt, Loading, Empty,
} from "../components";

const PERIOD_START = "2026-11-01T00:00:00+00:00";
const PERIOD_END = "2026-12-01T00:00:00+00:00";
const Q = `period_start=${encodeURIComponent(PERIOD_START)}&period_end=${encodeURIComponent(PERIOD_END)}`;

const COLUMN_LABEL = {
  metering_accrual: "应计量口径",
  external_fact: "外部账务事实",
  late_revision: "后补修订影响",
};

export default function BillingReview() {
  const toast = useToast();
  const [trace, setTrace] = useState(null);
  const [column, setColumn] = useState("all");
  const loadReview = useCallback(() => get(`/billing/review?${Q}`), []);
  const loadBridge = useCallback(() => get("/billing/bridge"), []);
  const review = useFetch(loadReview);
  const bridge = useFetch(loadBridge);

  const openTrace = async (fragmentId) => {
    try { setTrace(await get(`/billing/trace?fragment_id=${fragmentId}`)); }
    catch (e) { toast(e.message, "err"); }
  };

  if (!review.data || !bridge.data) return <Loading />;
  let bridgeRows = bridge.data;
  if (column !== "all") bridgeRows = bridgeRows.filter((x) => x.column_kind === column);

  return (
    <div>
      <div className="page-head">
        <h1>审阅投影与差异桥（最小披露）</h1>
        <p>仅输出地块、账期、差异来源与必要证据指针（版本号/回执号/快照号），不展示经营户姓名等无关身份信息；每条差额均可回查五类依据</p>
      </div>

      <h2 className="section-title">按地块的审阅投影</h2>
      <Card>
        {review.data.length === 0 ? <Empty text="暂无对账数据" /> : (
          <div className="table-wrap">
            <table className="data">
              <thead><tr>
                <th>账期</th><th>地块</th><th className="num">确认水量(m³)</th>
                <th className="num">应计金额</th><th className="num">外部净额</th>
                <th className="num">差额</th><th>阻断/冲突</th><th>差异来源</th><th>必要证据</th>
              </tr></thead>
              <tbody>
                {review.data.map((r) => (
                  <tr key={`${r.period_key}-${r.plot_code}`}>
                    <td className="mono">{r.period_key}</td>
                    <td><b>{r.plot_code}</b></td>
                    <td className="num">{fmt(r.confirmed_volume)}</td>
                    <td className="num">{fmt(r.accrual_amount, 2)}</td>
                    <td className="num">{fmt(r.external_net_amount, 2)}</td>
                    <td className="num" style={{
                      color: Math.abs(r.diff_amount) > 0.01 ? "var(--danger)" : "var(--ok)",
                      fontWeight: 700 }}>{fmt(r.diff_amount, 2)}</td>
                    <td style={{ fontSize: 11.5 }}>
                      {r.blocked_count > 0 && <div><Badge tone="critical">待证 {r.blocked_count}</Badge></div>}
                      {r.attribution_conflicts.map((c) => (
                        <div key={c.conflict_id}>
                          <Badge tone="candidate">归属待裁定</Badge>
                          <span className="mono muted" style={{ fontSize: 10.5 }}>
                            {" "}{fmt(c.amount_range[0], 2)}~{fmt(c.amount_range[1], 2)} 元
                          </span>
                        </div>
                      ))}
                      {r.blocked_count === 0 && r.attribution_conflicts.length === 0 && <span className="muted">无</span>}
                    </td>
                    <td style={{ fontSize: 11 }}>
                      {r.diff_sources.length === 0 ? <span className="muted">—</span> :
                        r.diff_sources.map((s) => <div key={s} className="mono">{s}</div>)}
                    </td>
                    <td className="mono muted" style={{ fontSize: 10.5 }}>
                      <div>计量事实: {r.evidence.supply_fact_ids.join(",") || "—"}</div>
                      <div>归属版本: {r.evidence.tenure_versions.join(",") || "—"}</div>
                      <div>价目版本: {r.evidence.price_versions.join(",") || "—"}</div>
                      <div>回执: {r.evidence.receipt_nos.join(",") || "—"}</div>
                      <div>快照: {r.evidence.snapshot_ids.join(",") || "—"}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <h2 className="section-title">差异桥三列（应计量 / 外部账务 / 后补修订）</h2>
      <div className="toolbar">
        <div className="pill-tabs" style={{ margin: 0 }}>
          {["all", "metering_accrual", "external_fact", "late_revision"].map((c) => (
            <button key={c} className={column === c ? "active" : ""} onClick={() => setColumn(c)}>
              {c === "all" ? "全部三列" : COLUMN_LABEL[c]}
            </button>
          ))}
        </div>
      </div>
      <Card>
        {bridgeRows.length === 0 ? <Empty text="暂无差异桥行，请先在工作台重建差异桥" /> : (
          <div className="table-wrap">
            <table className="data">
              <thead><tr>
                <th>列别</th><th>地块</th><th>来源</th><th className="num">水量</th>
                <th className="num">金额(元)</th><th>差异原因</th><th>说明</th><th></th>
              </tr></thead>
              <tbody>
                {bridgeRows.map((e) => (
                  <tr key={e.id}>
                    <td><Badge tone={e.column_kind === "metering_accrual" ? "target"
                      : e.column_kind === "external_fact" ? "stable" : "tentative"}>
                      {COLUMN_LABEL[e.column_kind]}</Badge></td>
                    <td><b>{e.plot_code || "—"}</b></td>
                    <td className="mono" style={{ fontSize: 11 }}>{e.source_kind}</td>
                    <td className="num">{e.column_kind === "external_fact" ? "—" : fmt(e.volume)}</td>
                    <td className="num" style={{ fontWeight: 600 }}>{fmt(e.amount, 2)}</td>
                    <td>{e.diff_reason ? <Badge tone="critical">{e.diff_reason}</Badge> : "—"}</td>
                    <td className="soft" style={{ fontSize: 12 }}>{e.narration}</td>
                    <td>{e.source_ref.fragment_id ?
                      <button className="ghost sm" onClick={() => openTrace(e.source_ref.fragment_id)}>回查</button> : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {trace && (
        <Drawer title="差额回查链：计量区间 → 归属版本 → 价目版本 → 账期快照 → 外部回执" wide onClose={() => setTrace(null)}>
          <div className="detail-panel"><pre>{JSON.stringify(trace, null, 2)}</pre></div>
        </Drawer>
      )}
    </div>
  );
}
