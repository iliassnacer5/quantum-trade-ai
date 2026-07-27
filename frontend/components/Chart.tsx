'use client';

import {
  CandlestickSeries,
  createChart,
  LineStyle,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts';
import { useEffect, useRef, useState } from 'react';
import { api, type Candle, type Signal } from '@/lib/api';

/**
 * Graphique TradingView (lightweight-charts) avec annotations IA :
 * les niveaux entrée / SL / TP du signal sélectionné sont tracés en lignes de prix.
 */
export function Chart({ asset, timeframe, signal }: { asset: string; timeframe: string; signal?: Signal | null }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const [error, setError] = useState('');

  // Création du graphique (une seule fois).
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: { background: { color: '#151A21' }, textColor: '#8A94A6' },
      grid: { vertLines: { color: '#232A33' }, horzLines: { color: '#232A33' } },
      timeScale: { timeVisible: true, borderColor: '#232A33' },
      rightPriceScale: { borderColor: '#232A33' },
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#1D9E75',
      downColor: '#E24B4A',
      borderUpColor: '#1D9E75',
      borderDownColor: '#E24B4A',
      wickUpColor: '#1D9E75',
      wickDownColor: '#E24B4A',
    });
    chartRef.current = chart;
    seriesRef.current = series;
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // Chargement des bougies quand l'actif / timeframe change.
  useEffect(() => {
    let cancelled = false;
    setError('');
    api
      .ohlcv(asset, timeframe)
      .then((res) => {
        if (cancelled || !seriesRef.current) return;
        // Aucune donnée RÉELLE : on affiche un graphique vide et on le DIT, plutôt que de tracer
        // une courbe inventée qui aurait l'air d'un vrai marché.
        if (!res.real || res.candles.length === 0) {
          seriesRef.current.setData([]);
          setError(res.note || `Aucune donnée de marché disponible pour ${asset}.`);
          return;
        }
        seriesRef.current.setData(
          res.candles.map((c: Candle) => ({
            time: c.time as UTCTimestamp,
            open: c.open,
            high: c.high,
            low: c.low,
            close: c.close,
          })),
        );
        chartRef.current?.timeScale().fitContent();
      })
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : 'Erreur chart'));
    return () => {
      cancelled = true;
    };
  }, [asset, timeframe]);

  // Lignes de prix du signal (entrée / SL / TP).
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    const lines: IPriceLine[] = [];
    if (signal && signal.asset === asset && signal.direction !== 'HOLD') {
      lines.push(series.createPriceLine({ price: signal.entry, color: '#FFFFFF', lineWidth: 1, lineStyle: LineStyle.Dashed, title: 'Entrée' }));
      lines.push(series.createPriceLine({ price: signal.stop_loss, color: '#E24B4A', lineWidth: 1, lineStyle: LineStyle.Dotted, title: 'SL' }));
      [signal.take_profit_1, signal.take_profit_2, signal.take_profit_3].forEach((tp, i) => {
        if (tp != null)
          lines.push(series.createPriceLine({ price: tp, color: '#1D9E75', lineWidth: 1, lineStyle: LineStyle.Dotted, title: `TP${i + 1}` }));
      });
    }
    return () => lines.forEach((l) => series.removePriceLine(l));
  }, [signal, asset]);

  return (
    <div className="rounded-xl border border-border bg-surface p-2">
      <div className="px-2 py-1 text-sm text-muted">
        {asset} · {timeframe}
        {signal && signal.asset === asset && <span className="ml-2 text-white">signal {signal.direction}</span>}
      </div>
      <div ref={containerRef} className="h-[320px] w-full" />
      {error && <p className="px-2 py-1 text-xs text-sell">{error}</p>}
    </div>
  );
}
