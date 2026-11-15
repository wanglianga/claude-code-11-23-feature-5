import React from "react";
import { Routes, Route, NavLink, useNavigate, Navigate } from "react-router-dom";
import { ToastProvider } from "./components";
import Login from "./pages/Login";
import Overview from "./pages/Overview";
import Staging from "./pages/Staging";
import Contracts from "./pages/Contracts";
import Master from "./pages/Master";
import Facts from "./pages/Facts";
import Quality from "./pages/Quality";
import Metrics from "./pages/Metrics";
import Lineage from "./pages/Lineage";
import Reconciliation from "./pages/Reconciliation";
import BillingLedger from "./pages/BillingLedger";
import BillingFragments from "./pages/BillingFragments";
import BillingControl from "./pages/BillingControl";
import BillingReview from "./pages/BillingReview";

const NAV = [
  { to: "/", label: "治理总览", ico: "▦", end: true },
  { to: "/metrics", label: "指标版本", ico: "∑" },
  { to: "/facts", label: "供水事实账", ico: "≣" },
  { to: "/billing/fragments", label: "计费分摊片段", ico: "⌗" },
  { to: "/billing/review", label: "对账审阅投影", ico: "◈" },
  { to: "/billing/control", label: "快照·回执·差异桥", ico: "❄" },
  { to: "/billing/ledger", label: "计费三本账", ico: "▤" },
  { to: "/quality", label: "质量事件与裁定", ico: "⚠" },
  { to: "/reconciliation", label: "对账与重算", ico: "⇄" },
  { to: "/staging", label: "接入暂存区", ico: "⛁" },
  { to: "/contracts", label: "数据契约", ico: "§" },
  { to: "/master", label: "主数据与映射", ico: "⌘" },
];

function Shell() {
  const navigate = useNavigate();
  const user = JSON.parse(localStorage.getItem("irr_user") || "null");
  const logout = () => {
    localStorage.removeItem("irr_token");
    localStorage.removeItem("irr_user");
    navigate("/login");
  };
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          灌区用水事实平台
          <small>多源治理 · 追加式事实账 · 可回放指标</small>
        </div>
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end}
            className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}>
            <span className="ico">{n.ico}</span>{n.label}
          </NavLink>
        ))}
        <div className="spacer" />
        <div className="user-box">
          <div>{user?.name || "未登录"}</div>
          <div style={{ color: "#9fc6cc" }}>角色：{user?.role || "—"}</div>
          <button className="ghost" style={{ color: "#fff", borderColor: "rgba(255,255,255,0.4)" }}
            onClick={logout}>退出登录</button>
        </div>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/metrics" element={<Metrics />} />
          <Route path="/metrics/:id/lineage" element={<Lineage />} />
          <Route path="/facts" element={<Facts />} />
          <Route path="/billing/fragments" element={<BillingFragments />} />
          <Route path="/billing/review" element={<BillingReview />} />
          <Route path="/billing/control" element={<BillingControl />} />
          <Route path="/billing/ledger" element={<BillingLedger />} />
          <Route path="/quality" element={<Quality />} />
          <Route path="/reconciliation" element={<Reconciliation />} />
          <Route path="/staging" element={<Staging />} />
          <Route path="/contracts" element={<Contracts />} />
          <Route path="/master" element={<Master />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  const token = localStorage.getItem("irr_token");
  return (
    <ToastProvider>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/*" element={token ? <Shell /> : <Navigate to="/login" replace />} />
      </Routes>
    </ToastProvider>
  );
}
