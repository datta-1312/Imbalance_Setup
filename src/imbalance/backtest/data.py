"""Historical 1-minute candles for candle-only backtests (spec §11 Data).

Broker history has no ATP, buy/sell split or intra-minute OI, so:
* turnover = volume x typical price (H+L+C)/3,
* direction comes from candle shape alone: buy share = close location value
  (C-L)/(H-L), so a green candle closing at its high reads as all buying,
* OI open/close come from consecutive candles' OI values.

Storage: one Parquet file per instrument (``<token>.parquet``: ts, open, high,
low, close, volume, oi) plus ``<token>.json`` with the instrument fields.
CSV files with the same columns are accepted too.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable

import pyarrow as pa
import pyarrow.parquet as pq

from ..baseline import MinuteBar, typical_turnover_cr
from ..models import Candle, Instrument, Kind
from ..timeutil import ist_date, minute_of_day, minute_start

_SCHEMA = pa.schema([("ts", pa.float64()), ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()),
                     ("close", pa.float64()), ("volume", pa.int64()), ("oi", pa.int64())])


@dataclass
class InstrumentHistory:
    inst: Instrument
    days: dict[date, list[Candle]] = field(default_factory=dict)

    def bars(self, d: date) -> list[MinuteBar]:
        return [MinuteBar(c.minute, c.open, c.high, c.low, c.close, c.volume, c.turnover_cr, c.oi_close)
                for c in self.days.get(d, [])]


def _inst_from_raw(raw: dict) -> Instrument:
    raw = dict(raw)
    raw["kind"] = Kind(raw.get("kind", "EQ"))
    exp = raw.get("expiry")
    raw["expiry"] = date.fromisoformat(exp) if isinstance(exp, str) and exp else None
    allowed = Instrument.__dataclass_fields__
    return Instrument(**{k: v for k, v in raw.items() if k in allowed})


def save_history(out_dir: str | Path, inst_raw: dict, rows: list[dict]) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tok = int(inst_raw["token"])
    cols = {c: [r.get(c) for r in rows] for c in ("ts", "open", "high", "low", "close", "volume", "oi")}
    cols["volume"] = [int(v or 0) for v in cols["volume"]]
    cols["oi"] = [int(v) if v is not None else None for v in cols["oi"]]
    p = out / f"{tok}.parquet"
    pq.write_table(pa.Table.from_pydict(cols, schema=_SCHEMA), p, compression="zstd")
    meta = dict(inst_raw)
    if isinstance(meta.get("expiry"), date):
        meta["expiry"] = meta["expiry"].isoformat()
    (out / f"{tok}.json").write_text(json.dumps(meta, default=str), encoding="utf-8")
    return p


def candles_from_rows(token: int, rows: Iterable[dict], lot_size: int = 1, volume_in_lots: bool = False
                      ) -> dict[date, list[Candle]]:
    by_day: dict[date, list[Candle]] = defaultdict(list)
    mult = lot_size if volume_in_lots else 1
    for r in sorted(rows, key=lambda x: x["ts"]):
        ts = minute_start(float(r["ts"]))
        o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
        vol = int(r.get("volume") or 0) * mult
        rng = h - l
        loc = (c - l) / rng if rng > 0 else 0.5
        buy = int(round(vol * loc))
        oi = r.get("oi")
        by_day[ist_date(ts)].append(Candle(
            token=token, start=ts, open=o, high=h, low=l, close=c, turnover_cr=typical_turnover_cr(h, l, c, vol),
            volume=vol, buy_vol=buy, sell_vol=vol - buy, oi_close=int(oi) if oi else None, closed=True, n_ticks=1))
    prev_close: float | None = None
    for d in sorted(by_day):
        cs = by_day[d]
        cum_v, cum_pv = 0, 0.0
        prev_oi = None
        for c in cs:
            cum_v += c.volume
            cum_pv += c.volume * (c.high + c.low + c.close) / 3.0
            c.vwap = cum_pv / cum_v if cum_v else c.close
            c.oi_open = prev_oi if prev_oi is not None else c.oi_close
            prev_oi = c.oi_close if c.oi_close is not None else prev_oi
            c.prev_day_close = prev_close
            c.ref_price = c.open
        if cs:
            prev_close = cs[-1].close
    return dict(by_day)


def load_history(directory: str | Path, volume_in_lots: bool = False) -> dict[int, InstrumentHistory]:
    """Every ``<token>.parquet`` / ``<token>.csv`` with a ``<token>.json`` beside it."""
    out: dict[int, InstrumentHistory] = {}
    d = Path(directory)
    for meta_path in sorted(d.glob("*.json")):
        try:
            inst = _inst_from_raw(json.loads(meta_path.read_text(encoding="utf-8")))
        except (ValueError, TypeError, KeyError):
            continue
        pq_path, csv_path = meta_path.with_suffix(".parquet"), meta_path.with_suffix(".csv")
        if pq_path.exists():
            rows = pq.read_table(pq_path).to_pylist()
        elif csv_path.exists():
            with csv_path.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        else:
            continue
        out[inst.token] = InstrumentHistory(inst, candles_from_rows(inst.token, rows, inst.lot_size,
                                                                    volume_in_lots and inst.is_future))
    return out


def history_to_dict(h: dict[int, InstrumentHistory]) -> dict:  # small helper for debugging/tests
    return {tok: {"inst": asdict(x.inst), "days": len(x.days)} for tok, x in h.items()}


def minute_map(history: dict[int, InstrumentHistory], d: date) -> dict[float, dict[int, Candle]]:
    """All instruments' candles of a day grouped by minute (the engine's batch input)."""
    out: dict[float, dict[int, Candle]] = defaultdict(dict)
    for tok, h in history.items():
        for c in h.days.get(d, []):
            out[c.start][tok] = c
    return dict(sorted(out.items()))


def session_minutes(kind: Kind, cont_start: int, cash_end: int, fut_end: int) -> range:
    return range(cont_start, cash_end if kind == Kind.EQ else fut_end)


def in_session(c: Candle, kind: Kind, cont_start: int, cash_end: int, fut_end: int) -> bool:
    m = minute_of_day(c.start)
    return cont_start <= m < (cash_end if kind == Kind.EQ else fut_end)
