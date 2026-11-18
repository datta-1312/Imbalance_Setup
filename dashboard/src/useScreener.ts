import { useCallback, useEffect, useRef, useState } from "react";
import type { Alert, Snapshot } from "./types";
import { play } from "./sound";

export interface Toast extends Alert {
  key: string;
  expires: number;
}

type ConnState = "connecting" | "open" | "closed";

const TOAST_MS: Record<string, number> = { ENTRY: 15000, EXIT_WARNING: 12000, S2_PROVISIONAL: 10000 };
const TOAST_TYPES = new Set([
  "S1_PROVISIONAL", "S1_CONFIRMED", "S1_CANCELLED", "ARMED", "S2_PROVISIONAL", "S2_CANCELLED", "ENTRY", "T1",
  "EXIT", "EXIT_WARNING", "SYSTEM",
]);

export function useScreener(soundOn: boolean) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [conn, setConn] = useState<ConnState>("connecting");
  const [status, setStatus] = useState<{ status: string; message: string | null } | null>(null);
  const [eventTick, setEventTick] = useState(0);
  const seen = useRef(new Set<number>());
  const soundRef = useRef(soundOn);
  soundRef.current = soundOn;

  const addAlerts = useCallback((list: Alert[], fresh: boolean) => {
    const add: Alert[] = [];
    for (const a of list) {
      if (seen.current.has(a.seq)) continue;
      seen.current.add(a.seq);
      add.push(a);
    }
    if (!add.length) return;
    setAlerts((prev) => [...add.reverse(), ...prev].slice(0, 400));
    if (!fresh) return;
    const now = Date.now();
    const newToasts = add
      .filter((a) => TOAST_TYPES.has(a.type) && !a.muted && !(a.grade === "C" && a.type !== "ENTRY"))
      .map((a) => ({ ...a, key: `${a.seq}`, expires: now + (TOAST_MS[a.type] ?? 7000) }));
    if (newToasts.length) setToasts((prev) => [...newToasts, ...prev].slice(0, 6));
    if (soundRef.current) {
      const loud = add.find((a) => a.sound && !a.muted);
      if (loud) play(loud.sound);
    }
  }, []);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let stopped = false;
    let retry = 500;
    let first = true;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      setConn("connecting");
      ws.onopen = () => {
        setConn("open");
        retry = 500;
      };
      ws.onmessage = (ev) => {
        let msg: { topic: string; data: unknown };
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg.topic === "snapshot") {
          const snap = msg.data as Snapshot;
          setSnapshot(snap);
          setStatus(null);
          addAlerts(snap.alerts ?? [], !first);
          first = false;
        } else if (msg.topic === "alert") {
          addAlerts([msg.data as Alert], true);
        } else if (msg.topic === "events") {
          setEventTick((n) => n + 1);
        } else if (msg.topic === "status") {
          setStatus(msg.data as { status: string; message: string | null });
        }
      };
      ws.onclose = () => {
        setConn("closed");
        if (!stopped) {
          setTimeout(connect, retry);
          retry = Math.min(retry * 2, 8000);
        }
      };
    };
    connect();
    return () => {
      stopped = true;
      ws?.close();
    };
  }, [addAlerts]);

  useEffect(() => {
    const id = setInterval(() => {
      const now = Date.now();
      setToasts((prev) => (prev.some((t) => t.expires < now) ? prev.filter((t) => t.expires >= now) : prev));
    }, 1000);
    return () => clearInterval(id);
  }, []);

  const dismiss = useCallback((key: string) => setToasts((prev) => prev.filter((t) => t.key !== key)), []);

  return { snapshot, alerts, toasts, conn, status, dismiss, eventTick };
}
