// Thin client for the JoeyLab HTTP API.

export class ApiError extends Error {}

export async function api(path, { method = "GET", body, params } = {}) {
  const url = params ? `${path}?${qs(params)}` : path;
  const opts = { method, headers: {} };
  // Every change carries JoeyLab's own header: a page on another site (or another port of this
  // Mac) can't send it without the browser asking first, and JoeyLab never says yes.
  if (method !== "GET") opts.headers["X-JoeyLab"] = "1";
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, opts);
  } catch {
    throw new ApiError("Can't reach JoeyLab. Is it still running on your Mac (joeylab serve, or the JoeyLab service)?");
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) throw new ApiError((data && data.error) || text || `Request failed (${res.status})`);
  return data;
}

export function qs(obj) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(obj || {})) {
    if (v === undefined || v === null || v === "" || v === false) continue;
    p.set(k, Array.isArray(v) ? v.join(",") : String(v));
  }
  return p.toString();
}

const e = encodeURIComponent;
export const img = {
  thumb: (p, r, size = "s") => `/img/${e(p)}/t/${r.id}?s=${size}&v=${r.v}`,
  preview: (p, r) => `/img/${e(p)}/v/${r.id}?v=${r.v}`,
  zoom: (p, r) => `/img/${e(p)}/z/${r.id}?v=${r.v}`,
  original: (p, fileId) => `/img/${e(p)}/o/${fileId}`,
};

export const P = (p, rest) => `/api/p/${e(p)}${rest}`;
