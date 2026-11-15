import React, { useCallback, useState } from "react";
import { get, post } from "../api";
import {
  Card, Badge, Drawer, useFetch, useToast,
  fmt, dayOf, timeOf, Loading, Empty,
} from "../components";

const PERIOD_START = "2026-11-01T00:00:00+00:00";
const PERIOD_END = "2026-12-01T00:00:00+00:00";

const KIND_LABEL = {
  normal: "常规片段", tier_split: "阶梯拆分", cross_tenure_split: "跨租约拆段",
  cross_price_pending: "跨价目待证", attribution_disputed: "归属双候选",
};
const STATUS_TONE = {
  confirmed: "confirmed", blocked_pending: "critical", candidate: "candidate",
};
const STATUS_LABEL = {
  confirmed: "已确认", blocked_pending: "阻断待证", candidate: "归属候选",
};

export default function BillingFragments() {
  const toast = useToast();
  const [statusFilter, setStatusFilter] = useState("all");
  const [trace, setTrace] = useState(null);
  const load = useCallback(() => get("/billing/fragments"), []);
  const { data, reload } = useFetch(load);

  const recompile = async () => {
    try {
      const r = await post(
        `/billing/compile?period_start=${encodeURIComponent(PERIOD_START)}&period_end=${encodeURIComponent(PERIOD_END)}`);
      toast(`编译完成：片段 ${r.fragments_written} 条（确认 ${r.confirmed} / 阻断 ${r.blocked} / 候选 ${r.candidates}），取代旧片段 ${r.superseded} 条`, "ok");
      reload();
    } catch (e) { toast(e.message, "err"); }
  };

  const openTrace = async (id) => {
    try {
      const chain = await get(`/billing/trace?fragment_id=${id}`);
      setTrace(chain);
    } catch (e) { toast(e.message, "err"); }
  };

  if (!data) return <Loading />;
  let rows = data;
  if (statusFilter !== "all") rows = rows.filter((x) => x.status === statusFilter);

  return (
    <div>
      <div className="page-head flex">
        <div style={{ flex: 1 }}>
          <h1>区间分摊事实片段（追加式，重算以版本链取代）</h1>
          <p>计量区间按归属沿革与价目生效点拆成可解释片段；跨价目缺中间读数只发「待证」并阻断确认，绝不按任一价目猜测拆分</p>
        </div>
        <button onClick={recompile}>重新分摊编译</button>
      </div>

      <div className="toolbar">
        <div className="pill-tabs" style={{ margin: 0 }}>
          {["all", "confirmed", "blocked_pending", "candidate"].map((s) => (
            <button key={s} className={statusFilter === s ? "active" : ""} onClick={() => setStatusFilter(s)}>
              {s === "all" ? "全部" : STATUS_LABEL[s]}
            </button>
          ))}
        </div>
        <div className="spacer" />
        <span className="muted" style={{ fontSize: 12 }}>共 {rows.length} 条活动片段</span>
      </div>

      <Card>
        {rows.length === 0 ? <Empty text="暂无片段，请先在账期内编译" /> : (
          <div className="table-wrap">
            <table className="data">
              <thead><tr>
                <th>片段类型</th><th>状态</th><th>地块</th><th>计量区间</th>
                <th className="num">水量(m³)</th><th>分摊依据</th><th>归属版本</th>
                <th>价目/阶梯</th><th className="num">单价</th><th className="num">金额(元)</th>
                <th>阻断原因</th><th></th>
              </tr></thead>
              <tbody>
                {rows.map((f) => (
                  <tr key={f.id}>
                    <td><Badge tone="neutral">{KIND_LABEL[f.fragment_kind] || f.fragment_kind}</Badge></td>
                    <td><Badge tone={STATUS_TONE[f.status]}>{STATUS_LABEL[f.status]}</Badge></td>
                    <td><b>{f.plot_code}</b></td>
                    <td className="mono" style={{ fontSize: 11.5 }}>
                      {dayOf(f.interval_start)}<br />
                      {timeOf(f.interval_start)}–{timeOf(f.interval_end)}
                    </td>
                    <td className="num">{fmt(f.volume)}</td>
                    <td className="muted" style={{ fontSize: 11.5 }}>
                      { { reading_anchored: "读数锚定", time_proportional: "时间比例",
                          pending_evidence: "待证", manual_claim_whole_interval: "人工主张整段",
                          adjudicated_tenure: "裁定沿革", adjudicated_claim: "裁定主张" }[f.split_basis] || f.split_basis}
                    </td>
                    <td className="mono">{f.tenure_version ? `v${f.tenure_version}` : "—"}</td>
                    <td className="mono" style={{ fontSize: 11.5 }}>
                      {f.price_version_no ? `价目v${f.price_version_no} / 档${f.tier_no ?? "-"}` : "—"}
                    </td>
                    <td className="num">{f.unit_price == null ? "—" : fmt(f.unit_price, 2)}</td>
                    <td className="num" style={{ fontWeight: 700 }}>
                      {f.amount == null ? "—" : fmt(f.amount, 2)}
                      {f.relief_amount ? <div className="muted" style={{ fontSize: 10.5 }}>含减免 {fmt(f.relief_amount, 2)}</div> : null}
                    </td>
                    <td>{f.blocking_reason ?
                      <Badge tone="critical">{f.blocking_reason}</Badge> : "—"}</td>
                    <td><button className="ghost sm" onClick={() => openTrace(f.id)}>回查</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {trace && (
        <Drawer title="差额回查链（计量区间 / 归属版本 / 价目版本 / 快照 / 外部回执）" wide onClose={() => setTrace(null)}>
          <div className="detail-panel"><pre>{JSON.stringify(trace, null, 2)}</pre></div>
          <p className="soft mt-12" style={{ fontSize: 12 }}>
            回查结果仅含版本号、凭证号与回执号等必要证据，不含经营户姓名等无关身份信息。
          </p>
        </Drawer>
      )}
    </div>
  );
}
