import React, { useCallback, useState } from "react";
import { get } from "../api";
import { Card, Badge, Drawer, useFetch, Loading, Empty, fmt, dayOf } from "../components";

const TABS = [
  { key: "tenure", label: "归属沿革账（双时间 SCD2）" },
  { key: "price", label: "价目版本账（阶梯/季节/减免）" },
  { key: "external", label: "外部账务事实账（无支付动作）" },
];

export default function BillingLedger() {
  const [tab, setTab] = useState("tenure");
  const [detail, setDetail] = useState(null);
  const loadTenures = useCallback(() => get("/billing/tenures"), []);
  const loadPrices = useCallback(() => get("/billing/price-versions"), []);
  const loadExternal = useCallback(() => get("/billing/external-entries"), []);
  const tenures = useFetch(loadTenures);
  const prices = useFetch(loadPrices);
  const externals = useFetch(loadExternal);

  return (
    <div>
      <div className="page-head">
        <h1>计费三本账：归属沿革 · 价目版本 · 外部账务事实</h1>
        <p>全部追加式 / SCD2 留痕：转包只新开归属区间，价目按批准版本保存，外部系统只接入账单行、冲补行、回执号与状态；平台不发起任何支付动作</p>
      </div>

      <div className="pill-tabs">
        {TABS.map((t) => (
          <button key={t.key} className={tab === t.key ? "active" : ""} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </div>

      {tab === "tenure" && (
        <Card title="归属沿革（业务有效区间 + 登记系统时间）">
          {!tenures.data ? <Loading /> : tenures.data.length === 0 ? <Empty /> : (
            <div className="table-wrap">
              <table className="data">
                <thead><tr>
                  <th>版本</th><th>地块</th><th>经营户编码</th><th>租约</th>
                  <th>业务有效区间</th><th>变更原因</th><th>凭证</th><th>登记时间（系统时间）</th>
                </tr></thead>
                <tbody>
                  {tenures.data.map((t) => (
                    <tr key={t.id}>
                      <td><Badge tone="primary">v{t.tenure_version}</Badge></td>
                      <td><b>{t.plot_code}</b></td>
                      <td className="mono">{t.operator_code}</td>
                      <td className="mono">{t.lease_code || "—"}</td>
                      <td className="mono" style={{ fontSize: 11.5 }}>
                        {dayOf(t.effective_from)} → {t.effective_to ? dayOf(t.effective_to) : "至今（开放）"}
                      </td>
                      <td><Badge tone={t.change_reason === "transfer" ? "candidate" : "neutral"}>
                        { { initial: "初始", transfer: "转包", lease_end: "租约结束", correction: "更正" }[t.change_reason] || t.change_reason}
                      </Badge></td>
                      <td className="mono" style={{ fontSize: 11.5 }}>{t.evidence_ref || "—"}</td>
                      <td className="mono muted" style={{ fontSize: 11.5 }}>{t.recorded_at.slice(0, 16).replace("T", " ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {tab === "price" && (
        <Card title="价目批准版本（旧版本保留可回放）">
          {!prices.data ? <Loading /> : prices.data.length === 0 ? <Empty /> : (
            <div className="table-wrap">
              <table className="data">
                <thead><tr>
                  <th>版本</th><th>价目</th><th>生效区间</th><th>状态</th>
                  <th>阶梯（m³ → 元/m³）</th><th>减免规则</th><th>季节说明</th>
                </tr></thead>
                <tbody>
                  {prices.data.map((p) => (
                    <tr key={p.id}>
                      <td><Badge tone="primary">v{p.version_no}</Badge></td>
                      <td><b>{p.code}</b><br /><span className="muted" style={{ fontSize: 11.5 }}>{p.name}</span></td>
                      <td className="mono" style={{ fontSize: 11.5 }}>
                        {dayOf(p.effective_from)} → {p.effective_to ? dayOf(p.effective_to) : "至今"}
                      </td>
                      <td><Badge tone={p.status === "approved" ? "confirmed" : "adjudicated"}>{p.status}</Badge></td>
                      <td className="mono" style={{ fontSize: 11.5 }}>
                        {p.tiers.map((t) => (
                          <div key={t.tier_no}>
                            档{t.tier_no}: [{fmt(t.volume_from, 0)}, {t.volume_to == null ? "∞" : fmt(t.volume_to, 0)})
                            = {fmt(t.unit_price, 2)} 元/m³
                            {t.season_tag ? `（${t.season_tag}）` : ""}
                          </div>
                        ))}
                      </td>
                      <td style={{ fontSize: 11.5 }}>
                        {p.relief_rules.length === 0 ? <span className="muted">无</span> :
                          p.relief_rules.map((r) => (
                            <div key={r.relief_code}>{r.name}：
                              {r.relief_kind === "rate" ? `比例 ${(r.rate * 100).toFixed(0)}%` : `定额 ${fmt(r.amount, 2)}`}
                            </div>
                          ))}
                      </td>
                      <td className="muted" style={{ fontSize: 11.5 }}>{p.season_note || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {tab === "external" && (
        <Card title="外部账务事实（账单行 / 冲补行 / 回执，原样留痕）">
          {!externals.data ? <Loading /> : externals.data.length === 0 ? <Empty /> : (
            <div className="table-wrap">
              <table className="data">
                <thead><tr>
                  <th>来源系统</th><th>行类型</th><th>回执号</th><th>账单行</th>
                  <th>地块</th><th>账期</th><th className="num">金额</th><th>方向</th>
                  <th>状态</th><th>指纹</th><th></th>
                </tr></thead>
                <tbody>
                  {externals.data.map((x) => (
                    <tr key={x.id}>
                      <td className="mono">{x.source_system}</td>
                      <td><Badge tone={x.entry_kind === "adjustment_line" ? "candidate" : "neutral"}>
                        { { bill_line: "账单行", adjustment_line: "冲补行", receipt: "回执" }[x.entry_kind] || x.entry_kind}
                      </Badge></td>
                      <td className="mono">{x.receipt_no || "—"}</td>
                      <td className="mono" style={{ fontSize: 11.5 }}>{x.bill_line_no || "—"}</td>
                      <td>{x.plot_code || "—"}</td>
                      <td className="mono" style={{ fontSize: 11.5 }}>
                        {dayOf(x.period_start)}~{dayOf(x.period_end)}
                      </td>
                      <td className="num" style={{ fontWeight: 700 }}>{fmt(x.amount, 2)}</td>
                      <td><Badge tone={x.direction === "debit" ? "critical" : "confirmed"}>
                        {x.direction === "debit" ? "应收(debit)" : "冲减(credit)"}
                      </Badge></td>
                      <td className="mono" style={{ fontSize: 11.5 }}>{x.status}</td>
                      <td className="mono muted" style={{ fontSize: 10.5 }}>{x.bill_line_fingerprint.slice(0, 12)}…</td>
                      <td><button className="ghost sm" onClick={() => setDetail(x)}>原文</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="soft mt-12" style={{ fontSize: 12 }}>
            指纹刻意排除回执号：同号同指纹同方向才算幂等重传；金额方向或指纹不同即冲突，详见「回执匹配与调整」页。
          </p>
        </Card>
      )}

      {detail && (
        <Drawer title={`外部账务原文 · ${detail.entry_uid}`} onClose={() => setDetail(null)}>
          <div className="detail-panel"><pre>{JSON.stringify(detail, null, 2)}</pre></div>
        </Drawer>
      )}
    </div>
  );
}
