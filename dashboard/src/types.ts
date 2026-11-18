export interface Check {
  rule: string;
  value: number | string | boolean | null;
  threshold: number | string | boolean | null;
  pass: boolean;
}

export interface OiReading {
  change_pct: number | null;
  reading: string | null;
  label: string;
}

export interface GradeDetail {
  score: number;
  grade: string;
  base: number;
  components: Record<string, number>;
  penalties: Record<string, number>;
}

export interface Plan {
  direction: number;
  entry: number;
  stop: number;
  R: number;
  risk_pct: number;
  qty: number;
  lots: number | null;
  t1: number;
  t2: number;
  position_value: number;
  entry_mode: string;
  note: string | null;
}

export interface SetupRow {
  id: string;
  token: number;
  symbol: string;
  display: string;
  kind: string;
  direction: number;
  state: string;
  s1_start: number;
  s1_time: string;
  s1_turnover: number;
  s1_rvol: number | null;
  s1_move_pct: number | null;
  s1_buy_share: number;
  s1_ohlc: { open: number; high: number; low: number; close: number };
  H: number;
  L: number | null;
  P: number | null;
  floor: number;
  leg_start: number;
  retracement: number;
  pb_ratio: number;
  pb_candles: number;
  pb_sell_share: number | null;
  distance_to_trigger_pct: number | null;
  last: number | null;
  oi: { s1?: OiReading; pullback?: OiReading; s2?: OiReading };
  cash_fut: { s1?: boolean; s2?: boolean; s1_late?: boolean };
  grade: string | null;
  score: number | null;
  grades: Record<string, GradeDetail>;
  tags: string[];
  expire_at: number;
  minutes_since_s1: number | null;
  minutes_left: number | null;
  armed_ts: number | null;
  entry_ts: number | null;
  end_ts: number | null;
  end_reason: string | null;
  extended: boolean;
  plan: Plan | null;
  s2: Record<string, number | string | boolean | null> | null;
  trade_id: string | null;
  suppressed: string | null;
  checks: Record<string, Check[]>;
  history: { ts: number; from: string; to: string; reason: string }[];
  book?: { total_ratio: number | null; depth5_ratio: number | null };
  basis?: { open_pct?: number; close_pct?: number; change_bp?: number };
}

export interface TradeExit {
  ts: number;
  price: number;
  fraction: number;
  reason: string;
}

export interface TradeRow {
  id: string;
  setup_id: string;
  token: number;
  symbol: string;
  display: string;
  kind: string;
  direction: number;
  status: string;
  entry_ts: number;
  entry: number;
  stop: number;
  initial_stop: number;
  t1: number;
  t2: number;
  qty: number;
  R: number;
  fraction_open: number;
  last: number | null;
  open_r: number | null;
  mfe_r: number | null;
  mae_r: number | null;
  exits: TradeExit[];
  exit_reason: string | null;
  closed_ts: number | null;
  gross_r: number | null;
  net_r: number | null;
  gross_pnl: number | null;
  net_pnl: number | null;
  costs: number | null;
  shadow: boolean;
  suppressed: string | null;
  grade: string | null;
  score: number | null;
  note: string | null;
  cash_fut: boolean;
  tags: string[];
  setup?: {
    s1_time: string;
    s1_turnover: number;
    s1_rvol: number | null;
    retracement: number;
    pb_ratio: number;
    state: string;
    tags: string[];
  };
}

export interface Alert {
  seq: number;
  type: string;
  kind: string;
  reason: string | null;
  ts: number;
  time: string;
  token: number;
  display: string;
  symbol: string;
  direction: number;
  setup_id: string | null;
  grade: string | null;
  score: number | null;
  provisional: boolean;
  text: string;
  sound: string | null;
  muted: boolean;
  latency_ms: number | null;
}

export interface LatencySummary {
  count: number;
  p50: number | null;
  p99: number | null;
  max: number | null;
}

export interface Health {
  mode: string;
  now: number;
  date: string;
  feed: { adapter: string; connected: boolean; reconnects: number; frames: number; ticks: number; subscribed: number };
  ticks_per_second: number;
  tick_to_state_ms: LatencySummary;
  processor: {
    feed_connected: boolean;
    ticks: number;
    stale: string[];
    builder: Record<string, number>;
    tick_latency_ms: LatencySummary;
    close_latency_ms: LatencySummary;
  };
  alerts: {
    counts: Record<string, number>;
    muted: number[];
    latency_confirmed_ms: LatencySummary;
    latency_provisional_ms: LatencySummary;
    telegram: { sent: number; failed: number; pending: number } | null;
  };
  bus: { subscribers: number; published: number; dropped: number };
  config_version: string;
  universe: { instruments: number; scannable: number; banned: string[]; notes: string[] };
  [k: string]: unknown;
}

export interface Snapshot {
  now: number;
  date: string;
  surge1: SetupRow[];
  pullback: SetupRow[];
  entries: TradeRow[];
  archive: (SetupRow | TradeRow)[];
  guardrails: {
    realized_pnl: number;
    losers: number;
    winners: number;
    closed: number;
    open_positions: number;
    max_open_positions: number;
    tripped: string | null;
    daily_loss_pct: number;
  };
  regime: Record<string, number | null>;
  config_version: string;
  counts: { instruments: number; scannable: number; live_setups: number; stale: number };
  health: Health;
  alerts: Alert[];
}

export interface CandlePoint {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  turnover: number;
  baseline: number | null;
  vwap: number | null;
  oi: number | null;
  buy_share: number;
  flags: number;
}

export interface InstrumentDetail {
  token: number;
  symbol: string;
  display: string;
  kind: string;
  underlying: string;
  scannable: boolean;
  scan_block_reason: string | null;
  lot_size: number;
  atr: number | null;
  stale: boolean;
  paused: boolean;
  muted?: boolean;
  candles: CandlePoint[];
  setup_id: string | null;
  trade: TradeRow | null;
  setups_today: string[];
}

export interface EventRecord {
  seq: number;
  ts: number;
  time: string;
  kind: string;
  token: number;
  display: string;
  setup_id: string | null;
  from: string | null;
  to: string | null;
  reason: string | null;
  alert: string | null;
  grade: string | null;
  score: number | null;
  values: Record<string, unknown>;
}

export interface SetupDetail {
  setup: SetupRow;
  instrument: InstrumentDetail;
  trade: TradeRow | null;
  events: EventRecord[];
}

export interface ParamMeta {
  group: string;
  range?: (number | string)[];
  options?: (string | boolean)[];
  unit?: string;
  section?: string;
}

export interface ConfigPayload {
  version: string;
  params: Record<string, number | string | boolean>;
  defaults: Record<string, number | string | boolean>;
  presets: Record<string, Record<string, unknown>>;
  active_preset: string | null;
  symbol_overrides: Record<string, Record<string, unknown>>;
  meta: Record<string, ParamMeta>;
  telegram_enabled: boolean;
}
