import React, { useCallback } from "react";
import { get } from "../api";
import { Card, Badge, useFetch, dayOf, Loading } from "../components";

const KIND = { device: "设备", manual: "人工", plan: "计划", system: "系统" };
const RT = { cumulative: "累计读数", instantaneous: "瞬时流量", plan: "计划" };

export default function Contracts() {
  const load = useCallback(() => get("/contracts"), []);
  const { data } = useFetch(load);
  if (!data) return <Loading />;
  return (
    <div>
      <div className="page-head">
        <h1>数据契约登记器</h1>
        <p>约束每类来源的设备编号模式、采集区间、事件/接收时间、累计或瞬时读数、计量单位与来源版本；契约不可原地修改，只能登记新版本</p>
      </div>
      <div className="grid cols-2">
        {data.map((c) => (
          <Card key={c.id} title={
            <span><b>{c.name}</b> <span className="mono muted">{c.code}</span></span>}>
            <div className="flex wrap gap-6" style={{ marginBottom: 10 }}>
              <Badge tone="primary">{KIND[c.source_kind]}</Badge>
              <Badge tone="neutral">{RT[c.reading_type]}</Badge>
              <Badge tone="target">单位 {c.unit}</Badge>
              <Badge tone="neutral">来源版本 {c.source_version}</Badge>
            </div>
            <table className="data">
              <tbody>
                <tr><td className="soft">设备编号模式</td><td className="mono">{c.device_id_pattern}</td></tr>
                <tr><td className="soft">采集区间</td><td className="mono">{c.collection_interval_sec} 秒</td></tr>
                <tr><td className="soft">生效起</td><td className="mono">{dayOf(c.active_from)}</td></tr>
                <tr><td className="soft">载荷必填字段</td><td className="mono">
                  {Object.entries(c.payload_schema || {}).map(([k, v]) =>
                    `${k}${v.required ? "*" : ""}`).join("，")}</td></tr>
              </tbody>
            </table>
          </Card>
        ))}
      </div>
    </div>
  );
}
