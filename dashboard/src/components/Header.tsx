import type { Snapshot } from "../types";
import { hms, num, pctRaw, rupees } from "../format";

interface Props {
  snap: Snapshot | null;
  conn: string;
  soundOn: boolean;
  onSound: () => void;
  onSettings: () => void;
  onHealth: () => void;
  theme: string;
  onTheme: () => void;
}

export function Header({ snap, conn, soundOn, onSound, onSettings, onHealth, theme, onTheme }: Props) {
  const h = snap?.health;
  const feedOk = h ? h.feed.connected && h.processor.feed_connected : false;
  const g = snap?.guardrails;
  const nifty = snap?.regime?.NIFTY;
  const p99 = h?.tick_to_state_ms?.p99 ?? h?.processor?.tick_latency_ms?.p99;
  const alertP99 = h?.alerts?.latency_confirmed_ms?.p99;
  return (
    <header className="header">
      <div className="brand">
        <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden>
          <rect width="32" height="32" rx="7" fill="var(--surface-2)" />
          <path d="M6 22l6-9 5 5 9-12" stroke="var(--up)" strokeWidth="3" fill="none" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        Imbalance Screener
        {h && <span className={`mode ${h.mode}`}>{h.mode.toUpperCase()}</span>}
      </div>
      <div className="clock" title="Exchange time (IST)">{snap ? hms(snap.now) : "--:--:--"}</div>
      <div className="stats">
        <span className="stat" title="Market-data feed">
          <small>Feed</small>
          <b>
            <span className={`dot ${conn !== "open" ? "wait" : feedOk ? "ok" : "bad"}`} />
            {conn !== "open" ? "UI reconnecting" : feedOk ? "connected" : "down"}
          </b>
        </span>
        <span className="stat opt"><small>Ticks/s</small><b>{h ? num(h.ticks_per_second, 0) : "–"}</b></span>
        <span className="stat opt" title="Tick-to-state latency, p99 (target < 50 ms)">
          <small>Tick→state p99</small><b>{p99 != null ? `${num(p99, 2)} ms` : "–"}</b>
        </span>
        {alertP99 != null && (
          <span className="stat opt" title="Candle close to confirmed alert, p99 (target < 1 s)">
            <small>Alert p99</small><b>{num(alertP99, 0)} ms</b>
          </span>
        )}
        <span className="stat opt"><small>Stale</small><b className={snap?.counts.stale ? "amber" : ""}>{snap?.counts.stale ?? "–"}</b></span>
        <span className="stat opt">
          <small>NIFTY</small>
          <b className={nifty == null ? "" : nifty >= 0 ? "up" : "down"}>{nifty == null ? "–" : pctRaw(nifty)}</b>
        </span>
        {g && (
          <span className="stat" title="Daily guardrails">
            <small>Day P&amp;L · losers · open</small>
            <b>
              <span className={g.realized_pnl >= 0 ? "up" : "down"}>{rupees(g.realized_pnl)}</span> · {g.losers} ·{" "}
              {g.open_positions}/{g.max_open_positions}
              {g.tripped && <span className="chip warn">entries paused</span>}
            </b>
          </span>
        )}
      </div>
      <div className="spacer" />
      <button className={`btn ${soundOn ? "on" : ""}`} onClick={onSound} title="Alert sounds">
        {soundOn ? "🔔 Sound on" : "🔕 Sound off"}
      </button>
      <button className="btn" onClick={onHealth}>Health</button>
      <button className="btn" onClick={onSettings}>Parameters</button>
      <button className="btn" onClick={onTheme} title="Toggle theme">{theme === "light" ? "☾" : "☀"}</button>
    </header>
  );
}
