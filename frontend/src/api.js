const BASE = "/api";

function getToken() {
  return localStorage.getItem("irr_token");
}

export async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(`${BASE}${path}`, { ...options, headers });
  if (res.status === 401) {
    localStorage.removeItem("irr_token");
    if (!path.includes("/auth/login")) window.location.reload();
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* ignore */ }
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

export const get = (p) => api(p);
export const post = (p, body) => api(p, { method: "POST", body: JSON.stringify(body || {}) });
