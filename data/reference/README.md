Reference files read by the screener (copy the `.example.csv` files and edit):

- `events.csv` — `date,symbol,type[,note]`; types: results, board_meeting, ex_date,
  index_rebalance, fo_inclusion, fo_exclusion, halt, expiry. `symbol` may be `*`.
- `holidays.csv` — one ISO date per line (exchange trading holidays).
- `fo_secban.csv` — written automatically from the NSE ban list each morning; used as a
  fallback when the download fails.

Everything else under `data/` (raw snapshots, candles, event logs, baselines, Kite
session) is runtime data and is not committed.
