"""Instrument service: daily F&O universe from the broker's instrument master,
ban list, rollover, price bands and liquidity floor (spec §4, §8.2).

Nothing is hard-coded: the list of F&O underlyings is rebuilt every morning from
the instrument master because stocks enter and leave the F&O segment.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from .calendar import TradingCalendar
from .config import Params, UniverseConfig
from .models import Instrument, Kind

log = logging.getLogger(__name__)


def parse_instrument_master(text: str) -> list[dict[str, Any]]:
    """Parse the Kite instrument dump (CSV) into dicts with typed fields."""
    rows = []
    for row in csv.DictReader(io.StringIO(text.strip())):
        try:
            row["instrument_token"] = int(row["instrument_token"])
            row["lot_size"] = int(float(row.get("lot_size") or 1))
            row["tick_size"] = float(row.get("tick_size") or 0.05)
            row["last_price"] = float(row.get("last_price") or 0)
            exp = (row.get("expiry") or "").strip()
            row["expiry"] = date.fromisoformat(exp) if len(exp) == 10 else None
        except (KeyError, ValueError):
            continue
        rows.append(row)
    return rows


def parse_ban_list(text: str) -> set[str]:
    """NSE ``fo_secban.csv``: a title line, then ``serial,SYMBOL`` rows (or NIL)."""
    out: set[str] = set()
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit() and parts[1]:
            out.add(parts[1].upper())
        elif len(parts) == 1 and parts[0] and parts[0].isupper() and " " not in parts[0] \
                and parts[0] not in {"NIL", "NONE"}:
            out.add(parts[0])
    return out


def load_ban_list(cfg: UniverseConfig, fetch: bool = True, timeout: float = 10.0) -> tuple[set[str], str]:
    """Fetch today's ban list (falls back to the local file). Returns (symbols, source)."""
    if fetch and cfg.ban_list_url:
        try:
            import httpx

            headers = {"User-Agent": "Mozilla/5.0 (screener)", "Accept": "text/csv,*/*"}
            r = httpx.get(cfg.ban_list_url, headers=headers, timeout=timeout, follow_redirects=True)
            r.raise_for_status()
            symbols = parse_ban_list(r.text)
            if cfg.ban_list_file:
                p = Path(cfg.ban_list_file)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(r.text, encoding="utf-8")
            return symbols, "url"
        except Exception as exc:  # network errors, NSE blocking automated clients, ...
            log.warning("ban list fetch failed (%s); using local file", exc)
    if cfg.ban_list_file and Path(cfg.ban_list_file).exists():
        return parse_ban_list(Path(cfg.ban_list_file).read_text(encoding="utf-8")), "file"
    log.warning("no F&O ban list available: futures in ban cannot be excluded today")
    return set(), "unavailable"


@dataclass
class Universe:
    date: date
    instruments: dict[int, Instrument]
    underlyings: dict[str, dict[str, int]] = field(default_factory=dict)  # und -> {"EQ","FUT","FUT_NEXT"}
    regime: dict[str, int] = field(default_factory=dict)                  # "NIFTY" -> token
    banned: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    def get(self, token: int) -> Instrument | None:
        return self.instruments.get(token)

    def scannable(self) -> list[Instrument]:
        return [i for i in self.instruments.values() if i.scannable]

    def tokens(self) -> list[int]:
        return sorted(self.instruments)

    def by_display(self) -> dict[str, Instrument]:
        return {i.display: i for i in self.instruments.values()}

    def to_dict(self) -> dict[str, Any]:
        def inst(i: Instrument) -> dict[str, Any]:
            d = asdict(i)
            d["kind"] = i.kind.value
            d["expiry"] = i.expiry.isoformat() if i.expiry else None
            return d
        return {
            "date": self.date.isoformat(),
            "instruments": [inst(i) for i in self.instruments.values()],
            "underlyings": self.underlyings,
            "regime": self.regime,
            "banned": sorted(self.banned),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Universe":
        insts = {}
        for raw in d["instruments"]:
            raw = dict(raw)
            raw["kind"] = Kind(raw["kind"])
            raw["expiry"] = date.fromisoformat(raw["expiry"]) if raw.get("expiry") else None
            i = Instrument(**raw)
            insts[i.token] = i
        return cls(date=date.fromisoformat(d["date"]), instruments=insts, underlyings=d.get("underlyings", {}),
                   regime=d.get("regime", {}), banned=set(d.get("banned", [])), notes=d.get("notes", []))


def build_universe(rows: Iterable[Mapping[str, Any]], today: date, calendar: TradingCalendar,
                   ban_list: set[str], params: Params, cfg: UniverseConfig,
                   adtv_cr: Mapping[int, float] | None = None,
                   bands: Mapping[int, tuple[float, float]] | None = None) -> Universe:
    """Build the day's universe: cash share + current-month future per F&O stock,
    next-month future in the rollover window, and index futures for the regime."""
    rows = list(rows)
    index_unds = {u.upper() for u in cfg.index_underlyings}
    regime_unds = {u.upper() for u in cfg.regime_underlyings}
    allow = {s.upper() for s in cfg.symbols_allowlist}

    futs: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        if r.get("instrument_type") == "FUT" and str(r.get("segment", "")).endswith("-FUT") \
                and r.get("exchange") == "NFO" and r.get("expiry") and r["expiry"] >= today:
            futs.setdefault(str(r["name"]).upper(), []).append(r)
    cash = {str(r["tradingsymbol"]).upper(): r for r in rows
            if r.get("exchange") == "NSE" and r.get("instrument_type") == "EQ" and r.get("segment") == "NSE"}

    instruments: dict[int, Instrument] = {}
    underlyings: dict[str, dict[str, int]] = {}
    regime: dict[str, int] = {}
    notes: list[str] = []

    for und, flist in sorted(futs.items()):
        flist = sorted(flist, key=lambda r: r["expiry"])
        cur = flist[0]
        nxt = flist[1] if len(flist) > 1 else None
        if und in index_unds:
            if und in regime_unds:
                i = Instrument(token=cur["instrument_token"], symbol=cur["tradingsymbol"], underlying=und,
                               kind=Kind.IDX, exchange="NFO", lot_size=cur["lot_size"], tick_size=cur["tick_size"],
                               expiry=cur["expiry"], series="current", scannable=False,
                               scan_block_reason="regime_only")
                instruments[i.token] = i
                regime[und] = i.token
            continue
        if allow and und not in allow:
            continue
        eq_row = cash.get(und)
        sessions_left = calendar.sessions_to_expiry(today, cur["expiry"])
        in_rollover = 0 < sessions_left <= params.rollover_sessions
        banned = und in ban_list
        entry: dict[str, int] = {}

        fut = Instrument(token=cur["instrument_token"], symbol=cur["tradingsymbol"], underlying=und, kind=Kind.FUT,
                         exchange="NFO", lot_size=cur["lot_size"], tick_size=cur["tick_size"], expiry=cur["expiry"],
                         series="current", banned=banned, rollover=in_rollover)
        if banned:
            fut.scannable, fut.scan_block_reason = False, "fo_ban"
        instruments[fut.token] = fut
        entry["FUT"] = fut.token

        if eq_row is not None:
            eq = Instrument(token=eq_row["instrument_token"], symbol=eq_row["tradingsymbol"], underlying=und,
                            kind=Kind.EQ, exchange="NSE", lot_size=1, tick_size=eq_row["tick_size"],
                            rollover=in_rollover)
            eq.linked_token, fut.linked_token = fut.token, eq.token
            instruments[eq.token] = eq
            entry["EQ"] = eq.token
        else:
            notes.append(f"{und}: no NSE EQ instrument found")

        if nxt is not None and in_rollover and cfg.include_next_month_in_rollover:
            nf = Instrument(token=nxt["instrument_token"], symbol=nxt["tradingsymbol"], underlying=und,
                            kind=Kind.FUT, exchange="NFO", lot_size=nxt["lot_size"], tick_size=nxt["tick_size"],
                            expiry=nxt["expiry"], series="next", banned=banned, rollover=True,
                            scannable=False, scan_block_reason="next_month")
            instruments[nf.token] = nf
            entry["FUT_NEXT"] = nf.token
        underlyings[und] = entry

    for tok, inst in instruments.items():
        if adtv_cr is not None and inst.kind != Kind.IDX:
            inst.adtv_cr = adtv_cr.get(tok)
            if inst.scannable:
                if inst.adtv_cr is None:
                    inst.scannable, inst.scan_block_reason = False, "no_baseline"
                elif inst.adtv_cr < params.adtv_floor_cr:
                    inst.scannable, inst.scan_block_reason = False, "adtv_below_floor"
        if bands and tok in bands:
            inst.lower_band, inst.upper_band = bands[tok]

    return Universe(date=today, instruments=instruments, underlyings=underlyings, regime=regime,
                    banned=set(ban_list), notes=notes)
