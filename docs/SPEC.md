# Imbalance Setup Screener — Build Prompt & Specification

Nov 18, 2026 · @Mr.D

## How to use this document

This whole document is the build prompt: give it to an AI coding tool or a developer together with the kickoff prompt in section 14. Sections 2–7 define the trading logic exactly; sections 8–13 define the software around it.

- Every number marked **default** is a starting value to be tuned by backtesting (section 11), not a proven setting.
- Assumption: "4–5 cr" means **traded value of ₹4–5 crore inside one 1-minute candle** (price × quantity), not 4–5 crore shares. The software keeps it configurable.
- Market: NSE cash shares of F&O stocks and their stock futures. Exchange timings and rules quoted here are as of the date above and must be loaded from configuration, because they changed several times in 2025–26.

## 1. Role and objective

You are building a real-time screener that finds the **imbalance setup** in NSE F&O stocks and their stock futures: a first institution-sized volume surge, a quiet pullback, then a second surge that is the entry signal.

The product must:

1. Ingest live tick data for every instrument in the universe and build 1-minute candles on exchange time.
2. Track each instrument through a state machine (section 5) and raise an alert at each stage: Surge 1 watchlist, Pullback armed, Surge 2 entry.
3. Fire a **provisional** alert the moment a forming candle crosses a surge threshold, and a **confirmed** alert when that candle closes.
4. Attach entry, stop, targets and position size to every entry alert (section 7).
5. Log every number behind every alert so the setup can be backtested and audited (section 11).

Version 1 is **alerts only** and places no orders. Automatic order placement is an optional later module with its own compliance rules (section 12).

Performance targets: tick-to-state-update under 50 ms at p99; confirmed alerts within 1 second of candle close; no instrument silently dropped from the scan.

## 2. The imbalance setup in plain words

The setup bets that a large buyer who could not finish buying in one burst will come back for the rest after a pullback, and that the second burst is the tradeable moment.

1. **Surge 1 (the footprint).** One 1-minute candle trades far more value than normal for that stock, about ₹4–5 crore or more, and price jumps. Reading: a large buyer swept the available offers.
2. **Stall.** Offers run out at higher prices, so the buyer stops lifting the price rather than overpay.
3. **Pullback (absorption).** Short-term holders book profit and price drifts down on light volume. The large buyer waits and bids lower for the remaining quantity.
4. **Surge 2 (the entry).** Once enough supply appears at lower prices, the buyer returns. A second high-value candle pushes price back up through the pullback's local high. The trader enters here and rides the move.

### What this spec adds to the original idea

| Gap in the original idea | Why it matters | Rule added |
| --- | --- | --- |
| A fixed ₹ threshold means different things per stock | ₹5 cr a minute is routine in the largest stocks and extreme in thinner ones | Absolute floor **and** relative volume against the stock's own same-minute normal (§3) |
| Volume does not show direction | A spike can be a large seller, not a buyer | Candle shape plus estimated buy/sell split of the volume (§3, §5) |
| "Sudden" is not defined | A slow ramp is not an imbalance | Surge must also dwarf the previous 10 candles (§5) |
| Pullback quality is not defined | A deep or heavy-volume pullback means distribution, not absorption | Retracement band, volume dry-up, structural floor (§5) |
| No invalidation or time limit | Without them, stale setups stay "alive" all day | Invalidation and expiry rules (§5) |
| Futures positioning is ignored | Price up with open interest down is short covering, not fresh buying | Open-interest reading and cash–futures confirmation (§6) |
| Session mechanics create fake spikes | Opening minutes, closing auction, block-deal window, expiry and results days all distort volume | Session and event filters (§4) |
| No risk plan | An entry without stop and size is not a system | Stop, targets, sizing, daily limits (§7) |
| Waiting for candle close is slow | "Real time" needs intra-minute detection | Provisional alert mid-candle, confirmed alert at close (§3, §5) |
| No proof the edge exists | The institutional story is a hypothesis | Logging, backtest and a paper-trading gate before real money (§11) |
| Only the buy side is covered | Large sellers leave the same footprint in reverse | Optional mirrored short setup, off by default (§5) |

## 3. Core definitions and normalisation

Every rule works on **rupee turnover per 1-minute candle**, compared both with a fixed floor and with that stock's own normal turnover for the same minute of the day.

### Turnover of a candle (T)

Broker websockets send snapshots, not every trade, so never add up "last traded quantity". Use the cumulative fields instead. Cumulative turnover equals average traded price (ATP) times cumulative volume, so a candle's turnover is the difference across its boundaries:

```latex
T_{candle} = \frac{ATP_{end} \cdot V_{end} - ATP_{start} \cdot V_{start}}{10^{7}} \quad (\text{₹ crore})
```

- V = "volume traded today" from the feed; ATP = average traded price for the day.
- ATP is rounded to paise. If the difference comes out negative or implausible, fall back to ΔV × mid-price for that interval and flag the candle.
- Futures: turnover is notional, quantity × price. Check whether the feed reports futures volume in units or in lots, and convert to units.

### Normal turnover and relative volume (RVOL)

- **Same-minute baseline B(m):** median turnover of minute-of-day *m* over the last 20 sessions (default), smoothed over m±2 minutes. The median resists one-off spikes.
- **RVOL = T / B(m).** A ₹5 cr candle in a stock whose normal is ₹4 cr has RVOL 1.25 (nothing); in a stock whose normal is ₹60 lakh it has RVOL 8.3 (a footprint).
- **Suddenness:** T divided by the mean turnover of the previous 10 candles today.
- **ADTV:** 20-day average daily turnover, used for the liquidity floor.

### Direction of the volume

- **Buy/sell split (estimated):** classify each volume increment ΔV between snapshots. If LTP ≥ previous best ask → buy. If LTP ≤ previous best bid → sell. Otherwise use the tick rule: price up → buy, down → sell, unchanged → same side as last. **Buy share** = buy volume / candle volume.
- **Book pressure (supporting only):** total buy quantity / total sell quantity, and 5-level bid quantity / ask quantity. Shown in the UI, lightly weighted in the grade.
- Snapshot feeds blur the split. A tick-by-tick data vendor improves it; the adapter interface (section 8) must allow swapping one in.

### Price references

- **VWAP:** the day's ATP from the feed.
- **ATR(14) on 1-minute candles:** used for stop buffers and "extended" checks.
- **Candle:** OHLC from LTP, aligned to exchange timestamps \[hh:mm:00, hh:mm:59.999\]. Close the candle at the boundary plus a 1.5-second grace period for late snapshots.

### Provisional and confirmed events

- **Provisional:** the running turnover of the forming candle crosses the surge thresholds and price is above the candle's open. Alert immediately, marked provisional.
- **Confirmed:** the candle closes and every surge rule still holds. If not, cancel the provisional alert with a reason.

## 4. Universe and market-session rules

Scan every NSE stock that has live derivatives, in two instruments each (the cash share and the current-month future), and allow new signals only in continuous trading from 09:20 to 15:00 by default.

### Universe

- Before 09:00 each day, download the broker's instrument master and build the list of F&O underlyings with their cash (EQ) token, current-month future token, lot size and price band. Do not hard-code the list; it changes.
- In the last 5 sessions before monthly expiry, also subscribe to the next-month future, because volume migrates during rollover.
- Cash and futures are separate instruments linked by underlying, so the engine can check cash–futures confirmation (section 6).
- **F&O ban list:** fetch it daily. Futures of a stock in ban are not scanned, because fresh positions are not allowed; the cash share is still scanned. Since December 2025 the ban is triggered on delta-based future-equivalent open interest crossing 95% of the market-wide position limit ([Zerodha](https://zerodha.com/z-connect/updates/understanding-the-new-delta-oi-based-mwpl-framework), [5paisa](https://www.5paisa.com/blog/sebis-fo-ban-rules-put-delta-based-trading-restrictions-under-the-spotlight)).
- Skip or flag any instrument within 1% of its price band or circuit limit.
- Liquidity floor: 20-day ADTV ≥ ₹25 cr (default).
- NIFTY and BANKNIFTY futures are subscribed for a market-regime reading only, never as setups.

### Session rules

Since 3 August 2026, F&O stocks have a cash-market closing auction from 15:15 to 15:35 and stock futures trade until 15:40 ([Anand Rathi](https://anandrathi.com/blog/nse-extends-trading-hours), [Rupeezy](https://rupeezy.in/share-market-holiday/nse)). Futures also have a pre-open session since December 2025, revised again on 7 September 2026 ([Angel One](https://www.angelone.in/announcements/market/pre-open-session-introduced-in-equity-derivatives-fo), [JM Financial](https://www.jmfinancialservices.in/blogs-and-articles/nse-revises-pre-open-session-framework-for-equity-derivatives-effective-date-7-september-2026)). Load all times from configuration.

| Time (IST) | Session | Screener behaviour |
| --- | --- | --- |
| 08:45–09:00 | Block-deal window 1 | No scanning |
| 09:00–09:15 | Pre-open (cash and current-month futures) | No signals; the opening auction trade is excluded from the first candle's turnover |
| 09:15–09:20 | First minutes of continuous trading | Build candles; no Surge 1 by default (configurable) |
| 09:20–14:05 | Continuous trading | Full scanning |
| 14:05–14:20 | Block-deal window 2 | Scan, but tag any surge "block window" and lower its grade; check whether your feed's volume includes block trades |
| 14:20–15:00 | Continuous trading | Full scanning; 15:00 is the last new Surge 1 |
| 15:00–15:15 | Late continuous trading | Manage live setups; Surge 2 entries allowed until 15:05 |
| 15:15–15:35 | Cash closing auction | Stop the cash scan; auction volume excluded |
| 15:15–15:40 | Futures still trading | No new signals; exit alerts only |

### Event filters

- **Results, board-meeting and ex-dates:** tag the instrument "event day". Option to suppress, or require grade A.
- **Expiry and rollover days:** futures volume is inflated by rolling positions. Require cash confirmation for futures setups on these days.
- **Index rebalancing and F&O inclusion/exclusion dates:** tag; large passive flows are not the discretionary buyer this setup looks for.
- **Halts, holidays and special sessions** (for example Muhurat trading): read from the exchange calendar.

## 5. Detection logic (state machine)

Each instrument moves IDLE → SURGE\_1 → PULLBACK → ARMED → ENTRY, and drops to INVALIDATED or EXPIRED the moment any rule fails. Only one setup per instrument is live at a time.

(State machine diagram: see docs/DESIGN.md §1.)

The left column is the only path to an entry; any rule failure in SURGE\_1, PULLBACK or ARMED ends the setup, and the instrument rests 15 minutes before it can start again.

### 5.1 Surge 1 (IDLE → SURGE\_1)

All rules must hold on the surge candle S1. The surge may be one candle, or two consecutive candles judged together (`surge_window_candles`, default 1).

| # | Rule | Default |
| --- | --- | --- |
| 1 | Turnover T ≥ absolute floor | ₹4.0 cr |
| 2 | RVOL ≥ | 5× |
| 3 | Suddenness: T ≥ multiple of the mean of the previous 10 candles | 4× |
| 4 | Green candle, close in the top part of its range | top 30% |
| 5 | Price change from previous close ≥ | 0.3% |
| 6 | Estimated buy share ≥ | 60% |
| 7 | Inside the allowed session window (§4) and not near a price band | — |

Scored, not required: close above VWAP; new intraday high or break of the opening-range high; NIFTY not down more than 0.5% on the day.

**Record on entry to SURGE\_1:** S1 open, high, low, close, turnover; leg start = lowest low of the 5 candles before S1; swing high H = S1 high. While price keeps rising after S1, update H to the highest high reached.

**Continuation:** if another candle meets all Surge 1 rules before the pullback starts, treat it as part of the same leg (update H and S1 turnover to the larger), not as a new setup.

### 5.2 Pullback (SURGE\_1 → PULLBACK → ARMED)

The pullback starts when a candle makes a lower high than H and closes below the previous candle's close.

While in PULLBACK, track:

- **Retracement r** = (H − lowest low since H) / (H − leg start).
- **Pullback turnover ratio** = mean turnover of pullback candles / S1 turnover.
- **Trigger level P** = highest high of the last 3 completed candles (`trigger_lookback_candles`). P falls as the pullback makes lower highs.
- **Pullback low L** = lowest low since H.

Move to **ARMED** when all hold:

| Rule | Default |
| --- | --- |
| Retracement r ≥ minimum ("sufficient selling has happened") | 25% |
| Pullback has lasted at least | 3 candles |
| Pullback turnover ratio ≤ (volume has dried up) | 35% |
| No candle has closed below the floor | S1 low |

Scored: price holding above VWAP; pullback sell share not extreme (≤ 65%); futures OI not falling sharply (§6).

### 5.3 Surge 2 (ARMED → ENTRY)

All rules must hold on candle S2.

| # | Rule | Default |
| --- | --- | --- |
| 1 | Turnover ≥ share of S1 turnover | 60% |
| 2 | Turnover ≥ absolute floor | ₹3.0 cr |
| 3 | RVOL ≥ | 3× |
| 4 | Green candle, close in the top part of its range | top 30% |
| 5 | Close above trigger level P | — |
| 6 | Estimated buy share ≥ | 60% |
| 7 | Before the last entry time | 15:05 |

- **Entry price:** S2 close (default). Alternative mode: buy-stop one tick above S2 high, valid for the next 2 candles.
- **Extended flag:** if S2 closes more than 0.5 × ATR above H, mark "extended" and lower the grade; the room to run is smaller.
- **Provisional S2 alert:** as soon as the forming candle's running turnover meets rules 1–3 and LTP is above P.

### 5.4 Invalidation (any live state → INVALIDATED)

- A candle closes below the floor (S1 low by default; S1 body midpoint in strict mode).
- Retracement exceeds the maximum (default 61.8%).
- **Distribution candle:** a red pullback candle with turnover ≥ 60% of S1 and sell share ≥ 60%. This is the large player selling, not retail profit booking.
- In futures: price falling with OI rising on high turnover (fresh shorts, §6).
- Halt, circuit hit or stale data (no snapshot for 30 seconds on an active setup).

### 5.5 Expiry and cooldown

- **EXPIRED** if no Surge 2 arrives within 45 minutes of S1 (default) or by the last entry time.
- After INVALIDATED or EXPIRED, the instrument cools down for 15 minutes before a new Surge 1 can start.
- Every exit from a state is logged with its reason code and the values that caused it.

### 5.6 Grade

Each confirmed alert gets a score from 0 to 100 built from configurable weights: RVOL size, buy share, VWAP position, OI reading, cash–futures confirmation, pullback dry-up quality, market regime, and penalties (block window, event day, extended, short covering). Grade A ≥ 75, B 60–74, C below 60 (shown muted).

### 5.7 Mirrored short setup (optional, off by default)

The same rules inverted: a red Surge 1 with high sell share, a bounce on light volume, then a red Surge 2 closing below the bounce's lowest low of the last 3 candles. For cash shares this is intraday-only shorting; futures can be shorted normally.

## 6. Futures rules: open interest and cash–futures confirmation

In futures, price up with open interest (OI) up means fresh longs and confirms the setup; price up with OI down is short covering and lowers the grade without blocking the alert.

| Phase | Price | OI | Reading | Effect |
| --- | --- | --- | --- | --- |
| Surge 1 or Surge 2 | Up | Up | Long build-up | Grade up |
| Surge 1 or Surge 2 | Up | Down | Short covering | Grade down, tag "short covering" |
| Pullback | Down | Down, mild | Long unwinding (profit booking) | Acceptable; this is the expected retail exit |
| Pullback | Down | Down, heavy | Large longs exiting | Warning; grade down |
| Pullback | Down | Up, on high turnover | Short build-up | Invalidate |

- **OI change threshold:** ΔOI ≥ 0.5% of OI (default) counts as up or down; smaller is "flat".
- OI updates arrive less often than price. Measure ΔOI over a 3-minute window around the surge candle, and verify the update frequency of your feed.
- **Cash–futures confirmation:** a surge in both the cash share and the future of the same underlying within the same or adjacent minute adds to the grade. A futures-only surge in rollover week needs cash confirmation (§4).
- **Basis:** a widening futures premium during Surge 1 supports aggressive buying; record it for later analysis, not in v1 rules.
- **Rollover week:** compute futures turnover as near-month plus next-month combined, or rely on the cash signal.

## 7. Risk management and exits

Every entry alert carries its stop, size and targets, so the trader sees a complete trade plan rather than a bare buy signal.

- **Stop:** pullback low L minus a buffer of 0.25 × ATR (default). Wide mode: below S1 low.
- **Risk per share R** = entry − stop. Skip the signal (log it as "risk out of range") if R is below 0.15% or above 1.2% of price (defaults).
- **Position size:** quantity = (capital × risk per trade) / R, with risk per trade 0.5% of capital by default. Round down to whole lots for futures; cap by a maximum position value.
- **Targets:** T1 = 1.5R, book half. T2 = 3R, or trail the rest.
- **Trailing:** after T1, move the stop to entry, then trail below the low of the last completed 3-minute candle or below VWAP, whichever is higher.
- **Time stop:** exit if price makes no new high within 10 candles of entry.
- **Exit warning:** a sell-side surge after entry (red, turnover ≥ 60% of S1, sell share ≥ 60%) raises an immediate exit alert.
- **Daily guardrails:** after 3 losing trades or a 1.5% daily loss, stop raising new entry alerts (keep logging them). Maximum 3 open positions at once.
- **End of day:** exit alerts for cash positions by 15:10, before the closing auction, and for futures by 15:25 (configurable; brokers also apply their own auto square-off times).

## 8. System architecture and data pipeline

One event-driven pipeline runs feed → candle builder → setup engine → alerts and storage, and the same setup engine runs unchanged in live mode and in backtest replay.

(Architecture diagram: see docs/DESIGN.md §4.)

Live snapshots flow left to right into alerts; the dashed path sends stored data back through the same adapter interface, so backtests run the exact code that trades live.

### Components

1. **Market-data adapter (pluggable).** One interface, several implementations: Zerodha Kite Connect first, then Upstox, Angel One SmartAPI, Fyers, Dhan or a tick-by-tick vendor, plus a replay adapter that reads recorded ticks. Kite Connect allows up to 3,000 instruments per websocket and 3 connections per API key; its full mode carries LTP, last traded quantity, ATP, day volume, total buy and sell quantity, OI with day high and low, exchange timestamp and 5-level depth ([Kite docs](https://kite.trade/docs/connect/v3/websocket/)). The universe of about two instruments per F&O stock fits in one connection.
   - Interface: `subscribe(tokens)`, `on_tick(Tick)`, `on_disconnect`, automatic reconnect with resubscribe, heartbeat.
   - `Tick` = token, exchange timestamp, LTP, last qty, ATP, cumulative volume, OI, bid\[5\], ask\[5\], total buy qty, total sell qty.
2. **Instrument and calendar service.** Daily instrument master, F&O list, token map, lot sizes, price bands, ban list, holidays, session timings and event calendar (results, ex-dates, expiry, rebalancing).
3. **Candle builder.** Per instrument, 1-minute candles on exchange time with turnover (§3), buy/sell split, OI open and close, gap flag. Emits `candle_update` on every snapshot and `candle_closed` at each boundary.
4. **Baseline engine.** A pre-market job pulls 20 sessions of 1-minute history and builds the same-minute median turnover profile, ADTV and ATR seeds for every instrument. Intraday it keeps the rolling 10-candle mean.
5. **Setup engine.** The state machine of §5–§7 as pure, deterministic functions over the candle stream, one state object per instrument.
6. **Scorer.** Computes the grade (§5.6) from configurable weights.
7. **Alert dispatcher.** Pushes to the UI websocket, plays sounds, sends Telegram messages; de-duplicates and rate-limits.
8. **Storage.** Raw snapshots as compressed daily Parquet files; candles in TimescaleDB or PostgreSQL; an append-only event log of every state transition with all input values.
9. **Replay and backtest runner.** Feeds stored ticks or historical candles through the same setup engine (§11).
10. **Execution module (later, optional).** Off in v1; see §12 before building it.

### Suggested stack

Python 3.12 with asyncio for v1 (a few hundred instruments at about one snapshot per second each is a light load), FastAPI with WebSockets for the backend, React for the dashboard, TimescaleDB, Redis for pub/sub between processes, Docker Compose for deployment on a small cloud VM in Mumbai.

### Reliability rules

- Broker access tokens expire daily: build a login step into the 08:30 start-up routine.
- Use exchange timestamps for candles, never local receive time; keep the server clock NTP-synced and log receive latency.
- If an active instrument sends no snapshot for 30 seconds, mark it stale and fire no signals on it until fresh data arrives.
- After a reconnect, cumulative volume keeps total turnover correct, but the candles spanning the gap are flagged "gap-affected" and cannot trigger a surge.
- Every component writes structured logs; a health panel shows feed status, ticks per second, stale instruments and alert latency.

## 9. Screener UI and alerts

One live screen shows three lists that match the setup stages, plus a detail panel that explains why each alert fired.

| Panel | Columns |
| --- | --- |
| **A. Surge 1 watchlist** | Symbol, EQ/FUT, time, turnover (₹ cr), RVOL, % move, buy share, OI reading, grade, minutes since surge |
| **B. Pullback / armed** | Retracement %, pullback turnover vs S1 %, trigger level P and distance to it, minutes left before expiry, state |
| **C. Entry signals** | Entry, stop, T1, T2, quantity, R, grade, cash–futures confirmation, live status (open / T1 / stopped / time exit) |
| **D. Archive** | Today's invalidated and expired setups with reason codes |

- **Detail view:** 1-minute chart with S1, the pullback and S2 marked; turnover bars with the same-minute baseline drawn as a band; VWAP; OI line for futures; a rule checklist showing each rule's value and pass or fail.
- **Alerts:** on-screen toast and sound, with distinct sounds for provisional and confirmed Surge 2; Telegram message; optional mobile push. Each alert shows exchange time and measured latency.
- **Controls:** parameter editor with saved presets, pause or mute per symbol, EQ-only / FUT-only filter, minimum grade filter.

Example Telegram entry alert (illustrative numbers):

```
ENTRY ▲ TATASTEEL FUT 10:42:00 | Grade A (82)
S1 10:21 ₹6.3 cr, RVOL 7.1x → pullback 41% on 22% of S1 volume
S2 ₹4.9 cr, close 162.35 > trigger 162.10 | OI +1.2% long build-up | cash confirms
Entry 162.35 | Stop 161.40 | T1 163.78 | T2 165.20 | Qty 5,500
```

## 10. Configurable parameters

All thresholds live in one YAML file, reload without a restart, and accept per-symbol overrides and named presets; the defaults below are starting points for backtesting.

| Parameter | Default | Range to test | Section |
| --- | --- | --- | --- |
| `surge_turnover_floor_cr` | 4.0 | 3–6 | 5.1 |
| `surge_rvol_min` | 5 | 3–10 | 5.1 |
| `suddenness_multiple` (vs previous 10 candles) | 4 | 3–6 | 5.1 |
| `surge_window_candles` | 1 | 1–2 | 5.1 |
| `surge_min_move_pct` | 0.3 | 0.2–0.8 | 5.1 |
| `close_in_top_pct` | 30 | 20–40 | 5.1, 5.3 |
| `buy_share_min` | 0.60 | 0.55–0.70 | 5.1, 5.3 |
| `baseline_sessions` | 20 | 10–30 | 3 |
| `pullback_min_retrace` | 0.25 | 0.20–0.40 | 5.2 |
| `pullback_max_retrace` | 0.618 | 0.50–0.786 | 5.4 |
| `pullback_floor` | S1 low | S1 low / S1 midpoint | 5.2, 5.4 |
| `pullback_max_turnover_ratio` | 0.35 | 0.25–0.50 | 5.2 |
| `pullback_min_candles` | 3 | 2–5 | 5.2 |
| `distribution_turnover_ratio` | 0.60 | 0.50–0.80 | 5.4 |
| `trigger_lookback_candles` | 3 | 2–5 | 5.2 |
| `s2_turnover_vs_s1` | 0.60 | 0.40–1.00 | 5.3 |
| `s2_floor_cr` | 3.0 | 2–5 | 5.3 |
| `s2_rvol_min` | 3 | 2–6 | 5.3 |
| `setup_expiry_minutes` | 45 | 20–90 | 5.5 |
| `cooldown_minutes` | 15 | 5–30 | 5.5 |
| `oi_change_min_pct` | 0.5 | 0.2–1.0 | 6 |
| `no_signal_before` | 09:20 | 09:15–09:30 | 4 |
| `last_surge1_time` | 15:00 | 14:30–15:05 | 4 |
| `last_entry_time` | 15:05 | 14:45–15:10 | 4 |
| `adtv_floor_cr` | 25 | 10–100 | 4 |
| `stop_buffer_atr` | 0.25 | 0.10–0.50 | 7 |
| `min_risk_pct` / `max_risk_pct` | 0.15 / 1.2 | 0.1–0.3 / 0.8–2.0 | 7 |
| `risk_per_trade_pct` | 0.5 | 0.25–1.0 | 7 |
| `t1_r` / `t2_r` | 1.5 / 3 | 1–2 / 2–4 | 7 |
| `time_stop_candles` | 10 | 5–20 | 7 |
| `max_daily_loss_pct` | 1.5 | 1–3 | 7 |
| `max_open_positions` | 3 | 1–5 | 7 |
| `grade_a_min` / `grade_b_min` | 75 / 60 | — | 5.6 |
| `short_setup_enabled` | false | — | 5.7 |

## 11. Backtesting, paper trading and logging

No signal is traded with real money until the rules show positive expectancy after costs in a backtest and then in at least 4 weeks of paper trading.

### Data

- **Historical 1-minute candles** for the universe, 12 months or more, from the broker's historical API. These support candle-only rules; the buy/sell split and intra-minute OI are not available, so the backtest version uses candle shape alone for direction.
- **Recorded live snapshots** from day one, replayed through the replay adapter for full-fidelity tests of every rule.

### Method

- The backtest calls the same setup engine as live mode; replaying one recorded day twice must produce an identical event log.
- No look-ahead: baselines use only earlier sessions; universe, lot sizes and ban list are taken as of each date (stocks enter and leave F&O).
- Fills at candle close plus a slippage model (default 0.05% per side for cash, one tick plus 0.03% for futures), plus brokerage, STT, exchange charges, GST and stamp duty from a configurable cost table.
- Tune parameters walk-forward (for example, fit on 6 months, test on the next 2, roll forward) and report how sensitive results are to each parameter.

### Metrics to report

- Funnel: Surge 1 count → armed → Surge 2 → winners.
- Win rate, average R, expectancy in R per trade, profit factor, maximum drawdown, maximum adverse and favourable excursion.
- All of the above split by grade, time of day, EQ vs FUT, OI reading, event day and market regime.
- **The core test of the idea:** compare entering at Surge 2 with entering at Surge 1, and with random entries at the same times of day. If waiting for Surge 2 does not beat these, the imbalance story is not adding value.

### Paper-trading gate

Run live alerts with simulated fills for at least 4 weeks and 50 entry signals. Promote to real trading only if expectancy stays positive after costs and live results are within the backtest's range.

### Logging

Every state transition writes one record: instrument, exchange time, from-state, to-state, reason code, and every input value used (turnover, RVOL, buy share, retracement, OI change, grade components). This log is the audit trail and the training data for any later improvement.

## 12. Compliance, limitations and disclaimers

Version 1 only raises alerts and sends no orders. Adding automatic orders brings in SEBI's retail algo framework, mandatory for all brokers since 1 April 2026.

### If an execution module is added later

- Orders must go through a broker API from a static IP the broker has whitelisted. Orders from other IPs are rejected ([Fyers](https://support.fyers.in/portal/en/kb/articles/what-are-the-new-sebi-rules-for-retail-algo-trading-from-april-01-2026)).
- Algo orders carry an exchange-issued Algo ID and need a daily two-factor login. Below 10 orders per second per exchange, a strategy generally needs no separate exchange registration ([Alice Blue](https://aliceblueonline.com/blog/sebi-algo-trading-rules-for-retail-traders-what-changed-from-april-2026)). This screener would stay far below that.
- Build a kill switch, order-rate limiter and full order audit trail into that module. Confirm the current rules with your broker before going live.

### Data rights

Broker API market data is licensed for the account holder's own use. Offering this screener to other people, free or paid, can require an exchange data-redistribution licence; check with the exchange or a licensed data vendor first.

### Known limitations

- Snapshot feeds (about one update per second) do not show every trade, so the buy/sell split is an estimate.
- A volume spike is not proof of an institution. News, block and bulk deals, index rebalancing, option writers hedging, short covering and operator activity all leave similar footprints. The software measures the pattern; it cannot confirm who traded.
- The confirming data (delivery percentage, bulk and block deal disclosures, FII/DII figures) arrives only after the close. Store it next to each day's signals for post-session review.

### Disclaimer

This is a specification for a screening tool, not investment advice. All trading decisions and their outcomes remain the user's responsibility.

## 13. Acceptance criteria and test scenarios

The build is accepted when every scenario below passes as an automated test using synthetic or recorded tick data, with default parameters.

| # | Scenario | Expected result |
| --- | --- | --- |
| 1 | Quiet stock: candle turnover ₹5 cr, RVOL 8, green, close in top 20%, buy share 70% | Provisional alert when running turnover crosses ₹4 cr mid-candle; SURGE\_1 confirmed within 1 s of candle close |
| 2 | Large-cap where ₹5 cr is normal (RVOL 1.3) | No alert |
| 3 | Red ₹6 cr candle, sell share 75% | No long setup; short setup only if enabled |
| 4 | Provisional surge whose candle closes with turnover below the floor | Provisional alert cancelled with reason |
| 5 | After S1 of ₹5 cr: 6-candle pullback, 40% retracement, pullback turnover 20% of S1 | ARMED |
| 6 | During pullback: red candle of ₹4 cr with sell share 70% | INVALIDATED, reason "distribution" |
| 7 | During pullback: a candle closes below S1 low | INVALIDATED, reason "floor broken" |
| 8 | ARMED; S2 of ₹3.5 cr, RVOL 4, green, close above trigger P | ENTRY with entry, stop, T1, T2 and quantity computed per §7 |
| 9 | ARMED; no Surge 2 within 45 minutes | EXPIRED |
| 10 | Qualifying surge at 09:16; another at 15:02 | Neither starts a setup |
| 11 | Stock in F&O ban | Future not scanned; cash share scanned |
| 12 | Futures surge with price up and OI down 1% | Alert raised, grade lowered, tag "short covering" |
| 13 | Feed silent for 20 s mid-candle, then reconnects | Candle flagged gap-affected and cannot trigger; day turnover still correct |
| 14 | Same recorded day replayed twice | Identical event logs |
| 15 | Full universe subscribed, 1,000 snapshots per second sustained | Tick-to-state p99 under 50 ms; no dropped instruments |

## 14. Kickoff prompt

Paste this text first, then this whole document, into an AI coding tool, or hand both to a developer.

```
You are a senior engineer building a real-time stock screener for the Indian market (NSE).
The attached document "Imbalance Setup Screener — Build Prompt & Specification" is the
source of truth. Where it is silent, choose the simplest option and note it.

Build version 1 (alerts only, no order placement):
1. A pluggable market-data adapter with a Zerodha Kite Connect implementation (websocket,
   full mode) and a replay adapter that reads recorded ticks.
2. Instrument/calendar service, 1-minute candle builder (turnover from cumulative ATP x
   volume, estimated buy/sell split, OI), baseline engine (20-session same-minute median
   turnover), and the per-instrument state machine exactly as in sections 3-7.
3. All thresholds in one YAML config with the defaults in section 10. Do not hard-code
   session timings, thresholds or broker specifics.
4. A FastAPI backend with a WebSocket feed, the React dashboard from section 9, and
   Telegram alerts.
5. Storage of raw ticks, candles and every state transition with its input values, plus a
   backtest runner that reuses the same engine (section 11).
6. Automated tests for every scenario in section 13.

Work in this order: restate the state machine in your own words and list any ambiguities
as questions; propose the project structure; then implement module by module, each with
its tests passing before moving on.
```

## Sources

- [Kite Connect WebSocket documentation](https://kite.trade/docs/connect/v3/websocket/): instrument limits and full-mode fields
- [NSE trading hours and block-deal windows (Rupeezy)](https://rupeezy.in/share-market-holiday/nse)
- [NSE F&O hours and closing auction session from 3 August 2026 (Anand Rathi)](https://anandrathi.com/blog/nse-extends-trading-hours)
- [NSE F&O close moved to 15:40 (JM Financial)](https://www.jmfinancialservices.in/blogs-and-articles/nse-extends-fando-trading-hours-starts-from-august-3-2026)
- [Pre-open session for equity futures (Angel One)](https://www.angelone.in/announcements/market/pre-open-session-introduced-in-equity-derivatives-fo)
- [Revised derivatives pre-open from 7 September 2026 (JM Financial)](https://www.jmfinancialservices.in/blogs-and-articles/nse-revises-pre-open-session-framework-for-equity-derivatives-effective-date-7-september-2026)
- [Delta-based MWPL and F&O ban (Zerodha)](https://zerodha.com/z-connect/updates/understanding-the-new-delta-oi-based-mwpl-framework)
- [F&O ban rules (5paisa)](https://www.5paisa.com/blog/sebis-fo-ban-rules-put-delta-based-trading-restrictions-under-the-spotlight)
- [SEBI retail algo rules from 1 April 2026 (Fyers)](https://support.fyers.in/portal/en/kb/articles/what-are-the-new-sebi-rules-for-retail-algo-trading-from-april-01-2026)
- [SEBI retail algo rules explained (Alice Blue)](https://aliceblueonline.com/blog/sebi-algo-trading-rules-for-retail-traders-what-changed-from-april-2026)
