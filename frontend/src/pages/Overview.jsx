import React, { useCallback } from "react";
import { Link } from "react-router-dom";
import { get, post } from "../api";
import { Card, Kpi, Bar, Badge, FactStatusBadge, MetricStatusBadge, useFetch, useToast, fmt, pct } from "../components";

export default function Overview() {
  const toast = useToast();
  const loadMetrics = useCallback(() => get("/metrics"), []);
  const loadEvents = useCallback(() => get("/quality/events"), []);
  const loadFacts = useCallback(() => get("/facts"), []);
  const loadRecs = useCallback(() => get("/reconciliations"), []);
  const m = useFetch(loadMetrics), e = useFetch(loadEvents);
  const f = useFetch(loadFacts), r = useFetch(loadRecs);

  const refresh = async () => {
    try {
      await post("/compile");
      await post("/metrics/publish?trigger=manual&note=前端手动重算发布");
      toast("已重新编译并发布新版本", "ok");
      [m, e, f, r].forEach((x) => x.reload());
    } catch (err) { toast(err.message, "err"); }
  };

  if (!m.data || !e.data || !f.data || !r.data) return <div className="empty">加载中…</div>;

  const metrics = m.data;
  const totalPlanned = metrics.reduce((s, x) => s + (x.grain_type === "batch" ? x.planned_volume : 0), 0);
  const totalConfirmed = metrics.reduce((s, x) => s + (x.grain_type === "batch" ? x.confirmed_volume : 0), 0);
  const totalPending = metrics.reduce((s, x) => s + (x.grain_type === "batch" ? x.pending_volume : 0), 0);
  const openEvents = e.data.filter((x) => x.status === "open" || x.status === "frozen");
  const frozenCount = metrics.filter((x) => x.status === "frozen").length;
  const facts = f.data;
  const candidateCount = facts.filter((x) => x.status === "candidate").length;
  const recOpen = r.data.filter((x) => x.status !== "resolved");

  const ruleSummary = e.data.reduce((acc, x) => {
    acc[x.rule_code] = acc[x.rule_code] || { count: 0, open: 0 };
    acc[x.rule_code].count += 1;
    if (x.status === "open" || x.status === "frozen") acc[x.rule_code].open += 1;
    return acc;
  }, {});

  return (
    <div>
      <div className="page-head flex">
        <div style={{ flex: 1 }}>
          <h1>治理总览</h1>
          <p>按地块 / 渠道 / 轮灌批次的计划目标、可确认实灌、待核实、损耗与质量风险一览（最新发布版本）</p>
        </div>
        <button onClick={refresh}>重新编译并发布</button>
      </div>

      <div className="grid cols-4">
        <Kpi label="计划水量（批次目标）" value={fmt(totalPlanned)} unit="m³"
          sub="仅作比对，不充当实灌" />
        <Kpi label="可确认实灌量" value={fmt(totalConfirmed)} unit="m³"
          tone="var(--ok)" sub={`计划完成率 ${pct(totalPlanned ? totalConfirmed / totalPlanned : 0)}`} />
        <Kpi label="待核实量" value={fmt(totalPending)} unit="m³"
          tone="var(--warn)" sub={`候选事实 ${candidateCount} 条未裁定`} />
        <Kpi label="开放质量事件 / 冻结指标" value={`${openEvents.length} / ${frozenCount}`}
          tone={openEvents.length ? "var(--danger)" : "var(--ok)"}
          sub={`开放对账任务 ${recOpen.length} 个`} />
      </div>

      <div className="grid cols-2 mt-16">
        <Card title="批次计划完成情况（指标 → 可追溯）">
          <div className="table-wrap">
            <table className="data">
              <thead><tr><th>粒度/对象</th><th className="num">计划</th><th className="num">确认</th>
                <th className="num">待核实</th><th>完成率</th><th>状态</th><th></th></tr></thead>
              <tbody>
                {metrics.map((x) => (
                  <tr key={x.id}>
                    <td><Badge tone="neutral">{x.grain_type}</Badge> <b>{x.grain_key}</b></td>
                    <td className="num">{fmt(x.planned_volume)}</td>
                    <td className="num">{fmt(x.confirmed_volume)}</td>
                    <td className="num">{fmt(x.pending_volume)}</td>
                    <td style={{ minWidth: 110 }}>
                      <Bar ratio={x.plan_completion_rate || 0}
                        tone={(x.plan_completion_rate || 0) > 1 ? "danger" : ""} />
                      <span className="muted mono" style={{ fontSize: 11 }}>{pct(x.plan_completion_rate)}</span>
                    </td>
                    <td><MetricStatusBadge status={x.status} /></td>
                    <td><Link to={`/metrics/${x.id}/lineage`} className="clickable">血缘</Link></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <Card title="质量规则命中（题面四类治理场景已实际触发）">
          <div className="table-wrap">
            <table className="data">
              <thead><tr><th>规则</th><th className="num">命中</th><th className="num">待处理</th></tr></thead>
              <tbody>
                {Object.entries(ruleSummary).sort().map(([code, s]) => (
                  <tr key={code}>
                    <td className="mono">{code}</td>
                    <td className="num">{s.count}</td>
                    <td className="num">
                      {s.open > 0 ? <Badge tone="critical">{s.open}</Badge> :
                        <Badge tone="closed">0</Badge>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-12 flex wrap gap-6">
            <Link to="/quality"><button className="ghost sm">去处理裁定</button></Link>
            <Link to="/reconciliation"><button className="ghost sm">去处理对账</button></Link>
            <Link to="/staging"><button className="ghost sm">查看暂存报文</button></Link>
          </div>
        </Card>
      </div>

      <div className="grid cols-3 mt-16">
        <Card title="事实账状态分布">
          {["confirmed", "candidate", "pending_verification", "target"].map((st) => {
            const cnt = facts.filter((x) => x.status === st).length;
            return (
              <div key={st} className="flex" style={{ justifyContent: "space-between", margin: "7px 0" }}>
                <FactStatusBadge status={st} />
                <span className="mono">{cnt} 条</span>
              </div>
            );
          })}
        </Card>
        <Card title="平台纪律">
          <ul style={{ margin: 0, paddingLeft: 18, color: "var(--text-soft)", fontSize: 12.5, lineHeight: 1.9 }}>
            <li>补传写入新报文行，旧报文保留不覆盖</li>
            <li>换表缺证即截断，倒退绝不解释为负供水</li>
            <li>证据冲突双候选，未裁定不进正式指标</li>
            <li>渠道失衡冻结损耗，禁止计划量补平</li>
            <li>每次发布生成新版本并写差异说明</li>
          </ul>
        </Card>
        <Card title="开放对账任务">
          {recOpen.length === 0 && <div className="empty">无</div>}
          {recOpen.map((t) => (
            <div key={t.id} className="diff-line" style={{ marginBottom: 8 }}>
              <b>{t.channel_code}</b> <Badge tone="frozen">{t.status}</Badge><br />
              上游 {fmt(t.upstream_value)} − 下游可解释 {fmt(t.downstream_explained)}
              ，差异率 <b className="mono">{pct(t.diff_ratio)}</b>
            </div>
          ))}
        </Card>
      </div>
    </div>
  );
}
