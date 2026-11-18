import type { ConfigPayload, InstrumentDetail, SetupDetail } from "./types";

const TOKEN_KEY = "imbalance.apiToken";

function authHeaders(): Record<string, string> {
  let token: string | null = null;
  try {
    token = localStorage.getItem(TOKEN_KEY);
  } catch {
    token = null;
  }
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export function setApiToken(token: string) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: token lives for this page only */
  }
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { headers: authHeaders() });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return (await r.json()) as T;
}

async function send<T>(method: string, path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  const text = await r.text();
  if (!r.ok) {
    let msg = text;
    try {
      const j = JSON.parse(text);
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* plain text */
    }
    throw new Error(`${r.status}: ${msg}`);
  }
  return JSON.parse(text) as T;
}

export const api = {
  setup: (id: string) => get<SetupDetail>(`/api/setups/${encodeURIComponent(id)}`),
  instrument: (token: number) => get<InstrumentDetail & { events: SetupDetail["events"] }>(`/api/instruments/${token}`),
  config: () => get<ConfigPayload>("/api/config"),
  putConfig: (patch: Record<string, unknown>) => send<ConfigPayload>("PUT", "/api/config", patch),
  mute: (token: number, value: boolean) => send<{ muted: boolean }>("POST", `/api/symbols/${token}/mute`, { value }),
  pause: (token: number, value: boolean) => send<{ paused: boolean }>("POST", `/api/symbols/${token}/pause`, { value }),
  status: () => get<{ status: string; message: string | null; login_url: string | null }>("/api/status"),
};
