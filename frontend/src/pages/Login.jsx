import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { post } from "../api";
import { useToast } from "../components";

export default function Login() {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("admin123");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const toast = useToast();

  const submit = async (e) => {
    e.preventDefault();
    setError(""); setLoading(true);
    try {
      const r = await post("/auth/login", { username, password });
      localStorage.setItem("irr_token", r.access_token);
      localStorage.setItem("irr_user", JSON.stringify({ name: r.name, role: r.role }));
      toast("登录成功", "ok");
      navigate("/");
    } catch (err) {
      setError(err.message || "登录失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <h1>灌区多源用水事实数据<br />与指标发布平台</h1>
        <p className="hint" style={{ textAlign: "center", marginTop: 12 }}>
          计划量仅作目标 · 实灌量需证据 · 更正留痕可回放
        </p>
        <div className="hint">
          测试账号：<b>admin / admin123</b>（管理员，可裁定/对账）<br />
          查看账号：<b>viewer / viewer123</b>
        </div>
        <label className="field">账号
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
        </label>
        <label className="field">密码
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error && <p className="error-text">{error}</p>}
        <button style={{ width: "100%", marginTop: 8 }} disabled={loading}>
          {loading ? "登录中…" : "登 录"}
        </button>
      </form>
    </div>
  );
}
