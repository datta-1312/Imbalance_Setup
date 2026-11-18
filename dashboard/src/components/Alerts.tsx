import type { Alert } from "../types";
import type { Toast } from "../useScreener";

const TYPE_LABEL: Record<string, string> = {
  S1_PROVISIONAL: "S1 · provisional",
  S1_CONFIRMED: "SURGE 1",
  S1_CANCELLED: "S1 cancelled",
  ARMED: "ARMED",
  S2_PROVISIONAL: "S2 · provisional",
  S2_CANCELLED: "S2 cancelled",
  ENTRY: "ENTRY",
  T1: "T1",
  EXIT: "EXIT",
  EXIT_WARNING: "EXIT WARNING",
  INVALIDATED: "INVALIDATED",
  EXPIRED: "EXPIRED",
  SYSTEM: "SYSTEM",
};

export function AlertFeed({ alerts, onOpen, filter }: { alerts: Alert[]; onOpen: (a: Alert) => void; filter: (a: Alert) => boolean }) {
  const list = alerts.filter(filter).slice(0, 150);
  return (
    <section className="panel feed">
      <div className="panel-head">
        <span className="tag">Live</span>
        <h2>Alerts</h2>
        <span className="count">{list.length}</span>
      </div>
      <div className="feed-list">
        {!list.length && <div className="empty">Alerts appear here as they fire.</div>}
        {list.map((a) => (
          <div key={a.seq} className={`alert ${a.type} ${a.muted || a.grade === "C" ? "muted" : ""}`} onClick={() => onOpen(a)}>
            <div className="meta">
              <span className="type">{TYPE_LABEL[a.type] ?? a.type}</span>
              <span>{a.time}</span>
              {a.latency_ms != null && <span title="exchange time to alert">{Math.round(a.latency_ms)} ms</span>}
              {a.grade && <span className={`grade ${a.grade}`}>{a.grade}</span>}
            </div>
            <pre>{a.text}</pre>
          </div>
        ))}
      </div>
    </section>
  );
}

export function Toasts({ toasts, onOpen, onDismiss }: { toasts: Toast[]; onOpen: (a: Alert) => void; onDismiss: (k: string) => void }) {
  return (
    <div className="toasts" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.key} className={`toast ${t.type}`} onClick={() => { onOpen(t); onDismiss(t.key); }}>
          <div className="meta">
            <b>{TYPE_LABEL[t.type] ?? t.type}</b>
            <span>{t.time}{t.latency_ms != null ? ` · ${Math.round(t.latency_ms)} ms` : ""}</span>
          </div>
          <pre>{t.text}</pre>
        </div>
      ))}
    </div>
  );
}
