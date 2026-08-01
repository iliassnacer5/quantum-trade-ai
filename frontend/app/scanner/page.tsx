'use client';

import { Fragment, useEffect, useMemo, useState } from 'react';
import { refreshLabel, useAutoRefresh } from '@/lib/useAutoRefresh';
import { api, Signal } from '@/lib/api';
import { Chart } from '@/components/Chart';
import { SignalCard } from '@/components/SignalCard';
import { MarketSelector, DirectionBadge } from '@/components/domain';
import { PageHeader, Segmented, Table, THead, TBody, TR, TH, TD } from '@/components/ui';
import { TIMEFRAMES } from '@/lib/markets';

/** Les colonnes triables sont celles de LA stratégie du desk : c'est ce qu'elle mesure qui décide,
 *  pas des indicateurs isolés qu'elle n'utilise plus. */
type SortKey = 'symbol' | 'reliability_score' | 'trend_score' | 'confirmations' | 'risk_reward';

const TIER_STYLE: Record<string, string> = {
  ready: 'bg-buy/20 text-buy',
  armed: 'bg-yellow-400/20 text-yellow-300',
  insufficient: 'bg-border text-muted',
};
const TIER_LABEL: Record<string, string> = {
  ready: 'exécutable',
  armed: 'armé',
  insufficient: 'données insuffisantes',
  none: 'refusé',
  'non balayé': 'non balayé',
};

/** Noms lisibles des outils de confirmation — les clés du moteur ne veulent rien dire à l'écran. */
const TOOL_LABEL: Record<string, string> = {
  supply_demand: 'Zone d’offre / de demande',
  structure: 'Cassure de structure (BOS / CHOCH)',
  price_action: 'Figure de retournement',
  support_resistance: 'Support / résistance classé',
  fibonacci: 'Retracement de Fibonacci',
  rsi: 'RSI 14',
  vwap: 'VWAP',
  ema_dynamic: 'EMA 20 / 50 dynamiques',
  volume: 'Volume',
};
const TF_LABEL: Record<string, string> = {
  monthly: 'Mensuel', daily: 'Journalier', h4: '4 h', h1: '1 h', m15: '15 min',
};

function TierBadge({ tier }: { tier?: string }) {
  if (!tier) return <span className="text-2xs text-muted">—</span>;
  return (
    <span className={`rounded px-2 py-0.5 text-2xs font-medium ${TIER_STYLE[tier] ?? 'bg-border text-muted'}`}>
      {TIER_LABEL[tier] ?? tier}
    </span>
  );
}

export default function ScannerPage() {
  const [cls, setCls] = useState('forex');
  const [symbols, setSymbols] = useState<{ symbol: string; asset_class: string }[]>([]);
  const [symbol, setSymbol] = useState('EUR/USD');
  const [tf, setTf] = useState('1h');
  const [tradableOnly, setTradableOnly] = useState(false);

  const [scan_, setScanResult] = useState<any | null>(null);
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
  const [sortKey, setSortKey] = useState<SortKey>('reliability_score');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');
  const [openRow, setOpenRow] = useState<string | null>(null);

  const results: any[] = scan_?.results ?? [];

  const visible = useMemo(() => {
    const arr = tradableOnly
      ? results.filter((r) => r.playbook_tier === 'ready' || r.playbook_tier === 'armed')
      : [...results];
    arr.sort((a, b) => {
      const av = a[sortKey] ?? (sortKey === 'symbol' ? '' : 0);
      const bv = b[sortKey] ?? (sortKey === 'symbol' ? '' : 0);
      const cmp = typeof av === 'string' ? String(av).localeCompare(String(bv)) : Number(av) - Number(bv);
      return sortDir === 'asc' ? cmp : -cmp;
    });
    return arr;
  }, [results, sortKey, sortDir, tradableOnly]);

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
      // 100 = tout le catalogue d'une classe. La stratégie balaie tout, le scanner doit suivre.
      setScanResult(await api.scan(cls || undefined, interval, 100, false, session || undefined));
      setScanned(true);
    } catch (e: any) {
      setError(e.message);
      throw e;
    } finally {
      setScanning(false);
    }
  }

  // Rescan AUTOMATIQUE une fois le premier scan lancé : les marchés bougent, et le verdict de la
  // stratégie est lu dans l'instantané pré-calculé (aucun recalcul lourd ici).
  // 20 s et non 10 s (30/07/2026) : le scanner est une vue de veille, pas un écran d'exécution —
  // dix secondes de fraîcheur en plus n'y changent aucune décision, et c'est autant de requêtes en
  // moins sur la route la plus large du site.
  const autoScan = useAutoRefresh(scan, 20_000, scanned);

  // Lance un trade PAPIER directement depuis une ligne de scan : génère le signal complet (SL/TP)
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
      // Sans niveaux, aucun ordre n'est envoyé : on ne devine pas un stop.
      if (sig.entry == null || sig.stop_loss == null) {
        setError(`${sym} : aucun niveau proposé — pas d'ordre possible.`);
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
        title="Scanner — la stratégie du desk, symbole par symbole"
        subtitle="Chaque ligne porte le verdict de LA stratégie : ses métriques, et pourquoi elle entre ou refuse."
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
          {scanning ? 'Scan…' : 'Scanner avec la stratégie'}
        </button>
        <label className="flex items-center gap-2 text-xs text-muted">
          <input type="checkbox" checked={tradableOnly} onChange={(e) => setTradableOnly(e.target.checked)} />
          Seulement ce que la stratégie prend
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
      {scanned && scan_ && (
        <section className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="flex flex-wrap items-center gap-2 text-lg font-semibold text-white">
              Verdict de la stratégie ({visible.length} symbole{visible.length > 1 ? 's' : ''})
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

          {/* Ce que la stratégie a conclu sur l'univers, avant tout détail. */}
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="rounded-lg border border-buy/40 bg-buy/10 px-3 py-1 text-buy">
              {scan_.ready ?? 0} exécutable(s) — déclencheur actif
            </span>
            <span className="rounded-lg border border-yellow-500/40 bg-yellow-500/10 px-3 py-1 text-yellow-300">
              {scan_.armed ?? 0} armé(s) — contexte validé, en attente du déclencheur
            </span>
            <span className="rounded-lg border border-border bg-background px-3 py-1 text-muted">
              {scan_.refused ?? 0} refusé(s) par la stratégie
            </span>
            {!!scan_.not_scanned && (
              <span className="rounded-lg border border-border bg-background px-3 py-1 text-muted">
                {scan_.not_scanned} pas encore balayé(s) par la boucle de fond
              </span>
            )}
          </div>
          <p className="text-xs text-muted">{scan_.strategy}</p>

          {visible.length === 0 && (
            <p className="text-muted">
              Aucun symbole retenu. Décoche « seulement ce que la stratégie prend » pour voir aussi
              les refus — avec le motif de chacun.
            </p>
          )}
          {tradeMsg && <p className="text-sm text-buy">{tradeMsg} <a href="/wallet" className="underline">Portefeuille →</a></p>}

          {visible.length > 0 && scanView === 'table' && (
            <Table>
              <THead>
                <TR>
                  <TH className="cursor-pointer select-none" onClick={() => toggleSort('symbol')}>Symbole{sortArrow('symbol')}</TH>
                  <TH>Sens</TH>
                  <TH className="text-right">Statut</TH>
                  <TH className="cursor-pointer select-none text-right" onClick={() => toggleSort('reliability_score')} title="Fiabilité du setup selon la stratégie, sur 5">Fiab.{sortArrow('reliability_score')}</TH>
                  <TH className="cursor-pointer select-none text-right" onClick={() => toggleSort('trend_score')} title="Score de la tendance multi-indicateurs (étape 1)">Tendance{sortArrow('trend_score')}</TH>
                  <TH className="cursor-pointer select-none text-right" onClick={() => toggleSort('confirmations')} title="Confirmations pondérées réunies pour l'entrée (étape 2)">Confirm.{sortArrow('confirmations')}</TH>
                  <TH className="cursor-pointer select-none text-right" onClick={() => toggleSort('risk_reward')}>R/R{sortArrow('risk_reward')}</TH>
                  <TH className="text-right">Actions</TH>
                </TR>
              </THead>
              <TBody>
                {visible.map((r) => (
                  <Fragment key={r.symbol}>
                    <TR
                      className={`cursor-pointer ${r.playbook_tier === 'ready' ? 'bg-buy/5' : ''}`}
                      onClick={() => setOpenRow(openRow === r.symbol ? null : r.symbol)}
                    >
                      <TD className="font-mono text-white">
                        <span className="mr-1.5 text-muted">{openRow === r.symbol ? '▾' : '▸'}</span>
                        {r.symbol}
                      </TD>
                      <TD>
                        {r.direction && r.direction !== 'HOLD'
                          ? <DirectionBadge direction={r.direction} size="sm" />
                          : <span className="text-2xs text-muted">—</span>}
                      </TD>
                      <TD className="text-right"><TierBadge tier={r.playbook_tier} /></TD>
                      <TD className="text-right font-mono text-white">
                        {r.reliability_score ? `${Math.abs(r.reliability_score)}/5` : '—'}
                      </TD>
                      <TD className="text-right font-mono text-white">
                        {r.trend_score != null ? `${r.trend_score}/100` : '—'}
                      </TD>
                      <TD className="text-right font-mono text-muted">
                        {r.confirmations != null
                          ? `${r.confirmations}${r.confirmation_score ? ` · ${r.confirmation_score} pt` : ''}`
                          : '—'}
                      </TD>
                      <TD className="text-right font-mono text-muted">
                        {r.risk_reward ? `1:${Number(r.risk_reward).toFixed(2)}` : '—'}
                      </TD>
                      <TD className="text-right" onClick={(e: any) => e.stopPropagation()}>
                        <div className="flex justify-end gap-1.5">
                          <button onClick={() => analyze(r.symbol)} className="rounded border border-border px-2 py-0.5 text-2xs text-white hover:border-accent">Analyser</button>
                          {r.playbook_tier === 'ready' && r.direction !== 'HOLD' && (
                            <button onClick={() => tradeFromScan(r.symbol)} disabled={tradingSym === r.symbol}
                              className={`rounded px-2 py-0.5 text-2xs font-medium text-white disabled:opacity-50 ${r.direction === 'BUY' ? 'bg-buy' : 'bg-sell'}`}>
                              {tradingSym === r.symbol ? '…' : '📈'}
                            </button>
                          )}
                        </div>
                      </TD>
                    </TR>
                    {openRow === r.symbol && (
                      <TR>
                        <TD colSpan={8} className="bg-background/60 p-0">
                          <WhyPanel row={r} />
                        </TD>
                      </TR>
                    )}
                  </Fragment>
                ))}
              </TBody>
            </Table>
          )}

          {scanView === 'cards' && (
            <div className="grid gap-3 md:grid-cols-2">
              {visible.map((r) => (
                <div key={r.symbol}
                  className={`rounded-xl border bg-surface ${r.playbook_tier === 'ready' ? 'border-buy/40' : 'border-border'}`}>
                  <div className="flex items-center justify-between p-4 pb-2">
                    <span className="font-mono font-semibold text-white">{r.symbol}</span>
                    <div className="flex items-center gap-2">
                      {r.direction && r.direction !== 'HOLD' && <DirectionBadge direction={r.direction} size="sm" />}
                      <TierBadge tier={r.playbook_tier} />
                    </div>
                  </div>
                  <div className="grid grid-cols-2 gap-1 px-4 text-xs text-muted">
                    <span>Prix : <span className="text-white">{r.price}</span></span>
                    <span>Tendance : <span className="text-white">{r.trend_score != null ? `${r.trend_score}/100` : '—'}</span></span>
                    <span>Confirmations : <span className="text-white">{r.confirmations ?? '—'}</span></span>
                    <span>R/R : <span className="text-white">{r.risk_reward ? `1:${Number(r.risk_reward).toFixed(2)}` : '—'}</span></span>
                  </div>
                  <WhyPanel row={r} />
                  <div className="flex gap-2 p-4 pt-0">
                    <button onClick={() => analyze(r.symbol)}
                      className="flex-1 rounded-lg border border-border px-2 py-1 text-xs text-white hover:border-accent">
                      Analyser
                    </button>
                    {r.playbook_tier === 'ready' && r.direction !== 'HOLD' && (
                      <button onClick={() => tradeFromScan(r.symbol)} disabled={tradingSym === r.symbol}
                        className={`flex-1 rounded-lg px-2 py-1 text-xs font-medium text-white disabled:opacity-50 ${r.direction === 'BUY' ? 'bg-buy' : 'bg-sell'}`}>
                        {tradingSym === r.symbol ? '…' : '📈 Trader en paper'}
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      <p className="text-xs text-muted">Aide à la décision, pas un conseil en investissement. Aucun setup ne garantit son résultat.</p>
    </div>
  );
}

/**
 * POURQUOI la stratégie décide cela — la partie qui manquait au scanner.
 * On y lit les trois étapes dans l'ordre où le moteur les exécute : comment la tendance a été
 * établie et quelles unités de temps s'alignent, quelles confirmations se sont réunies avec leur
 * poids, d'où viennent le stop et l'objectif. Et quand il n'y a PAS de trade, ce qui a bloqué —
 * c'est l'information la plus utile, celle qui évite de chercher un signal qui n'existe pas.
 */
function WhyPanel({ row }: { row: any }) {
  const why = row.why;
  if (!why) {
    return (
      <p className="p-4 text-xs text-muted">
        Ce symbole n’a pas encore été balayé par la boucle de fond de la stratégie — elle publie un
        instantané complet toutes les quelques minutes. Clique « Analyser » pour forcer le calcul.
      </p>
    );
  }
  const conf: any[] = why.confirmations ?? [];
  const tfs: any[] = why.timeframes ?? [];
  const blocking: string[] = why.blocking ?? [];

  return (
    <div className="space-y-4 p-4 text-xs">
      {/* Étape 1 — la tendance */}
      <section>
        <h4 className="mb-1.5 font-medium text-white">1 — Tendance de fond</h4>
        {tfs.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {tfs.map((t) => (
              <span
                key={t.tf}
                title={`score ${t.score}`}
                className={`rounded px-2 py-0.5 font-mono text-2xs ${
                  t.aligned ? 'bg-buy/15 text-buy' : t.bias === 0 ? 'bg-border text-muted' : 'bg-sell/15 text-sell'
                }`}
              >
                {TF_LABEL[t.tf] ?? t.tf} {t.score != null ? (t.score > 0 ? '+' : '') + Number(t.score).toFixed(2) : '—'}
              </span>
            ))}
          </div>
        )}
        {why.trend_explanation && (
          <p className="whitespace-pre-wrap leading-relaxed text-muted">{why.trend_explanation}</p>
        )}
        {why.trend_reasons?.length > 0 && (
          <p className="mt-1 text-muted">Réserve : {why.trend_reasons.join(' · ')}</p>
        )}
      </section>

      {/* Étape 2 — les confirmations d'entrée */}
      <section>
        <h4 className="mb-1.5 font-medium text-white">
          2 — Confirmations d’entrée{' '}
          {conf.length > 0 && (
            <span className="font-normal text-muted">
              ({conf.length} outils · {row.confirmation_score ?? 0} points pondérés)
            </span>
          )}
        </h4>
        {conf.length === 0 ? (
          <p className="text-muted">Aucune confirmation réunie pour l’instant sur l’unité d’entrée.</p>
        ) : (
          <ul className="space-y-1">
            {conf.map((c, i) => (
              <li key={i} className="flex flex-wrap items-baseline gap-x-2 rounded bg-surface px-2 py-1">
                <span className="font-medium text-white">{TOOL_LABEL[c.key] ?? c.key}</span>
                {c.strong && <span className="rounded bg-buy/20 px-1 text-[10px] text-buy">forte</span>}
                <span className="font-mono text-2xs text-muted">
                  {c.contribution} pt (poids {c.weight} × qualité {c.quality})
                </span>
                <span className="w-full text-muted">{c.reading}</span>
              </li>
            ))}
          </ul>
        )}
        {row.trigger && <p className="mt-1.5 text-muted">Déclencheur : {row.trigger}</p>}
      </section>

      {/* Étape 3 — les sorties */}
      {row.entry != null && (
        <section>
          <h4 className="mb-1.5 font-medium text-white">3 — Sorties posées sur des niveaux</h4>
          <div className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-2xs text-muted">
            <span>Entrée <span className="text-white">{row.entry}</span></span>
            <span>SL <span className="text-sell">{row.stop_loss}</span>{row.risk_pips ? ` (${row.risk_pips} ${row.pips_label ?? ''})` : ''}</span>
            <span>TP1 <span className="text-buy">{row.take_profit_1}</span>{row.reward_pips ? ` (${row.reward_pips} ${row.pips_label ?? ''})` : ''}</span>
            {row.take_profit_2 != null && <span>TP2 <span className="text-buy">{row.take_profit_2}</span></span>}
            {row.risk_reward ? <span>R/R <span className="text-white">1:{Number(row.risk_reward).toFixed(2)}</span></span> : null}
            {row.horizon_label && <span>horizon {row.horizon_label}</span>}
          </div>
          {why.stop_basis && <p className="mt-1 text-muted">Stop : {why.stop_basis}</p>}
          {why.target_basis && <p className="text-muted">Objectif : {why.target_basis}</p>}
          {why.secure_stop != null && (
            <p className="text-muted">
              Sécurisation : le stop remontera sur {why.secure_stop} dès que +2R sera parcouru
              {why.tp1_lock_stop != null && `, puis sur ${why.tp1_lock_stop} si TP1 est touché avec un momentum qui confirme`}.
            </p>
          )}
          {why.volatility?.decision && why.volatility.decision !== 'none' && (
            <p className="text-warn">Volatilité : {why.volatility.reason ?? why.volatility.decision}</p>
          )}
        </section>
      )}

      {/* Ce qui bloque — la réponse la plus utile quand il n'y a pas de trade */}
      {blocking.length > 0 && (
        <section className="rounded-lg border border-sell/30 bg-sell/5 p-2">
          <h4 className="mb-1 font-medium text-sell">Pourquoi il n’y a pas de trade</h4>
          <ul className="space-y-0.5">
            {blocking.map((b, i) => (
              <li key={i} className="flex gap-1.5 text-muted"><span className="text-sell">✕</span>{b}</li>
            ))}
          </ul>
        </section>
      )}

      {/* La checklist brute des étapes : l'audit complet, pour qui veut vérifier */}
      {why.checklist?.length > 0 && (
        <details>
          <summary className="cursor-pointer text-muted hover:text-white">Checklist complète des étapes</summary>
          <ul className="mt-1.5 space-y-0.5">
            {why.checklist.map((c: any, i: number) => (
              <li key={i} className="flex gap-1.5">
                <span className={c.pass ? 'text-buy' : 'text-sell'}>{c.pass ? '✓' : '✕'}</span>
                <span className="text-muted"><span className="text-white">{c.label}</span> — {c.value}</span>
              </li>
            ))}
          </ul>
        </details>
      )}

      {/* Ce que la MESURE dit de ce symbole — le backtest hebdomadaire, pas une opinion. */}
      {row.pair_verdict && (
        <p className="text-muted">
          Verdict mesuré de la paire : <span className="text-white">{row.pair_verdict.emoji} {row.pair_verdict.status}</span>
          {row.pair_verdict.trades != null && (
            <span className="font-mono">
              {' '}· {row.pair_verdict.expectancy_r > 0 ? '+' : ''}{row.pair_verdict.expectancy_r} R
              sur {row.pair_verdict.trades} trades ({row.pair_verdict.win_rate} %)
            </span>
          )}
          {row.pair_verdict.reason ? ` — ${row.pair_verdict.reason}` : ''}
        </p>
      )}
    </div>
  );
}
