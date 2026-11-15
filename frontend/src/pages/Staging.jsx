import React, { useCallback, useState } from "react";
import { get, post } from "../api";
import { Card, Badge, Drawer, useFetch, useToast, dayOf, timeOf, Loading, Empty } from "../components";

export default function Staging() {
  const toast = useToast();
  const [tab, setTab] = useState("raw");
  const [device, setDevice] = useState("");
  const [detail, setDetail] = useState(null);
  const [form, setForm] = useState(_blankRaw());
  const loadRaw = useCallback(() => get(`/ingestion/raw${device ? `?device_id=${device}` : ""}`), [device]);
  const loadMan = useCallback(() => get(`/ingestion/manual${device ? `?device_id=${device}` : ""}`), [device]);
  const raw = useFetch(loadRaw), man = useFetch(loadMan);

  function _blankRaw() {
    return { message_uid: `MSG-${Date.now()}`, contract_code: "CT-CUM-01", device_id: "D-HEAD-01",
      reading: 100, unit: "m3", event_time: "2026-11-10T07:00:00+00:00",
      received_at: "2026-11-12T10:00:00+00:00", source_version: "fw-1.4", supersedes_uid: "" };
  }
  const [manForm, setManForm] = useState({ revision_uid: `MAN-${Date.now()}`, contract_code: "CT-MAN-01",
    device_id: "D-B2-01", event_time: "2026-11-10T02:30:00+00:00", value: 50, unit: "m3",
    basis: "", operator: "admin", received_at: "2026-11-12T11:00:00+00:00" });

  const sendRaw = async () => {
    try {
      const r = await post("/ingestion/raw", {
        ...form, supersedes_uid: form.supersedes_uid || null,
        payload: { reading: Number(form.reading), unit: form.unit } });
      toast(r.is_late ? `已入暂存区（判定为迟到补传 rev${r.revision_no}）` : "报文已原样入暂存区", "ok");
      if (r.violations?.length) toast(`契约违规：${r.violations.join("；")}`, "err");
      setForm(_blankRaw()); raw.reload();
    } catch (e) { toast(e.message, "err"); }
  };
  const sendMan = async () => {
    try {
      const r = await post("/ingestion/manual", { ...manForm, value: Number(manForm.value) });
      toast("人工修订已留痕" + (r.violations?.length ? `（${r.violations.join("；")}）` : ""),
        r.violations?.length ? "err" : "ok");
      man.reload();
    } catch (e) { toast(e.message, "err"); }
  };

  if (!raw.data || !man.data) return <Loading />;

  return (
    <div>
      <div className="page-head">
        <h1>接入暂存区（原样保存 · 历史不可覆盖）</h1>
        <p>设备报文与人工修订均原样留存；补传以 supersedes 链写入新行，旧行保留；人工补录必须带依据</p>
      </div>

      <div className="toolbar">
        <input style={{ width: 220 }} placeholder="按设备编号过滤，如 D-B1-01"
          value={device} onChange={(e) => setDevice(e.target.value)} />
        <div className="spacer" />
        <div className="pill-tabs" style={{ margin: 0 }}>
          <button className={tab === "raw" ? "active" : ""} onClick={() => setTab("raw")}>设备报文 ({raw.data.length})</button>
          <button className={tab === "man" ? "active" : ""} onClick={() => setTab("man")}>人工修订 ({man.data.length})</button>
        </div>
      </div>

      <div className="grid cols-2">
        <Card title={tab === "raw" ? "投递设备报文（可模拟补传）" : "设备报文暂存"}>
          {tab === "raw" ? (
            <div>
              <div className="form-row">
                <label className="field">报文 UID（重复则幂等拒绝）
                  <input value={form.message_uid} onChange={(e) => setForm({ ...form, message_uid: e.target.value })} /></label>
                <label className="field">契约
                  <input value={form.contract_code} onChange={(e) => setForm({ ...form, contract_code: e.target.value })} /></label>
              </div>
              <div className="form-row">
                <label className="field">设备编号
                  <input value={form.device_id} onChange={(e) => setForm({ ...form, device_id: e.target.value })} /></label>
                <label className="field">来源版本
                  <input value={form.source_version} onChange={(e) => setForm({ ...form, source_version: e.target.value })} /></label>
              </div>
              <div className="form-row">
                <label className="field">累计/瞬时读数
                  <input type="number" value={form.reading} onChange={(e) => setForm({ ...form, reading: e.target.value })} /></label>
                <label className="field">单位（填 L 可触发单位不一致）
                  <input value={form.unit} onChange={(e) => setForm({ ...form, unit: e.target.value })} /></label>
              </div>
              <div className="form-row">
                <label className="field">事件时间
                  <input value={form.event_time} onChange={(e) => setForm({ ...form, event_time: e.target.value })} /></label>
                <label className="field">接收时间（晚于事件15分钟判迟到）
                  <input value={form.received_at} onChange={(e) => setForm({ ...form, received_at: e.target.value })} /></label>
              </div>
              <label className="field">supersedes_uid（填被补传的旧报文 UID 即形成补传链）
                <input value={form.supersedes_uid} onChange={(e) => setForm({ ...form, supersedes_uid: e.target.value })}
                  placeholder="可选，如 MSG-HEAD-06" /></label>
              <button onClick={sendRaw}>投递报文</button>
            </div>
          ) : <RawTable rows={raw.data} onOpen={setDetail} />}
        </Card>

        <Card title={tab === "man" ? "投递人工补录（依据必填）" : "人工修订暂存"}>
          {tab === "man" ? (
            <div>
              <div className="form-row">
                <label className="field">修订 UID
                  <input value={manForm.revision_uid} onChange={(e) => setManForm({ ...manForm, revision_uid: e.target.value })} /></label>
                <label className="field">设备编号
                  <input value={manForm.device_id} onChange={(e) => setManForm({ ...manForm, device_id: e.target.value })} /></label>
              </div>
              <div className="form-row">
                <label className="field">事件时间
                  <input value={manForm.event_time} onChange={(e) => setManForm({ ...manForm, event_time: e.target.value })} /></label>
                <label className="field">水量/读数
                  <input type="number" value={manForm.value} onChange={(e) => setManForm({ ...manForm, value: e.target.value })} /></label>
              </div>
              <label className="field">补录依据（留空将触发 MANUAL_WITHOUT_BASIS）
                <textarea rows={2} value={manForm.basis} onChange={(e) => setManForm({ ...manForm, basis: e.target.value })}
                  placeholder="例如：便携式超声流量计现场比对记录" /></label>
              <div className="form-row">
                <label className="field">操作人
                  <input value={manForm.operator} onChange={(e) => setManForm({ ...manForm, operator: e.target.value })} /></label>
                <label className="field">接收时间
                  <input value={manForm.received_at} onChange={(e) => setManForm({ ...manForm, received_at: e.target.value })} /></label>
              </div>
              <button onClick={sendMan}>投递人工修订</button>
            </div>
          ) : (
            <div className="table-wrap">
              <table className="data">
                <thead><tr><th>设备</th><th>事件时间</th><th className="num">值</th><th>依据</th><th>操作人</th></tr></thead>
                <tbody>
                  {man.data.map((m) => (
                    <tr key={m.id}>
                      <td className="mono">{m.device_id}</td>
                      <td className="mono" style={{ fontSize: 11.5 }}>{dayOf(m.event_time)} {timeOf(m.event_time)}</td>
                      <td className="num">{m.value} {m.unit}</td>
                      <td style={{ maxWidth: 160 }}>{m.basis ?
                        <span className="soft" style={{ fontSize: 12 }}>{m.basis}</span> :
                        <Badge tone="critical">缺依据</Badge>}</td>
                      <td>{m.operator}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

      {tab === "raw" && (
        <Card title="设备报文暂存（含补传历史行）" className="mt-16">
          <RawTable rows={raw.data} onOpen={setDetail} />
        </Card>
      )}

      {detail && (
        <Drawer title="原始报文（原样留痕）" onClose={() => setDetail(null)}>
          <div className="detail-panel"><pre>{JSON.stringify(detail, null, 2)}</pre></div>
        </Drawer>
      )}
    </div>
  );
}

function RawTable({ rows, onOpen }) {
  if (!rows.length) return <Empty text="暂无报文" />;
  return (
    <div className="table-wrap">
      <table className="data">
        <thead><tr><th>UID / 代次</th><th>设备</th><th>事件时间</th><th>接收时间</th>
          <th className="num">读数</th><th>标记</th><th></th></tr></thead>
        <tbody>
          {rows.map((m) => (
            <tr key={m.id}>
              <td className="mono" style={{ fontSize: 11.5 }}>
                {m.message_uid} <span className="muted">rev{m.revision_no}</span>
                {m.supersedes_uid && <div className="muted">取代 {m.supersedes_uid}</div>}
              </td>
              <td className="mono">{m.device_id}</td>
              <td className="mono" style={{ fontSize: 11.5 }}>{dayOf(m.event_time)} {timeOf(m.event_time)}</td>
              <td className="mono muted" style={{ fontSize: 11.5 }}>{dayOf(m.received_at)} {timeOf(m.received_at)}</td>
              <td className="num mono">{m.payload.reading} {m.payload.unit}</td>
              <td className="flex wrap gap-6">
                {m.is_late && <Badge tone="warning">迟到补传</Badge>}
                {m.payload.unit !== "m3" && m.payload.unit !== "m3_h" && <Badge tone="critical">单位异常</Badge>}
              </td>
              <td><button className="ghost sm" onClick={() => onOpen(m)}>原文</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
