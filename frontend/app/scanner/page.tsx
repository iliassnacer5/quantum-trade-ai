'use client';

import { useEffect, useMemo, useState } from 'react';
import { refreshLabel, useAutoRefresh } from '@/lib/useAutoRefresh';
import { api, Signal } from '@/lib/api';
import { Chart } from '@/components/Chart';
import { SignalCard } from '@/components/SignalCard';
import { MarketSelector, DirectionBadge } from '@/components/domain';
import { PageHeader, Segmented, Table, THead, TBody, TR, TH, TD } from '@/components/ui';
import { TIMEFRAMES } from '@/lib/markets';

type SortKey = 'symbol' | 'conviction' | 'adx' | 'rsi';

export default function ScannerPage() {
  const [cls, setCls] = useState('crypto');
  const [symbols, setSymbols] = useState<{ symbol: string; asset_class: string }[]>([]);
  const [symbol, setSymbol] = useState('BTC/USDT');
  const [tf, setTf] = useState('swing');
  const [hcOnly, setHcOnly] = useState(false);

  const [results, setResults] = useState<any[]>([]);
  const [scanning, setScanning] = useState(false);
  const [scanned, setScanned] = useState(false);
  const [signal, setSignal] = useState<Signal | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sessions, setSessions] = useState<{ id: string; label: string; window_utc: string; open: boolean; symbol_count: number }[]>([]);
  const [utcTime, setUtcTime] = useState('');
  const [session, setSession] = useState<string>('');
  const [tradingSym, setTradingSym] = useState<string | null>(null);
  const [tradeMsg, setTradeMsg] = useState<string | null>(null);
  const [scanView, setScanView] = useState<'table' | 'cards'>('table');
  const [sortKey, setSortKey] = useState<SortKey>('conviction');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');

  const sortedResults = useMemo(() => {
    const arr = [...results];
    arr.sort((a, b) => {
      const av = a[sortKey] ?? (sortKey === 'symbol' ? '' : 0);
      const bv = b[sortKey] ?? (sortKey === 'symbol' ? '' : 0);
      const cmp = typeof av === 'string' ? String(av).localeCompare(String(bv)) : Number(av) - Number(bv);
      return sortDir === 'asc' ? cmp : -cmp;
    });
    return arr;
  }, [results, sortKey, sortDir]);

  function toggleSort(k: SortKey) {
    if (sortKey === k) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    else { setSortKey(k); setSortDir(k === 'symbol' ? 'asc' : 'desc'); }
  }
  const sortArrow = (k: SortKey) => (sortKey === k ? (sortDir === 'asc' ? ' ▲' : ' ▼') : '');

  const interval = useMemo(() => TIMEFRAMES.find((t) => t.tf === tf)?.interval ?? '1h', [tf]);

  useEffect(() => {
    api.sessions().then((d) => { setSessions(d.sessions); setUtcTime(d.utc_time); }).catch(() => {});
  }, []);

  // Charge la liste des symboles selon la classe.
  useEffect(() => {
    api.symbols(undefined, cls || undefined)
      .then((d) => {
        setSymbols(d.results);
        if (d.results.length && !d.results.some((r) => r.symbol === symbol)) setSymbol(d.results[0].symbol);
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cls]);

  async function scan() {
    setScanning(true);
    setError(null);
    try {
      const r = await api.scan(cls || undefined, interval, 30, hcOnly, session || undefined);
      setResults(r.results);
      setScanned(true);
    } catch (e: any) {
      setError(e.message);
      throw e;
    } finally {
      setScanning(false);
    }
  }

  // Rescan AUTOMATIQUE toutes les 3 min une fois le premier scan lancé : les marchés bougent,
  // l'analyse affichée ne doit pas dater.
  const autoScan = useAutoRefresh(scan, 180_000, scanned);

  // Lance un trade PAPIER directement depuis une carte de scan : génère le signal complet (SL/TP)
  // puis ouvre la position. Refuse si le signal consolidé est HOLD (cohérent avec l'analyse).
  async function tradeFromScan(sym: string) {
    setTradingSym(sym);
    setError(null);
    setTradeMsg(null);
    try {
      const sig = await api.generate(sym, tf, false);
      if (sig.direction === 'HOLD') {
        setError(`${sym} : signal consolidé HOLD — pas de trade (les agents divergent).`);
        return;
      }
      const conns = await api.brokers();
      const paper = conns.find((c) => c.mode === 'paper') ?? (await api.connectBroker('paper', 'paper'));
      const riskPerUnit = Math.abs(sig.entry - sig.stop_loss);
      const qty = riskPerUnit > 0 ? Number(((10000 * 0.01) / riskPerUnit).toFixed(6)) : 0;
      if (!qty) { setError('Taille nulle — niveaux invalides.'); return; }
      await api.placeOrder(paper.id, sym, sig.direction === 'BUY' ? 'buy' : 'sell', qty, sig.stop_loss, sig.take_profit_1);
      setTradeMsg(`✅ ${sig.direction} ${qty} ${sym} ouvert en paper (1% de risque). Voir le portefeuille.`);
    } catch (e: any) {
      setError(e.message?.includes('402') ? 'Limite du plan atteinte.' : e.message);
    } finally {
      setTradingSym(null);
    }
  }

  async function analyze(sym?: string) {
    const target = sym ?? symbol;
    if (sym) setSymbol(sym);
    setAnalyzing(true);
    setError(null);
    try {
      setSignal(await api.generate(target, tf, false));
    } catch (e: any) {
      setError(e.message?.includes('402') ? 'Limite de marchés du plan atteinte (passe en Pro/Elite).' : e.message);
    } finally {
      setAnalyzing(false);
    }
  }

  return (
    <div className="p-6 space-y-5">
      <PageHeader
        title="Poste d’analyse & Scanner"
        subtitle="Choisis marché, paire et timeframe — chart réel + analyse multi-agents."
      />

      {/* Sessions mondiales */}
      {sessions.length > 0 && (
        <section className="rounded-xl border border-border bg-surface p-4">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-sm font-semibold text-white">Sessions mondiales</span>
            <span className="text-xs text-muted">{utcTime}</span>
          </div>
          <div className="flex flex-wrap gap-2">
            <button onClick={() => setSession('')}
              className={`rounded-lg border px-3 py-1.5 text-sm ${session === '' ? 'border-accent bg-accent/10 text-white' : 'border-border text-muted hover:bg-background'}`}>
              Tous marchés
            </button>
            {sessions.map((s) => (
              <button key={s.id} onClick={() => setSession(s.id)}
                className={`rounded-lg border px-3 py-1.5 text-sm ${session === s.id ? 'border-accent bg-accent/10 text-white' : 'border-border text-muted hover:bg-background'}`}>
                <span className={`mr-1.5 inline-block h-2 w-2 rounded-full ${s.open ? 'bg-buy' : 'bg-muted/40'}`} />
                {s.label} <span className="text-[10px] text-muted">({s.window_utc})</span>
                {s.open && <span className="ml-1 text-[10px] text-buy">● ouverte</span>}
              </button>
            ))}
          </div>
          {session && <p className="mt-2 text-xs text-muted">Le scan ne portera que sur les paires liquides de cette session.</p>}
        </section>
      )}

      {/* Sélecteurs */}
      <section className="flex flex-wrap items-end gap-3 rounded-xl border border-border bg-surface p-4">
        <div>
          <label className="mb-1 block text-xs text-muted">Marché</label>
          <MarketSelector value={cls} onChange={setCls} label={null} />
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted">Paire / Symbole</label>
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}
            className="rounded-lg border border-border bg-background px-3 py-2 font-mono text-sm outline-none focus:border-accent">
            {symbols.map((s) => <option key={s.symbol} value={s.symbol}>{s.symbol}</option>)}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted">Timeframe</label>
          <select value={tf} onChange={(e) => setTf(e.target.value)}
            className="rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:border-accent">
            {TIMEFRAMES.map((t) => <option key={t.tf} value={t.tf}>{t.label}</option>)}
          </select>
        </div>
        <button onClick={() => analyze()} disabled={analyzing}
          className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-50">
          {analyzing ? 'Analyse…' : 'Analyser ce symbole'}
        </button>
        <button onClick={scan} disabled={scanning}
          className="rounded-lg border border-border px-4 py-2 text-sm text-white hover:bg-background disabled:opacity-50">
          {scanning ? 'Scan…' : 'Scanner le marché'}
        </button>
        <label className="flex items-center gap-2 text-xs text-muted">
          <input type="checkbox" checked={hcOnly} onChange={(e) => setHcOnly(e.target.checked)} />
          Haute-conviction seulement
        </label>
      </section>

      {error && <p className="text-sell">{error}</p>}

      {/* Chart réel + carte signal */}
      <section className="grid gap-5 lg:grid-cols-2">
        <Chart asset={symbol} timeframe={tf} signal={signal} />
        <div>
          {signal ? <SignalCard s={signal} /> : (
            <div className="flex h-full min-h-[200px] items-center justify-center rounded-xl border border-dashed border-border text-sm text-muted">
              Clique « Analyser ce symbole » pour l&apos;analyse complète (métriques + multi-timeframe + news).
            </div>
          )}
        </div>
      </section>

      {/* Résultats du scan */}
      {scanned && (
        <section className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="flex flex-wrap items-center gap-2 text-lg font-semibold text-white">
              Résultats du scan ({results.length}{results.length ? ` · ${results.filter((r) => r.high_conviction).length} haute-conviction` : ''})
              <span className="text-[11px] font-normal text-muted">
                <span className={autoScan.refreshing ? 'text-accent' : ''}>🔄 rescan auto</span> · {refreshLabel(autoScan)}
              </span>
              <span className="rounded bg-surface px-2 py-0.5 text-xs font-normal text-muted">
                ⏱ {TIMEFRAMES.find((t) => t.tf === tf)?.label ?? tf}
              </span>
            </h2>
            <Segmented
              value={scanView}
              onChange={setScanView}
              options={[{ value: 'table', label: '☰ Table' }, { value: 'cards', label: '▦ Cartes' }]}
            />
          </div>
          {results.length === 0 && <p className="text-muted">Aucun symbole ne correspond. Décoche « haute-conviction » pour voir tout le classement.</p>}
          {tradeMsg && <p className="text-sm text-buy">{tradeMsg} <a href="/wallet" className="underline">Portefeuille →</a></p>}

          {results.length > 0 && scanView === 'table' && (
            <Table>
              <THead>
                <TR>
                  <TH className="cursor-pointer select-none" onClick={() => toggleSort('symbol')}>Symbole{sortArrow('symbol')}</TH>
                  <TH>Direction</TH>
                  <TH className="cursor-pointer select-none text-right" onClick={() => toggleSort('conviction')}>Conviction{sortArrow('conviction')}</TH>
                  <TH className="cursor-pointer select-none text-right" onClick={() => toggleSort('adx')}>ADX{sortArrow('adx')}</TH>
                  <TH className="cursor-pointer select-none text-right" onClick={() => toggleSort('rsi')}>RSI{sortArrow('rsi')}</TH>
                  <TH className="text-right">Multi-TF</TH>
                  <TH className="text-right">Actions</TH>
                </TR>
              </THead>
              <TBody>
                {sortedResults.map((r) => (
                  <TR key={r.symbol} className={r.high_conviction ? 'bg-buy/5' : ''}>
                    <TD className="font-mono text-white">{r.symbol}</TD>
                    <TD>
                      {r.consolidated
                        ? <DirectionBadge direction={r.direction} size="sm" />
                        : <span className="text-2xs text-muted">⏳ à analyser</span>}
                    </TD>
                    <TD className="text-right font-mono text-white">{r.conviction}</TD>
                    <TD className="text-right font-mono text-muted">{r.adx}</TD>
                    <TD className="text-right font-mono text-muted">{r.rsi}</TD>
                    <TD className={`text-right ${r.mtf_aligned >= 2 ? 'text-buy' : 'text-warn'}`}>
                      {r.mtf_total != null ? `${r.mtf_aligned}/${r.mtf_total}` : '—'}
                    </TD>
                    <TD className="text-right">
                      <div className="flex justify-end gap-1.5">
                        <button onClick={() => analyze(r.symbol)} className="rounded border border-border px-2 py-0.5 text-2xs text-white hover:border-accent">Analyser</button>
                        {r.consolidated && r.direction !== 'HOLD' && (
                          <button onClick={() => tradeFromScan(r.symbol)} disabled={tradingSym === r.symbol}
                            className={`rounded px-2 py-0.5 text-2xs font-medium text-white disabled:opacity-50 ${r.direction === 'BUY' ? 'bg-buy' : 'bg-sell'}`}>
                            {tradingSym === r.symbol ? '…' : '📈'}
                          </button>
                        )}
                      </div>
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )}

          <div className={`grid gap-3 md:grid-cols-2 lg:grid-cols-3 ${scanView === 'table' ? 'hidden' : ''}`}>
            {results.map((r) => (
              <div key={r.symbol}
                className={`rounded-xl border bg-surface p-4 ${r.high_conviction ? 'border-buy/40' : 'border-border'}`}>
                <div className="flex items-center justify-between">
                  <span className="font-mono font-semibold text-white">{r.symbol}</span>
                  {r.consolidated ? (
                    <span className={`rounded px-2 py-0.5 text-xs font-bold ${r.direction === 'BUY' ? 'bg-buy/20 text-buy' : r.direction === 'SELL' ? 'bg-sell/20 text-sell' : 'bg-border text-muted'}`}>{r.direction}</span>
                  ) : (
                    <span className="rounded bg-border px-2 py-0.5 text-xs text-muted" title="Lead technique — clique Analyser pour la décision complète">⏳ à analyser</span>
                  )}
                </div>
                <div className="mt-2 grid grid-cols-2 gap-1 text-xs text-muted">
                  <span>Prix : <span className="text-white">{r.price}</span></span>
                  <span>ADX : <span className="text-white">{r.adx}</span></span>
                  <span>RSI : <span className="text-white">{r.rsi}</span></span>
                  <span>Conviction : <span className="text-white">{r.conviction}</span></span>
                  {r.mtf_total != null && (
                    <span>Multi-TF : <span className={r.mtf_aligned >= 2 ? 'text-buy' : 'text-yellow-300'}>{r.mtf_aligned}/{r.mtf_total}</span></span>
                  )}
                </div>
                <p className="mt-1 text-xs text-gray-400">{r.trend}</p>
                {/* Verdict affiché UNIQUEMENT pour les candidats consolidés (= identiques à l'analyse). */}
                {r.consolidated && r.high_conviction ? (
                  <span className="mt-2 inline-block rounded bg-buy/20 px-2 py-0.5 text-[10px] font-bold text-buy">★ HAUTE CONVICTION (= analyse détaillée)</span>
                ) : r.consolidated && r.direction !== 'HOLD' ? (
                  <span className="mt-2 inline-block rounded bg-yellow-500/15 px-2 py-0.5 text-[10px] font-bold text-yellow-300">
                    Signal {r.direction} — sans haute-conviction
                  </span>
                ) : !r.consolidated ? (
                  <span className="mt-2 inline-block rounded bg-border px-2 py-0.5 text-[10px] text-muted">Lead technique — clique « Analyser » pour la décision</span>
                ) : null}
                <div className="mt-3 flex gap-2">
                  <button onClick={() => analyze(r.symbol)}
                    className="flex-1 rounded-lg border border-border px-2 py-1 text-xs text-white hover:border-accent">
                    Analyser
                  </button>
                  {r.consolidated && r.direction !== 'HOLD' && (
                    <button onClick={() => tradeFromScan(r.symbol)} disabled={tradingSym === r.symbol}
                      className={`flex-1 rounded-lg px-2 py-1 text-xs font-medium text-white disabled:opacity-50 ${r.direction === 'BUY' ? 'bg-buy' : 'bg-sell'}`}>
                      {tradingSym === r.symbol ? '…' : '📈 Trader en paper'}
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      <p className="text-xs text-muted">Aide à la décision, pas un conseil en investissement. La haute conviction ne garantit pas le résultat.</p>
    </div>
  );
}
