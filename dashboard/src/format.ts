import type { Check } from "./types";

const IST = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});
const IST_HM = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export const hms = (ts: number | null | undefined) => (ts ? IST.format(new Date(ts * 1000)) : "–");
export const hm = (ts: number | null | undefined) => (ts ? IST_HM.format(new Date(ts * 1000)) : "–");

export function num(x: number | null | undefined, digits = 2): string {
  if (x === null || x === undefined || Number.isNaN(x)) return "–";
  return x.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export const px = (x: number | null | undefined) => num(x, 2);
export const cr = (x: number | null | undefined) => (x === null || x === undefined ? "–" : `₹${num(x, 2)} cr`);
export const crShort = (x: number | null | undefined) => (x === null || x === undefined ? "–" : num(x, 2));
export const pct = (x: number | null | undefined, digits = 0) =>
  x === null || x === undefined ? "–" : `${(x * 100).toFixed(digits)}%`;
export const pctRaw = (x: number | null | undefined, digits = 2) =>
  x === null || x === undefined ? "–" : `${x >= 0 ? "+" : ""}${x.toFixed(digits)}%`;
export const times = (x: number | string | null | undefined) => {
  if (x === null || x === undefined) return "–";
  if (x === "inf") return "∞";
  if (typeof x === "string") return x;
  return `${x.toFixed(1)}x`;
};
export const qty = (x: number | null | undefined) => (x === null || x === undefined ? "–" : x.toLocaleString("en-IN"));
export const rmult = (x: number | null | undefined) =>
  x === null || x === undefined ? "–" : `${x >= 0 ? "+" : ""}${x.toFixed(2)}R`;
export const rupees = (x: number | null | undefined) =>
  x === null || x === undefined ? "–" : `${x < 0 ? "−" : ""}₹${Math.abs(x).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;

export const arrow = (d: number) => (d > 0 ? "▲" : "▼");

export const RULE_LABELS: Record<string, string> = {
  turnover_floor_cr: "Turnover ≥ floor (₹ cr)",
  rvol: "RVOL vs same-minute baseline",
  suddenness: "vs mean of previous candles",
  green_close_in_top: "Close in top of range",
  move_pct: "Move from previous close (%)",
  buy_share: "Estimated buy share",
  scannable: "Instrument scannable",
  session_window: "Inside session window",
  cooldown: "Cooldown elapsed",
  data_quality: "Data quality (no gap/stale)",
  price_band: "Distance to price band (%)",
  not_halted: "Not halted",
  event_day: "Event-day filter",
  cash_confirmation_rollover: "Cash confirmation (rollover)",
  turnover_vs_s1: "Turnover vs S1",
  close_above_trigger: "Close beyond trigger P",
  entry_window: "Before last entry time",
  retracement_min: "Retracement ≥ minimum",
  pullback_candles: "Pullback candles",
  pullback_turnover_ratio: "Pullback turnover / S1",
  above_floor: "No close beyond floor",
  above_open: "Price beyond candle open",
  ltp_above_trigger: "LTP beyond trigger",
};

const RATIO_RULES = new Set(["buy_share", "turnover_vs_s1", "retracement_min", "pullback_turnover_ratio", "green_close_in_top"]);
const TIMES_RULES = new Set(["rvol", "suddenness"]);

export function ruleValue(rule: string, v: Check["value"]): string {
  if (v === null || v === undefined) return "–";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "string") return v === "inf" ? "∞" : v;
  if (RATIO_RULES.has(rule)) return pct(v, 0);
  if (TIMES_RULES.has(rule)) return times(v);
  return num(v, 2);
}


export const REASON_LABELS: Record<string, string> = {
  floor_broken: "floor broken",
  max_retrace: "retracement beyond max",
  distribution: "distribution candle",
  short_buildup: "short build-up (OI up, price down)",
  stale_data: "stale data",
  risk_out_of_range: "risk out of range",
  timeout: "no Surge 2 in time",
  last_entry_time: "past last entry time",
  buy_stop_not_triggered: "buy-stop not triggered",
  halt: "halt",
  circuit: "circuit / price band hit",
  session_end: "session end",
  max_daily_losses: "guardrail: daily losers",
  max_daily_loss: "guardrail: daily loss",
  max_open_positions: "guardrail: open positions",
  event_day_requires_grade_a: "event day needs grade A",
};

export const reasonLabel = (r: string | null | undefined) => (r ? REASON_LABELS[r] ?? r.replace(/_/g, " ") : "–");

export const STATUS_LABELS: Record<string, string> = {
  pending: "pending (buy-stop)",
  open: "open",
  t1: "T1 booked",
  closed: "closed",
  cancelled: "cancelled",
};

export const EXIT_LABELS: Record<string, string> = {
  stop: "stopped",
  breakeven_stop: "stopped at entry",
  trail_stop: "trailing stop",
  t2: "T2",
  t1: "T1",
  time_stop: "time exit",
  eod: "end of day",
  sell_surge: "sell-surge exit",
  session_end: "session end",
};
