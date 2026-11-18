import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { enableSound } from "./sound";
import type { Alert, SetupRow, TradeRow } from "./types";
import { useScreener } from "./useScreener";
import { Header } from "./components/Header";
import { ArchiveTable, EntryTable, Panel, PullbackTable, Surge1Table } from "./components/Tables";
import { AlertFeed, Toasts } from "./components/Alerts";
import { DetailDrawer, type DetailTarget } from "./components/DetailDrawer";
import { HealthModal, SettingsModal } from "./components/Modals";

type KindFilter = "ALL" | "EQ" | "FUT";
const GRADE_RANK: Record<string, number> = { A: 3, B: 2, C: 1 };

function load<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : (JSON.parse(v) as T);
  } catch {
    return fallback;
  }
}

function save(key: string, v: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(v));
  } catch {
    /* per-viewer convenience only */
  }
}

export default function App() {
  const [soundOn, setSoundOn] = useState(false);
  const [theme, setTheme] = useState<string>(() =>
    load("imbalance.theme", window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark"));
  const [kind, setKind] = useState<KindFilter>(() => load("imbalance.kind", "ALL"));
  const [minGrade, setMinGrade] = useState<string>(() => load("imbalance.minGrade", "C"));
  const [query, setQuery] = useState("");
  const [detail, setDetail] = useState<DetailTarget | null>(null);
  const [modal, setModal] = useState<"settings" | "health" | null>(null);
  const [muted, setMuted] = useState<Set<number>>(new Set());
  const [rvolMin, setRvolMin] = useState(5);
  const { snapshot, alerts, toasts, conn, status, dismiss, eventTick } = useScreener(soundOn);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    save("imbalance.theme", theme);
  }, [theme]);
  useEffect(() => save("imbalance.kind", kind), [kind]);
  useEffect(() => save("imbalance.minGrade", minGrade), [minGrade]);
  useEffect(() => {
    if (snapshot?.health?.alerts?.muted) setMuted(new Set(snapshot.health.alerts.muted));
  }, [snapshot?.health?.alerts?.muted?.join(",")]);
  useEffect(() => {
    api.config().then((c) => setRvolMin(Number(c.params.surge_rvol_min) || 5)).catch(() => undefined);
  }, [snapshot?.config_version]);

  const pass = useCallback((r: { kind: string; grade: string | null; display: string }) => {
    if (kind === "EQ" && r.kind !== "EQ") return false;
    if (kind === "FUT" && r.kind === "EQ") return false;
    if (minGrade !== "C" && (GRADE_RANK[r.grade ?? "C"] ?? 1) < GRADE_RANK[minGrade]) return false;
    if (query && !r.display.toLowerCase().includes(query.toLowerCase())) return false;
    return true;
  }, [kind, minGrade, query]);

  const surge1 = useMemo(() => (snapshot?.surge1 ?? []).filter(pass), [snapshot, pass]);
  const pullback = useMemo(() => (snapshot?.pullback ?? []).filter(pass)
    .sort((a, b) => (a.state === b.state ? 0 : a.state === "ARMED" ? -1 : 1)), [snapshot, pass]);
  const entries = useMemo(() => (snapshot?.entries ?? []).filter(pass), [snapshot, pass]);
  const archive = useMemo(() => (snapshot?.archive ?? []).filter((r) => pass({ kind: r.kind, grade: r.grade, display: r.display })), [snapshot, pass]);

  const openSetup = (r: SetupRow) => setDetail({ setupId: r.id, token: r.token });
  const openTrade = (t: TradeRow) => setDetail({ setupId: t.setup_id, token: t.token });
  const openArchive = (r: SetupRow | TradeRow) => ("setup_id" in r ? openTrade(r as TradeRow) : openSetup(r as SetupRow));
  const openAlert = (a: Alert) => a.token && setDetail({ setupId: a.setup_id, token: a.token });

  const toggleSound = () => {
    if (!soundOn) enableSound();
    setSoundOn(!soundOn);
  };
  const setMute = async (token: number, v: boolean) => {
    try {
      await api.mute(token, v);
      setMuted((m) => { const n = new Set(m); if (v) n.add(token); else n.delete(token); return n; });
    } catch {
      /* surfaced by the API token prompt in Parameters */
    }
  };

  const g = snapshot?.guardrails;
  return (
    <div className="app">
      <Header snap={snapshot} conn={conn} soundOn={soundOn} onSound={toggleSound} onSettings={() => setModal("settings")}
        onHealth={() => setModal("health")} theme={theme} onTheme={() => setTheme(theme === "light" ? "dark" : "light")} />
      {status && status.status !== "running" && (
        <div className="banner info">
          {status.status === "login_required" ? <>Kite login required — <a href="/kite/login">log in</a> to start today's session.</> :
            `Screener ${status.status}${status.message ? `: ${status.message}` : ""}`}
        </div>
      )}
      {g?.tripped && <div className="banner">Daily guardrail hit ({g.tripped.replace(/_/g, " ")}): new entry alerts are paused and only logged.</div>}
      {snapshot && !snapshot.health.feed.connected && snapshot.health.mode === "live" && (
        <div className="banner">Market-data feed disconnected — reconnecting. Candles spanning the gap cannot trigger.</div>
      )}
      <div className="toolbar">
        <div className="seg" role="group" aria-label="Instrument filter">
          {(["ALL", "EQ", "FUT"] as KindFilter[]).map((k) => (
            <button key={k} className={kind === k ? "on" : ""} onClick={() => setKind(k)}>{k === "ALL" ? "EQ + FUT" : `${k} only`}</button>
          ))}
        </div>
        <div className="seg" role="group" aria-label="Minimum grade">
          {["C", "B", "A"].map((x) => (
            <button key={x} className={minGrade === x ? "on" : ""} onClick={() => setMinGrade(x)}>{x === "C" ? "All grades" : `${x}${x === "B" ? "+" : ""}`}</button>
          ))}
        </div>
        <input className="search" placeholder="Filter symbol…" value={query} onChange={(e) => setQuery(e.target.value)} />
        <span className="spacer" />
        {snapshot && (
          <span className="faint">
            {snapshot.counts.scannable} of {snapshot.counts.instruments} instruments scanned · {snapshot.counts.live_setups} live setups · config {snapshot.config_version}
          </span>
        )}
      </div>
      {!snapshot ? (
        <div className="notice">{conn === "open" ? "Waiting for the first snapshot…" : "Connecting to the screener…"}</div>
      ) : (
        <div className="main">
          <div className="col">
            <div className="row2">
              <Panel title="Surge 1 watchlist" tag="A" count={surge1.length}>
                <Surge1Table rows={surge1} onOpen={openSetup} muted={muted} />
              </Panel>
              <Panel title="Pullback / armed" tag="B" count={pullback.length}>
                <PullbackTable rows={pullback} onOpen={openSetup} muted={muted} />
              </Panel>
            </div>
            <Panel title="Entry signals" tag="C" count={entries.length}>
              <EntryTable rows={entries} onOpen={openTrade} muted={muted} />
            </Panel>
            <Panel title="Archive — invalidated, expired, suppressed" tag="D" count={archive.length}>
              <ArchiveTable rows={archive} onOpen={openArchive} />
            </Panel>
            <div className="footer">
              Alerts only — no orders are placed. A volume footprint is not proof of an institution; all trading decisions remain yours.
            </div>
          </div>
          <AlertFeed alerts={alerts} onOpen={openAlert} filter={(a) => !a.token || pass({ kind: a.display.includes("FUT") ? "FUT" : "EQ", grade: a.grade, display: a.display })} />
        </div>
      )}
      <Toasts toasts={toasts} onOpen={openAlert} onDismiss={dismiss} />
      {detail && (
        <DetailDrawer target={detail} onClose={() => setDetail(null)} eventTick={eventTick} theme={theme}
          muted={muted.has(detail.token)} onMute={(v) => setMute(detail.token, v)} rvolMin={rvolMin} />
      )}
      {modal === "settings" && <SettingsModal onClose={() => setModal(null)} />}
      {modal === "health" && <HealthModal health={snapshot?.health} onClose={() => setModal(null)} />}
    </div>
  );
}
