import { useEffect, useRef } from "react";
import {
  ColorType,
  CrosshairMode,
  LineStyle,
  createChart,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type SeriesMarker,
  type UTCTimestamp,
} from "lightweight-charts";
import type { CandlePoint } from "../types";

export interface Level {
  price: number | null | undefined;
  color: string;
  title: string;
  dashed?: boolean;
}

export interface Mark {
  t: number;
  text: string;
  color: string;
  below?: boolean;
}

const IST_OFFSET = 19800; // the chart's time axis is UTC; shift so it reads IST
const GAP = 1 | 4 | 8;

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888888";
}

interface Series {
  chart: IChartApi;
  candles: ISeriesApi<"Candlestick">;
  turnover: ISeriesApi<"Histogram">;
  baseline: ISeriesApi<"Line">;
  threshold: ISeriesApi<"Line">;
  vwap: ISeriesApi<"Line">;
  oi: ISeriesApi<"Line">;
  lines: IPriceLine[];
}

export function PriceChart({ candles, levels, marks, showOi, rvolMin, theme }: {
  candles: CandlePoint[];
  levels: Level[];
  marks: Mark[];
  showOi: boolean;
  rvolMin: number;
  theme: string;
}) {
  const el = useRef<HTMLDivElement>(null);
  const s = useRef<Series | null>(null);
  const fitted = useRef(false);

  useEffect(() => {
    if (!el.current) return;
    const up = cssVar("--up");
    const down = cssVar("--down");
    const border = cssVar("--border");
    const chart = createChart(el.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: cssVar("--surface") }, textColor: cssVar("--muted"), fontSize: 11 },
      grid: { vertLines: { color: `${border}66` }, horzLines: { color: `${border}66` } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: border, scaleMargins: { top: 0.06, bottom: 0.32 } },
      leftPriceScale: { borderColor: border, visible: showOi, scaleMargins: { top: 0.06, bottom: 0.32 } },
      timeScale: { borderColor: border, timeVisible: true, secondsVisible: false, rightOffset: 4 },
    });
    const series: Series = {
      chart,
      candles: chart.addCandlestickSeries({
        upColor: up, downColor: down, wickUpColor: up, wickDownColor: down, borderVisible: false,
        priceFormat: { type: "price", precision: 2, minMove: 0.01 },
      }),
      turnover: chart.addHistogramSeries({
        priceScaleId: "turnover", lastValueVisible: false, priceLineVisible: false,
        priceFormat: { type: "custom", formatter: (x: number) => `₹${x.toFixed(2)}cr`, minMove: 0.01 },
      }),
      baseline: chart.addLineSeries({
        priceScaleId: "turnover", color: cssVar("--amber"), lineWidth: 1, lineStyle: LineStyle.Dotted,
        lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false,
      }),
      threshold: chart.addLineSeries({
        priceScaleId: "turnover", color: cssVar("--amber"), lineWidth: 1, lineStyle: LineStyle.Dashed,
        lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false,
      }),
      vwap: chart.addLineSeries({ color: cssVar("--violet"), lineWidth: 1, lastValueVisible: false, priceLineVisible: false }),
      oi: chart.addLineSeries({
        priceScaleId: "left", color: cssVar("--sky"), lineWidth: 1, lastValueVisible: false, priceLineVisible: false,
        visible: showOi, priceFormat: { type: "volume" },
      }),
      lines: [],
    };
    chart.priceScale("turnover").applyOptions({ scaleMargins: { top: 0.74, bottom: 0 } });
    s.current = series;
    fitted.current = false;
    return () => {
      chart.remove();
      s.current = null;
    };
  }, [theme, showOi]);

  useEffect(() => {
    const ser = s.current;
    if (!ser) return;
    const up = cssVar("--up");
    const down = cssVar("--down");
    const faint = cssVar("--faint");
    const T = (t: number) => (t + IST_OFFSET) as UTCTimestamp;
    ser.candles.setData(candles.map((c) => ({ time: T(c.t), open: c.o, high: c.h, low: c.l, close: c.c })));
    ser.turnover.setData(candles.map((c) => ({
      time: T(c.t), value: c.turnover,
      color: c.flags & GAP ? `${faint}99` : c.c >= c.o ? `${up}aa` : `${down}aa`,
    })));
    ser.baseline.setData(candles.filter((c) => c.baseline != null).map((c) => ({ time: T(c.t), value: c.baseline as number })));
    ser.threshold.setData(candles.filter((c) => c.baseline != null).map((c) => ({ time: T(c.t), value: (c.baseline as number) * rvolMin })));
    ser.vwap.setData(candles.filter((c) => c.vwap != null).map((c) => ({ time: T(c.t), value: c.vwap as number })));
    ser.oi.setData(candles.filter((c) => c.oi != null && c.oi > 0).map((c) => ({ time: T(c.t), value: c.oi as number })));
    const ms: SeriesMarker<UTCTimestamp>[] = marks
      .filter((m) => m.t)
      .sort((a, b) => a.t - b.t)
      .map((m) => ({
        time: T(Math.floor(m.t / 60) * 60), position: m.below ? "belowBar" : "aboveBar", color: m.color,
        shape: m.below ? "arrowUp" : "arrowDown", text: m.text,
      }));
    ser.candles.setMarkers(ms);
    for (const l of ser.lines) ser.candles.removePriceLine(l);
    ser.lines = levels
      .filter((l) => l.price != null && Number.isFinite(l.price))
      .map((l) => ser.candles.createPriceLine({
        price: l.price as number, color: l.color, lineWidth: 1, lineStyle: l.dashed ? LineStyle.Dashed : LineStyle.Solid,
        axisLabelVisible: true, title: l.title,
      }));
    if (!fitted.current && candles.length) {
      const n = candles.length;
      ser.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 120), to: n + 3 });
      fitted.current = true;
    }
  }, [candles, levels, marks, rvolMin, theme, showOi]);

  return <div ref={el} className="chart" />;
}
