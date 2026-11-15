import React, { useCallback, useState } from "react";
import { get, post } from "../api";
import {
  Card, Badge, SeverityBadge, Drawer, useFetch, useToast,
  dayOf, timeOf, Loading, Empty,
} from "../components";

const RULE_NAME = {
  READING_REVERSAL: "读数倒退（禁止负供水）",
  METER_CHANGE_NO_VOUCHER: "换表缺证",
  METER_CHANGE_TRUNCATION: "换表缺证·区间截断",
  UNIT_MISMATCH: "计量单位不一致",
  TIME_OVERLAP: "时间重叠",
  CLOCK_DRIFT: "设备时钟漂移",
  GAP_MISSING: "采集缺测",
  CHANNEL_IMBALANCE: "渠道守恒失衡",
  EVIDENCE_CONFLICT: "设备/人工证据冲突",
  MANUAL_WITHOUT_BASIS: "人工补录缺依据",
  LATE_ARRIVAL: "迟到补传",
  CONTRACT_VIOLATION: "契约校验违规",
};

export default function Quality() {
  const toast = useToast();
  const [filter, setFilter] = useState("all");
  const [detail, setDetail] = useState(null);
  const loadEvents = useCallback(() => get("/quality/events"), []);
  const loadAdj = useCallback(() => get("/adjudications"), []);
  const ev = useFetch(loadEvents), adj = useFetch(loadAdj);

  const decide = async (adjId, decision) => {
    const rationale = window.prompt(
      `请输入裁定理由（${decision === "accept_device" ? "采信设备" : decision === "accept_manual" ? "采信人工" : "拒绝双方"}）`,
      "经核验证据可信度后裁定");
    if (rationale === null) return;
    try {
      await post(`/adjudications/${adjId}`, { decision, rationale, operator: "admin" });
      toast("裁定已保存，重编译后候选事实转为正式/拒绝", "ok");
      adj.reload(); ev.reload();
    } catch (e) { toast(e.message, "err"); }
  };

  if (!ev.data || !adj.data) return <Loading />;
  const events = filter === "all" ? ev.data : ev.data.filter((x) => x.status === filter);
  const pendingAdj = adj.data.filter((a) => a.decision === "pending");

  return (
    <div>
      <div className="page-head">
        <h1>质量规则引擎与人工裁定</h1>
        <p>读数倒退 / 单位不一致 / 时间重叠 / 守恒失衡 / 时钟漂移 / 缺测 / 补录缺依据；证据冲突须人工裁定后方可进入正式指标</p>
      </div>

      {pendingAdj.length > 0 && (
        <Card title={`待裁定证据冲突（${pendingAdj.length}）`} className="mt-16">
          {pendingAdj.map((a) => (
            <div key={a.id} className="diff-line" style={{ marginBottom: 10 }}>
              <b className="mono">{a.device_id}</b>　区间
              <span className="mono"> {dayOf(a.interval_start)} {timeOf(a.interval_start)}–
                {timeOf(a.interval_end)}</span>
              <div className="flex gap-6 mt-12">
                <button className="sm" onClick={() => decide(a.id, "accept_device")}>采信设备补传</button>
                <button className="sm ghost" onClick={() => decide(a.id, "accept_manual")}>采信人工补录</button>
                <button className="sm ghost" onClick={() => decide(a.id, "reject_both")}>双方均拒绝</button>
              </div>
            </div>
          ))}
          <p className="muted" style={{ fontSize: 12 }}>
            裁定后请到「供水事实账」点击「重新编译事实」，再到「指标版本」发布新版本，差异会写入版本差异说明。
          </p>
        </Card>
      )}

      <div className="pill-tabs mt-16">
        {["all", "open", "adjudicated", "closed"].map((s) => (
          <button key={s} className={filter === s ? "active" : ""} onClick={() => setFilter(s)}>
            { { all: "全部", open: "待处理", adjudicated: "已裁定", closed: "已闭环" }[s] }
          </button>
        ))}
      </div>

      <Card>
        {events.length === 0 ? <Empty /> : (
          <div className="table-wrap">
            <table className="data">
              <thead><tr>
                <th>规则</th><th>级别</th><th>设备/渠道</th><th>区间</th>
                <th>状态</th><th>摘要</th><th></th>
              </tr></thead>
              <tbody>
                {events.map((e) => (
                  <tr key={e.id}>
                    <td className="mono" style={{ fontSize: 12 }}>
                      {e.rule_code}<br /><span className="muted">{RULE_NAME[e.rule_code] || ""}</span>
                    </td>
                    <td><SeverityBadge severity={e.severity} /></td>
                    <td className="mono" style={{ fontSize: 12 }}>
                      {e.device_id || "—"}<br /><span className="muted">{e.channel_code || ""}</span>
                    </td>
                    <td className="mono" style={{ fontSize: 11.5 }}>
                      {e.interval_start ? `${dayOf(e.interval_start)} ${timeOf(e.interval_start)}` : "—"}
                    </td>
                    <td><Badge tone={e.status === "open" ? "pending_verification" :
                      e.status === "closed" ? "closed" : "adjudicated"}>{e.status}</Badge></td>
                    <td style={{ maxWidth: 320, fontSize: 12 }} className="soft">
                      {e.detail.action || e.detail.note || JSON.stringify(e.detail).slice(0, 90)}
                    </td>
                    <td><button className="ghost sm" onClick={() => setDetail(e)}>详情</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {detail && (
        <Drawer title={`质量事件 · ${detail.rule_code}`} onClose={() => setDetail(null)}>
          <div className="detail-panel"><pre>{JSON.stringify(detail.detail, null, 2)}</pre></div>
          <p className="mt-12 soft">
            关联事实 ID：<span className="mono">{(detail.related_fact_ids || []).join(", ") || "无"}</span>
          </p>
        </Drawer>
      )}
    </div>
  );
}
