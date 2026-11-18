import { Fragment, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { Check, EventRecord, GradeDetail, InstrumentDetail, SetupRow, TradeRow } from "../types";
import {
  EXIT_LABELS, RULE_LABELS, STATUS_LABELS, arrow, cr, hm, hms, num, pct, px, qty, reasonLabel, rmult, rupees,
  ruleValue, times,
} from "../format";
import { PriceChart, type Level, type Mark } from "./Chart";
import { Grade } from "./Tables";

export interface DetailTarget {
  setupId?: string | null;
  token: number;
}

function Checklist({ title, checks }: { title: string; checks?: Check[] }) {
  if (!checks || !checks.length) return null;
  return (
    <section className="panel">
      <div className="panel-head"><span className="tag">Rules</span><h2>{title}</h2>
        <span className="count">{checks.filter((c) => c.pass).length}/{checks.length} pass</span></div>
      <table className="checks">
        <thead><tr><th>Rule</th><th className="v">Value</th><th className="t">Threshold</th><th /></tr></thead>
        <tbody>
          {checks.map((c, i) => (
            <tr key={i}>
              <td>{RULE_LABELS[c.rule] ?? c.rule}</td>
              <td className="v">{ruleValue(c.rule, c.value)}</td>
              <td className="t faint">{ruleValue(c.rule, c.threshold)}</td>
              <td className={c.pass ? "pass" : "fail"}>{c.pass ? "✓" : "✗"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function GradePanel({ grades }: { grades: Record<string, GradeDetail> }) {
  const stages = (["entry", "armed", "s1"] as const).filter((s) => grades?.[s]);
  if (!stages.length) return null;
  const g = grades[stages[0]];
  return (
    <section className="panel">
      <div className="panel-head"><span className="tag">Grade</span><h2>{stages[0] === "s1" ? "Surge 1" : stages[0] === "armed" ? "Armed" : "Entry"} score</h2>
        <span className="count"><Grade g={g.grade} s={g.score} /></span></div>
      {Object.entries(g.components).map(([k, v]) => (
        <div className="comp" key={k}>
          <span className="faint">{k.replace(/_/g, " ")}</span>
          <span className="bar"><i style={{ width: `${v * 100}%`, background: v >= 0.66 ? "var(--up)" : v >= 0.33 ? "var(--amber)" : "var(--down)" }} /></span>
          <span>{Math.round(v * 100)}</span>
        </div>
      ))}
      {Object.entries(g.penalties).map(([k, v]) => (
        <div className="comp" key={k}><span className="amber">− {k.replace(/_/g, " ")}</span><span /><span className="amber">−{v}</span></div>
      ))}
      <div className="comp"><span className="faint">weighted base</span><span /><span>{num(g.base, 0)}</span></div>
    </section>
  );
}

function Timeline({ events }: { events: EventRecord[] }) {
  const rows = events.filter((e) => e.kind !== "status").slice(-40);
  if (!rows.length) return null;
  return (
    <section className="panel">
      <div className="panel-head"><span className="tag">Audit</span><h2>Event log</h2><span className="count">{rows.length}</span></div>
      <ul className="timeline">
        {rows.map((e) => (
          <li key={e.seq}>
            <span className="faint">{e.time}</span>
            <span>
              {e.kind === "transition" ? <><b>{e.from} → {e.to}</b> · {reasonLabel(e.reason)}</> : <><b>{e.kind}</b> · {reasonLabel(e.reason)}</>}
              {e.alert && <span className="chip">{e.alert}</span>}
              {e.grade && <span className={`grade ${e.grade}`} style={{ marginLeft: 6 }}>{e.grade}</span>}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function SetupSummary({ s }: { s: SetupRow }) {
  const long = s.direction > 0;
  return (
    <section className="panel">
      <div className="panel-head"><span className="tag">Setup</span><h2>{s.state} · S1 {s.s1_time}</h2></div>
      <dl className="kv">
        <dt>S1 turnover / RVOL</dt><dd>{cr(s.s1_turnover)} · {times(s.s1_rvol)}</dd>
        <dt>S1 {long ? "buy" : "sell"} share · move</dt><dd>{pct(s.s1_buy_share)} · {s.s1_move_pct == null ? "–" : `${(s.s1_move_pct * s.direction).toFixed(2)}%`}</dd>
        <dt>S1 open → close</dt><dd>{px(s.s1_ohlc?.open)} → {px(s.s1_ohlc?.close)}</dd>
        <dt>S1 range</dt><dd>{px(s.s1_ohlc?.low)} – {px(s.s1_ohlc?.high)}</dd>
        <dt>{long ? "Swing high H" : "Swing low H"} · leg start</dt><dd>{px(s.H)} · {px(s.leg_start)}</dd>
        <dt>Floor ({long ? "S1 low" : "S1 high"})</dt><dd>{px(s.floor)}</dd>
        <dt>Pullback {long ? "low" : "high"} L</dt><dd>{px(s.L)}</dd>
        <dt>Retracement</dt><dd>{pct(s.retracement, 1)}</dd>
        <dt>Pullback candles · vol / S1</dt><dd>{s.pb_candles} · {pct(s.pb_ratio, 0)}</dd>
        <dt>Pullback {long ? "sell" : "buy"} share</dt><dd>{pct(s.pb_sell_share)}</dd>
        <dt>Trigger P</dt><dd>{px(s.P)}</dd>
        <dt>Expires</dt><dd>{hm(s.expire_at)}{s.end_reason ? ` · ended: ${reasonLabel(s.end_reason)}` : ""}</dd>
        {s.oi?.s1 && <><dt>OI at S1</dt><dd>{s.oi.s1.change_pct == null ? "–" : `${s.oi.s1.change_pct.toFixed(2)}%`} {s.oi.s1.label}</dd></>}
        {s.oi?.pullback && <><dt>OI in pullback</dt><dd>{s.oi.pullback.change_pct == null ? "–" : `${s.oi.pullback.change_pct.toFixed(2)}%`} {s.oi.pullback.label}</dd></>}
        {s.oi?.s2 && <><dt>OI at S2</dt><dd>{s.oi.s2.change_pct == null ? "–" : `${s.oi.s2.change_pct.toFixed(2)}%`} {s.oi.s2.label}</dd></>}
        {s.book && (s.book.total_ratio != null || s.book.depth5_ratio != null) && <>
          <dt>Book pressure at S1 (total · 5-level)</dt>
          <dd>{num(s.book.total_ratio, 2)} · {num(s.book.depth5_ratio, 2)}</dd></>}
        {s.basis?.close_pct != null && <>
          <dt>Futures basis at S1</dt>
          <dd>{s.basis.close_pct.toFixed(2)}% ({(s.basis.change_bp ?? 0) >= 0 ? "+" : ""}{(s.basis.change_bp ?? 0).toFixed(1)} bp in the candle)</dd></>}
        <dt>Cash–futures</dt><dd>{s.cash_fut?.s2 ? "confirmed at S2" : s.cash_fut?.s1 ? `confirmed at S1${s.cash_fut.s1_late ? " (next minute)" : ""}` : "no"}</dd>
        {s.tags.length > 0 && <><dt>Tags</dt><dd>{s.tags.join(", ")}</dd></>}
      </dl>
    </section>
  );
}

function TradePanel({ t }: { t: TradeRow }) {
  return (
    <section className="panel">
      <div className="panel-head"><span className="tag">Trade plan</span><h2>{STATUS_LABELS[t.status] ?? t.status}{t.shadow ? " · shadow (suppressed)" : ""}</h2></div>
      <dl className="kv">
        <dt>Entry</dt><dd>{px(t.entry)} at {hms(t.entry_ts)}</dd>
        <dt>Stop (initial)</dt><dd className="down">{px(t.stop)} ({px(t.initial_stop)})</dd>
        <dt>T1 · T2</dt><dd>{px(t.t1)} · {px(t.t2)}</dd>
        <dt>Qty · R per share</dt><dd>{t.qty ? qty(t.qty) : "0"} · {num(t.R, 2)}</dd>
        {t.note && <><dt>Note</dt><dd className="amber">{t.note}</dd></>}
        <dt>Open · MFE · MAE</dt><dd>{rmult(t.open_r)} · {rmult(t.mfe_r)} · {rmult(t.mae_r)}</dd>
        {t.exits.map((x, i) => (
          <Fragment key={i}>
            <dt>Exit {Math.round(x.fraction * 100)}%</dt>
            <dd>{EXIT_LABELS[x.reason] ?? x.reason} at {px(x.price)} · {hms(x.ts)}</dd>
          </Fragment>
        ))}
        {t.status === "closed" && <><dt>Result gross · net</dt><dd className={(t.net_r ?? 0) >= 0 ? "up" : "down"}>{rmult(t.gross_r)} · {rmult(t.net_r)}</dd>
          <dt>Net P&amp;L · costs{t.qty ? "" : " (notional qty)"}</dt><dd>{rupees(t.net_pnl)} · {rupees(t.costs)}</dd></>}
      </dl>
    </section>
  );
}

export function DetailDrawer({ target, onClose, eventTick, theme, muted, onMute, rvolMin }: {
  target: DetailTarget;
  onClose: () => void;
  eventTick: number;
  theme: string;
  muted: boolean;
  onMute: (v: boolean) => void;
  rvolMin: number;
}) {
  const [setup, setSetup] = useState<SetupRow | null>(null);
  const [inst, setInst] = useState<InstrumentDetail | null>(null);
  const [setupTrade, setSetupTrade] = useState<TradeRow | null>(null);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        if (target.setupId) {
          const d = await api.setup(target.setupId);
          if (cancelled) return;
          setSetup(d.setup);
          setSetupTrade(d.trade);
          setInst(d.instrument);
          setEvents(d.events);
          setPaused(d.instrument.paused);
        } else {
          const d = await api.instrument(target.token);
          if (cancelled) return;
          setSetup(null);
          setSetupTrade(null);
          setInst(d);
          setEvents(d.events);
          setPaused(d.paused);
        }
        setError(null);
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    };
    void load();
    const id = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [target.setupId, target.token, eventTick]);

  const trade = setupTrade ?? (inst?.trade && (!setup || inst.trade.setup_id === setup.id) ? inst.trade : null);

  const { levels, marks } = useMemo(() => {
    const L: Level[] = [];
    const M: Mark[] = [];
    if (setup) {
      const long = setup.direction > 0;
      L.push({ price: setup.H, color: "var(--amber)", title: "H", dashed: true });
      L.push({ price: setup.floor, color: cssColor("--down"), title: "floor", dashed: true });
      if (setup.state === "ARMED" || setup.state === "PULLBACK") L.push({ price: setup.P, color: cssColor("--violet"), title: "P" });
      M.push({ t: setup.s1_start, text: "S1", color: cssColor("--amber"), below: long });
      const pb = setup.history.find((h) => h.to === "PULLBACK");
      if (pb) M.push({ t: pb.ts - 61.5, text: "PB", color: cssColor("--sky"), below: !long });
      const armed = setup.history.find((h) => h.to === "ARMED");
      if (armed) M.push({ t: armed.ts - 61.5, text: "armed", color: cssColor("--violet"), below: !long });
      const entry = setup.history.find((h) => h.to === "ENTRY");
      if (entry) M.push({ t: entry.ts - 61.5, text: "S2", color: cssColor("--up"), below: long });
    }
    const t = trade ?? null;
    if (t) {
      L.push({ price: t.entry, color: cssColor("--sky"), title: "entry" });
      L.push({ price: t.stop, color: cssColor("--down"), title: "stop" });
      L.push({ price: t.t1, color: cssColor("--up"), title: "T1", dashed: true });
      L.push({ price: t.t2, color: cssColor("--up"), title: "T2", dashed: true });
      for (const x of t.exits) M.push({ t: x.ts, text: x.reason.toUpperCase(), color: cssColor("--muted"), below: t.direction < 0 });
    }
    // resolve CSS variable strings for the canvas
    return { levels: L.map((l) => ({ ...l, color: l.color.startsWith("var(") ? cssColor(l.color.slice(4, -1)) : l.color })), marks: M };
  }, [setup, trade]);

  const title = inst?.display ?? setup?.display ?? "…";
  const dir = setup?.direction ?? trade?.direction ?? 1;
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-label={`${title} detail`}>
        <div className="drawer-head">
          <span className={dir > 0 ? "up" : "down"}>{arrow(dir)}</span>
          <h3>{title}</h3>
          {setup && <span className={`state ${setup.state}`}>{setup.state}</span>}
          {setup && <Grade g={setup.grade} s={setup.score} />}
          {inst && !inst.scannable && <span className="chip warn">not scanned: {inst.scan_block_reason}</span>}
          {inst?.stale && <span className="chip warn">stale</span>}
          <span className="spacer" />
          <button className={`btn ${muted ? "on" : ""}`} onClick={() => onMute(!muted)}>{muted ? "Muted" : "Mute"}</button>
          <button className={`btn ${paused ? "on" : ""}`} onClick={async () => {
            if (!inst) return;
            try { await api.pause(inst.token, !paused); setPaused(!paused); } catch (e) { setError(String(e)); }
          }}>{paused ? "Paused" : "Pause scanning"}</button>
          <button className="btn" onClick={onClose}>Close ✕</button>
        </div>
        <div className="drawer-body">
          {error && <div className="error">{error}</div>}
          {inst && (
            <section className="panel">
              <PriceChart candles={inst.candles} levels={levels} marks={marks} showOi={inst.kind !== "EQ"} rvolMin={rvolMin} theme={theme} />
              <div className="legend">
                <span><i style={{ background: "var(--violet)" }} />VWAP</span>
                <span><i style={{ background: "var(--amber)" }} />turnover baseline B(m) (dotted) and {rvolMin}× surge line (dashed)</span>
                {inst.kind !== "EQ" && <span><i style={{ background: "var(--sky)" }} />open interest (left axis)</span>}
                <span>bars: turnover per minute, grey = gap-affected</span>
                {inst.atr != null && <span>ATR(14) {num(inst.atr, 2)}</span>}
              </div>
            </section>
          )}
          <div className="grid2">
            {setup && <SetupSummary s={setup} />}
            {trade && <TradePanel t={trade} />}
            {setup && <GradePanel grades={setup.grades} />}
          </div>
          {setup && (
            <div className="grid2 wide">
              <Checklist title="Surge 1 rules" checks={setup.checks.s1} />
              <Checklist title="Arming rules" checks={setup.checks.armed} />
              <Checklist title="Surge 2 rules" checks={setup.checks.s2} />
            </div>
          )}
          {!setup && inst && !trade && <div className="notice">No setup on this instrument right now.</div>}
          <Timeline events={events} />
        </div>
      </aside>
    </>
  );
}

function cssColor(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}
