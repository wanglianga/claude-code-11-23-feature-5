import React, { createContext, useContext, useState, useCallback } from "react";

// ---------------- 状态徽章 ----------------
export function Badge({ children, tone = "neutral" }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

const FACT_TONE = {
  confirmed: "confirmed", candidate: "candidate",
  pending_verification: "pending_verification", rejected: "rejected", target: "target",
};
const METRIC_TONE = { stable: "stable", tentative: "tentative", frozen: "frozen" };
const SEV_TONE = { critical: "critical", warning: "warning", info: "info" };

export function FactStatusBadge({ status }) {
  const label = { confirmed: "已确认", candidate: "候选", pending_verification: "待核实",
    rejected: "已拒绝", target: "计划目标" }[status] || status;
  return <Badge tone={FACT_TONE[status] || "neutral"}>{label}</Badge>;
}
export function MetricStatusBadge({ status }) {
  const label = { stable: "稳定", tentative: "暂定", frozen: "冻结" }[status] || status;
  return <Badge tone={METRIC_TONE[status] || "neutral"}>{label}</Badge>;
}
export function SeverityBadge({ severity }) {
  return <Badge tone={SEV_TONE[severity] || "neutral"}>
    { { critical: "严重", warning: "警告", info: "提示" }[severity] || severity }
  </Badge>;
}
export function RuleBadge({ code, status }) {
  const tone = { open: "pending_verification", adjudicated: "adjudicated",
    frozen: "frozen", closed: "closed" }[status] || "neutral";
  return <Badge tone={tone}>{code}</Badge>;
}

// ---------------- 卡片 / KPI / 条形 ----------------
export function Card({ title, extra, children, className = "", tight }) {
  return (
    <div className={`card ${tight ? "tight" : ""} ${className}`}>
      {title && (
        <h3>
          <span style={{ float: "right" }}>{extra}</span>
          {title}
        </h3>
      )}
      {children}
    </div>
  );
}

export function Kpi({ label, value, unit, sub, tone }) {
  return (
    <Card className="kpi">
      <div className="label">{label}</div>
      <div className={`value ${unit ? "unit" : ""}`} style={tone ? { color: tone } : undefined}>
        {value}{unit && <span style={{ fontSize: 13, color: "var(--text-muted)" }}> {unit}</span>}
      </div>
      {sub && <div className="sub">{sub}</div>}
    </Card>
  );
}

export function Bar({ ratio, tone = "", title }) {
  const pct = Math.max(0, Math.min(100, (ratio || 0) * 100));
  return (
    <div className={`bar ${tone}`} title={title || `${pct.toFixed(1)}%`}>
      <span style={{ width: `${pct}%` }} />
    </div>
  );
}

export function Empty({ text = "暂无数据" }) {
  return <div className="empty">{text}</div>;
}

export function Loading() {
  return <div className="empty">加载中…</div>;
}

// ---------------- 抽屉 ----------------
export function Drawer({ title, onClose, children, wide }) {
  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <div className="drawer" style={wide ? { width: 640 } : undefined}>
        <span className="close" onClick={onClose}>×</span>
        <h3>{title}</h3>
        <div className="mt-12">{children}</div>
      </div>
    </>
  );
}

// ---------------- Toast ----------------
const ToastCtx = createContext(null);
export function useToast() { return useContext(ToastCtx); }

export function ToastProvider({ children }) {
  const [toast, setToast] = useState(null);
  const show = useCallback((message, kind = "") => {
    setToast({ message, kind });
    setTimeout(() => setToast(null), 3200);
  }, []);
  return (
    <ToastCtx.Provider value={show}>
      {children}
      {toast && <div className={`toast ${toast.kind}`}>{toast.message}</div>}
    </ToastCtx.Provider>
  );
}

// ---------------- 通用小工具 ----------------
export function fmt(n, digits = 1) {
  if (n === null || n === undefined) return "—";
  return Number(n).toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
export function pct(n, digits = 1) {
  if (n === null || n === undefined) return "—";
  return `${(Number(n) * 100).toFixed(digits)}%`;
}
export function dayOf(iso) {
  return iso ? iso.slice(0, 10) : "—";
}
export function timeOf(iso) {
  return iso ? iso.slice(11, 16) : "—";
}

export function useFetch(getFn) {
  const [data, setData] = React.useState(null);
  const [error, setError] = React.useState(null);
  const [tick, setTick] = React.useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  React.useEffect(() => {
    let alive = true;
    setData(null);
    getFn()
      .then((d) => alive && setData(d))
      .catch((e) => alive && setError(e.message));
    return () => { alive = false; };
  }, [getFn, tick]);
  return { data, error, reload };
}
