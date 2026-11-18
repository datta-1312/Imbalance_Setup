import { Fragment, useEffect, useMemo, useState } from "react";
import { api, setApiToken } from "../api";
import type { ConfigPayload, Health, LatencySummary } from "../types";
import { num } from "../format";

export function SettingsModal({ onClose }: { onClose: () => void }) {
  const [cfg, setCfg] = useState<ConfigPayload | null>(null);
  const [draft, setDraft] = useState<Record<string, string | number | boolean>>({});
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [token, setToken] = useState("");
  const [presetName, setPresetName] = useState("");

  useEffect(() => {
    api.config().then((c) => { setCfg(c); setDraft({ ...c.params }); }).catch((e) => setError(String(e)));
  }, []);

  const groups = useMemo(() => {
    if (!cfg) return [] as [string, string[]][];
    const g: Record<string, string[]> = {};
    for (const k of Object.keys(cfg.params)) {
      const group = cfg.meta[k]?.group ?? "Other";
      (g[group] ??= []).push(k);
    }
    const order = ["Surge 1", "Pullback", "Surge 2", "Expiry", "Futures OI", "Session", "Risk", "Grade", "Events", "Direction", "Baseline", "Other"];
    return order.filter((x) => g[x]).map((x) => [x, g[x]] as [string, string[]]);
  }, [cfg]);

  if (!cfg) return <ModalShell title="Parameters" onClose={onClose}>{error ? <div className="error">{error}</div> : <div className="notice">Loading…</div>}</ModalShell>;

  const changed = Object.fromEntries(Object.entries(draft).filter(([k, v]) => v !== cfg.params[k]));
  const save = async () => {
    setError(null);
    setSaved(null);
    try {
      if (token) setApiToken(token);
      const out = await api.putConfig({ params: changed });
      setCfg(out);
      setDraft({ ...out.params });
      setSaved(`Saved. Config version ${out.version}; the engine uses it from the next candle.`);
    } catch (e) {
      setError(String(e));
    }
  };
  const applyPreset = async (name: string) => {
    setError(null);
    try {
      if (token) setApiToken(token);
      const out = await api.putConfig({ active_preset: name || null });
      setCfg(out);
      setDraft({ ...out.params });
      setSaved(name ? `Preset “${name}” active.` : "Preset cleared.");
    } catch (e) {
      setError(String(e));
    }
  };

  const savePreset = async () => {
    const name = presetName.trim();
    if (!name) return;
    setError(null);
    try {
      if (token) setApiToken(token);
      const diff = Object.fromEntries(Object.entries(draft).filter(([k, v]) => v !== cfg.defaults[k]));
      const out = await api.putConfig({ presets: { [name]: diff } });
      setCfg(out);
      setSaved(`Saved preset “${name}” (${Object.keys(diff).length} values that differ from the defaults).`);
      setPresetName("");
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <ModalShell title="Parameters" onClose={onClose} actions={
      <>
        <select className="search" value={cfg.active_preset ?? ""} onChange={(e) => applyPreset(e.target.value)} title="Named presets">
          <option value="">No preset</option>
          {Object.keys(cfg.presets).map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <input className="search" placeholder="API token (if required)" type="password" value={token} onChange={(e) => setToken(e.target.value)} />
        <button className="btn" onClick={() => setDraft({ ...cfg.params })} disabled={!Object.keys(changed).length}>Reset</button>
        <button className="btn primary" onClick={save} disabled={!Object.keys(changed).length}>Save {Object.keys(changed).length || ""}</button>
      </>
    }>
      {error && <div className="error">{error}</div>}
      {saved && <div className="banner info" style={{ margin: "0 0 4px" }}>{saved}</div>}
      <div style={{ display: "flex", gap: 8, padding: "0 14px", alignItems: "center", flexWrap: "wrap" }}>
        <input className="search" placeholder="Preset name" value={presetName} onChange={(e) => setPresetName(e.target.value)} />
        <button className="btn" onClick={savePreset} disabled={!presetName.trim()}>Save current values as preset</button>
      </div>
      <div className="faint" style={{ padding: "0 14px" }}>
        Defaults are starting values to tune by backtesting, not proven settings. Changes are written to config/local.yaml and hot-reloaded. Version {cfg.version}.
      </div>
      {groups.map(([group, keys]) => (
        <section className="panel" key={group}>
          <div className="panel-head"><h2>{group}</h2></div>
          <div className="params">
            {keys.map((k) => {
              const meta = cfg.meta[k];
              const v = draft[k];
              const def = cfg.defaults[k];
              const isChanged = v !== cfg.params[k];
              const options = meta?.options ?? (typeof v === "boolean" ? [false, true] : undefined);
              return (
                <div className="param" key={k}>
                  <label htmlFor={`p-${k}`}><span>{k}</span><span className="faint">{meta?.section ? `§${meta.section}` : ""}</span></label>
                  {options ? (
                    <select id={`p-${k}`} className={isChanged ? "changed" : ""} value={String(v)}
                      onChange={(e) => setDraft({ ...draft, [k]: typeof v === "boolean" ? e.target.value === "true" : e.target.value })}>
                      {options.map((o) => <option key={String(o)} value={String(o)}>{String(o)}</option>)}
                    </select>
                  ) : (
                    <input id={`p-${k}`} className={isChanged ? "changed" : ""} value={String(v)}
                      type={typeof def === "number" ? "number" : "text"} step="any"
                      onChange={(e) => setDraft({ ...draft, [k]: typeof def === "number" ? (e.target.value === "" ? "" : Number(e.target.value)) : e.target.value })} />
                  )}
                  <span className="hint">
                    default {String(def)}{meta?.unit ? ` ${meta.unit}` : ""}{meta?.range ? ` · test ${meta.range.join("–")}` : ""}
                  </span>
                </div>
              );
            })}
          </div>
        </section>
      ))}
    </ModalShell>
  );
}

const lat = (l?: LatencySummary) => (l && l.count ? `p50 ${num(l.p50, 2)} · p99 ${num(l.p99, 2)} · max ${num(l.max, 2)} ms (${l.count})` : "–");

export function HealthModal({ health, onClose }: { health: Health | undefined; onClose: () => void }) {
  if (!health) return <ModalShell title="Health" onClose={onClose}><div className="notice">No data yet.</div></ModalShell>;
  const b = health.processor.builder;
  return (
    <ModalShell title="System health" onClose={onClose}>
      <div className="grid2">
        <section className="panel">
          <div className="panel-head"><h2>Feed</h2></div>
          <dl className="kv">
            <dt>Adapter</dt><dd>{health.feed.adapter} · {health.feed.connected ? "connected" : "disconnected"}</dd>
            <dt>Subscribed</dt><dd>{health.feed.subscribed || health.universe.instruments}</dd>
            <dt>Reconnects</dt><dd>{health.feed.reconnects}</dd>
            <dt>Snapshots · frames</dt><dd>{health.feed.ticks.toLocaleString("en-IN")} · {health.feed.frames.toLocaleString("en-IN")}</dd>
            <dt>Ticks per second</dt><dd>{num(health.ticks_per_second, 1)}</dd>
            <dt>Stale instruments</dt><dd>{health.processor.stale.length ? health.processor.stale.join(", ") : "none"}</dd>
          </dl>
        </section>
        <section className="panel">
          <div className="panel-head"><h2>Latency</h2></div>
          <dl className="kv">
            <dt>Tick → state</dt><dd>{lat(health.tick_to_state_ms)}</dd>
            <dt>Candle close batch</dt><dd>{lat(health.processor.close_latency_ms)}</dd>
            <dt>Confirmed alerts</dt><dd>{lat(health.alerts.latency_confirmed_ms)}</dd>
            <dt>Provisional alerts</dt><dd>{lat(health.alerts.latency_provisional_ms)}</dd>
          </dl>
        </section>
        <section className="panel">
          <div className="panel-head"><h2>Candles</h2></div>
          <dl className="kv">
            {Object.entries(b).map(([k, v]) => <Fragment key={k}><dt>{k.replace(/_/g, " ")}</dt><dd>{v.toLocaleString("en-IN")}</dd></Fragment>)}
          </dl>
        </section>
        <section className="panel">
          <div className="panel-head"><h2>Alerts &amp; universe</h2></div>
          <dl className="kv">
            {Object.entries(health.alerts.counts).map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd>{v}</dd></Fragment>)}
            <dt>Telegram</dt><dd>{health.alerts.telegram ? `${health.alerts.telegram.sent} sent · ${health.alerts.telegram.failed} failed` : "off"}</dd>
            <dt>Instruments · scannable</dt><dd>{health.universe.instruments} · {health.universe.scannable}</dd>
            <dt>F&amp;O ban</dt><dd>{health.universe.banned.length ? health.universe.banned.join(", ") : "none"}</dd>
            <dt>Config version</dt><dd>{health.config_version}</dd>
          </dl>
          {health.universe.notes.length > 0 && <div className="faint" style={{ padding: "0 14px 12px" }}>{health.universe.notes.join(" · ")}</div>}
        </section>
      </div>
    </ModalShell>
  );
}

function ModalShell({ title, onClose, actions, children }: { title: string; onClose: () => void; actions?: React.ReactNode; children: React.ReactNode }) {
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="modal" role="dialog" aria-label={title}>
        <div className="drawer-head">
          <h3>{title}</h3>
          <span className="spacer" />
          {actions}
          <button className="btn" onClick={onClose}>Close ✕</button>
        </div>
        <div className="drawer-body">{children}</div>
      </div>
    </>
  );
}
