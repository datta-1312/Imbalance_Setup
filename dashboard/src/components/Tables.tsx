import type { ReactNode } from "react";
import type { SetupRow, TradeRow } from "../types";
import { EXIT_LABELS, STATUS_LABELS, arrow, crShort, hm, num, pct, px, qty, reasonLabel, rmult, times } from "../format";

export function Panel({ title, tag, count, children }: { title: string; tag: string; count?: number; children: ReactNode }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <span className="tag">{tag}</span>
        <h2>{title}</h2>
        {count !== undefined && <span className="count">{count}</span>}
      </div>
      {children}
    </section>
  );
}

export const Grade = ({ g, s }: { g: string | null; s?: number | null }) =>
  g ? <span className={`grade ${g}`} title={s != null ? `score ${s}` : undefined}>{g}{s != null ? ` ${Math.round(s)}` : ""}</span> : <span className="faint">–</span>;

const Sym = ({ display, direction, kind }: { display: string; direction: number; kind: string }) => (
  <span className="sym">
    <span className={`arr ${direction > 0 ? "up" : "down"}`}>{arrow(direction)}</span>
    {display.replace(/ FUT(-NEXT)?$/, "")} <span className="kind">{kind === "EQ" ? "EQ" : "FUT"}</span>
  </span>
);

const oiText = (r: SetupRow) => {
  const o = r.oi?.s1;
  if (!o || o.change_pct == null) return <span className="faint">{r.kind === "EQ" ? "n/a" : "–"}</span>;
  const good = o.reading === "buildup";
  return <span className={good ? "up" : o.reading === "covering" ? "amber" : "faint"} title={o.label}>
    {o.change_pct >= 0 ? "+" : ""}{o.change_pct.toFixed(1)}%
  </span>;
};

const Tags = ({ tags }: { tags: string[] }) => (
  <>{tags.map((t) => <span key={t} className={`chip ${/cover|unwind|block|event|extended|estimated/.test(t) ? "warn" : ""}`}>{t}</span>)}</>
);

interface RowProps<T> { rows: T[]; onOpen: (r: T) => void; muted: Set<number> }

export function Surge1Table({ rows, onOpen, muted }: RowProps<SetupRow>) {
  if (!rows.length) return <div className="empty">No Surge 1 footprints right now.</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>
          <th>Symbol</th><th>Time</th><th>Turnover ₹cr</th><th>RVOL</th><th>Move</th><th>Buy share</th><th className="l">OI</th><th>Grade</th><th>Min</th>
        </tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} onClick={() => onOpen(r)} className={muted.has(r.token) || r.grade === "C" ? "muted" : ""}>
              <td><Sym display={r.display} direction={r.direction} kind={r.kind} /><Tags tags={r.tags.filter((t) => t !== "rollover")} /></td>
              <td>{r.s1_time}</td>
              <td>{crShort(r.s1_turnover)}</td>
              <td>{times(r.s1_rvol)}</td>
              <td className={r.direction > 0 ? "up" : "down"}>{r.s1_move_pct == null ? "–" : `${(r.s1_move_pct * r.direction).toFixed(2)}%`}</td>
              <td>{pct(r.s1_buy_share)}</td>
              <td className="l">{oiText(r)}{r.cash_fut?.s1 && <span className="chip ok" title="cash–futures confirmation">{r.kind === "EQ" ? "fut ✓" : "cash ✓"}</span>}</td>
              <td><Grade g={r.grade} s={r.score} /></td>
              <td>{r.minutes_since_s1 == null ? "–" : Math.floor(r.minutes_since_s1)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function PullbackTable({ rows, onOpen, muted }: RowProps<SetupRow>) {
  if (!rows.length) return <div className="empty">No setups in pullback.</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>
          <th>Symbol</th><th>State</th><th>Retrace</th><th>PB vol / S1</th><th>Trigger P</th><th>To P</th><th>Left</th><th>Grade</th>
        </tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} onClick={() => onOpen(r)} className={muted.has(r.token) || r.grade === "C" ? "muted" : ""}>
              <td><Sym display={r.display} direction={r.direction} kind={r.kind} /></td>
              <td><span className={`state ${r.state}`}>{r.state}</span></td>
              <td title="(H − pullback low) / (H − leg start)">
                <span className="bar"><i style={{ width: `${Math.min(100, (r.retracement / 0.618) * 100)}%` }} /></span> {pct(r.retracement)}
              </td>
              <td className={r.pb_ratio <= 0.35 ? "" : "amber"}>{pct(r.pb_ratio)}</td>
              <td>{px(r.P)}</td>
              <td>{r.distance_to_trigger_pct == null ? "–" : `${(r.distance_to_trigger_pct * r.direction).toFixed(2)}%`}</td>
              <td className={r.minutes_left != null && r.minutes_left < 8 ? "amber" : ""}>{r.minutes_left == null ? "–" : `${Math.max(0, Math.floor(r.minutes_left))}m`}</td>
              <td><Grade g={r.grade} s={r.score} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function tradeStatus(t: TradeRow): { text: string; cls: string } {
  if (t.status === "closed") {
    return { text: EXIT_LABELS[t.exit_reason ?? ""] ?? t.exit_reason ?? "closed", cls: (t.gross_r ?? 0) >= 0 ? "up" : "down" };
  }
  return { text: STATUS_LABELS[t.status] ?? t.status, cls: t.status === "t1" ? "up" : "" };
}

export function EntryTable({ rows, onOpen, muted }: RowProps<TradeRow>) {
  if (!rows.length) return <div className="empty">No Surge 2 entries yet today.</div>;
  return (
    <div className="table-wrap tall">
      <table>
        <thead><tr>
          <th>Symbol</th><th>Time</th><th>Entry</th><th>Stop</th><th>T1</th><th>T2</th><th>Qty</th><th>R</th><th>Grade</th>
          <th className="l">Cash/Fut</th><th className="l">Status</th><th>Result</th>
        </tr></thead>
        <tbody>
          {[...rows].reverse().map((t) => {
            const st = tradeStatus(t);
            const result = t.status === "closed" ? t.net_r : t.open_r;
            return (
              <tr key={t.id} onClick={() => onOpen(t)} className={muted.has(t.token) || t.grade === "C" ? "muted" : ""}>
                <td><Sym display={t.display} direction={t.direction} kind={t.kind} /></td>
                <td>{hm(t.entry_ts)}</td>
                <td>{px(t.entry)}</td>
                <td className="down" title={t.stop !== t.initial_stop ? `current stop ${px(t.stop)}` : undefined}>{px(t.initial_stop)}</td>
                <td>{px(t.t1)}</td>
                <td>{px(t.t2)}</td>
                <td title={t.note ?? undefined}>{t.qty ? qty(t.qty) : <span className="amber">0</span>}</td>
                <td>{num(t.R, 2)}</td>
                <td><Grade g={t.grade} s={t.score} /></td>
                <td className="l">{t.cash_fut ? <span className="chip ok">confirmed</span> : <span className="faint">–</span>}</td>
                <td className="l">
                  <span className={`state ${t.status}`}>{st.text}</span>
                  {t.status !== "closed" && t.stop !== t.initial_stop && <span className="chip" title="trailing / breakeven stop">stop {px(t.stop)}</span>}
                </td>
                <td className={result == null ? "" : result >= 0 ? "up" : "down"} title={t.status === "closed" ? "net of costs" : "open"}>
                  {rmult(result)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const isTrade = (r: SetupRow | TradeRow): r is TradeRow => "entry" in r && "status" in r;

export function ArchiveTable({ rows, onOpen }: { rows: (SetupRow | TradeRow)[]; onOpen: (r: SetupRow | TradeRow) => void }) {
  if (!rows.length) return <div className="empty">Nothing invalidated or expired yet.</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Symbol</th><th>S1</th><th>Ended</th><th className="l">Outcome</th><th className="l">Reason</th><th>Grade</th></tr></thead>
        <tbody>
          {[...rows].reverse().map((r) =>
            isTrade(r) ? (
              <tr key={r.id} onClick={() => onOpen(r)} className="muted">
                <td><Sym display={r.display} direction={r.direction} kind={r.kind} /></td>
                <td>{r.setup?.s1_time ?? "–"}</td>
                <td>{hm(r.entry_ts)}</td>
                <td className="l"><span className="state closed">entry suppressed</span></td>
                <td className="l">{reasonLabel(r.suppressed)}</td>
                <td><Grade g={r.grade} s={r.score} /></td>
              </tr>
            ) : (
              <tr key={r.id} onClick={() => onOpen(r)}>
                <td><Sym display={r.display} direction={r.direction} kind={r.kind} /></td>
                <td>{r.s1_time}</td>
                <td>{hm(r.end_ts)}</td>
                <td className="l"><span className={`state ${r.state}`}>{r.state}</span></td>
                <td className="l">{reasonLabel(r.end_reason)}</td>
                <td><Grade g={r.grade} s={r.score} /></td>
              </tr>
            ),
          )}
        </tbody>
      </table>
    </div>
  );
}
