'use client';

import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';
import { useAutoRefresh } from '@/lib/useAutoRefresh';
import { Chart } from '@/components/Chart';
import { SignalCard } from '@/components/SignalCard';
import { motion } from 'framer-motion';
import { MarketSelector, SessionPicker, SymbolPicker, AgentProgress } from '@/components/domain';
import { staggerContainer, staggerItem } from '@/lib/motion';
import {
  api,
  openSignalStream,
  type HeatmapItem,
  type Me,
  type Portfolio,
  type RiskStatus,
  type Signal,
} from '@/lib/api';

const TIMEFRAMES = ['scalp', 'intraday', 'swing', 'position'];

export default function DashboardPage() {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [asset, setAsset] = useState('BTC/USDT');
  const [timeframe, setTimeframe] = useState('swing');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [live, setLive] = useState(false);
  const [livePrices, setLivePrices] = useState<Record<string, number>>({});
  const [sigMode, setSigMode] = useState('strict');

  useEffect(() => {
    api.signalMode().then((d) => setSigMode(d.mode)).catch(() => {});
  }, []);
  const [selected, setSelected] = useState<Signal | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [pnl, setPnl] = useState<Portfolio | null>(null);
  const [risk, setRisk] = useState<RiskStatus | null>(null);
  const [heat, setHeat] = useState<HeatmapItem[]>([]);
  const [heatMix, setHeatMix] = useState(false);
  const [symbols, setSymbols] = useState<{ symbol: string; asset_class: string }[]>([]);
  const [mktClass, setMktClass] = useState('');
  const [session, setSession] = useState('');

  // Catalogue de symboles filtré par marché / session.
  useEffect(() => {
    api.symbols(undefined, mktClass || undefined, session || undefined)
      .then((d) => setSymbols(d.results))
      .catch(() => {});
  }, [mktClass, session]);

  useEffect(() => {
    api.heatmap(heatMix).then(setHeat).catch(() => {});
    // `autoPanels` prend ensuite le relais pour maintenir la heatmap à jour.
  }, [heatMix]);

  const loadPanels = useCallback(async () => {
    try {
      const [p, r, h] = await Promise.all([api.portfolio(), api.riskStatus(), api.heatmap(heatMix)]);
      setPnl(p);
      setRisk(r);
      setHeat(h);
    } catch {
      /* panels best-effort */
    }
  }, [heatMix]);

  // Portefeuille, risque et variations 24 h rafraîchis TOUT SEULS (les prix bougent en continu).
  const autoPanels = useAutoRefresh(loadPanels, 30_000);

  const load = useCallback(async () => {
    try {
      const [m, s] = await Promise.all([api.me(), api.listSignals()]);
      setMe(m);
      // La recherche de la topbar route vers /dashboard?symbol=… : on l'honore en priorité.
      const querySymbol =
        typeof window !== 'undefined' ? new URLSearchParams(window.location.search).get('symbol') : null;
      setAsset(querySymbol ? querySymbol.toUpperCase() : (m.watchlist[0] ?? 'BTC/USDT'));
      setSignals(s);
      loadPanels();
    } catch {
      router.push('/login');
    }
  }, [router, loadPanels]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    const ws = openSignalStream(
      (sig) => setSignals((prev) => [sig, ...prev]),
      (candle) => setLivePrices((prev) => ({ ...prev, [candle.symbol]: candle.close })),
    );
    if (ws) {
      ws.onopen = () => setLive(true);
      ws.onclose = () => setLive(false);
    }
    return () => ws?.close();
  }, []);

  async function generate() {
    setLoading(true);
    setError('');
    try {
      const sig = await api.generate(asset, timeframe, false);
      // Le WS pousse aussi le signal ; on déduplique par sécurité.
      setSignals((prev) => (prev.some((p) => p.id === sig.id) ? prev : [sig, ...prev]));
      setSelected(sig);
      loadPanels();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Erreur');
    } finally {
      setLoading(false);
    }
  }

  async function clearHistory() {
    if (!confirm("Vider tout l'historique des signaux ? Cette action est irréversible.")) return;
    try {
      await api.clearSignals();
      setSignals([]);
      setSelected(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Erreur');
    }
  }

  // Déduplication : un seul signal (le plus récent) par actif. La liste arrive déjà triée par récence.
  const uniqueSignals = Array.from(new Map(signals.map((s) => [s.asset, s])).values());
  const visibleSignals = showAll ? uniqueSignals : uniqueSignals.slice(0, 6);

  return (
    <main className="mx-auto max-w-5xl p-6">
      <header className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Dashboard</h1>
          {me && (
            <p className="text-xs text-muted">
              {me.email} · plan <span className="uppercase text-white">{me.plan}</span> · capital{' '}
              {me.capital} USDT · profil {me.risk_profile}
              <span className={`ml-2 ${live ? 'text-buy' : 'text-muted'}`}>{live ? '● live' : '○ hors-ligne'}</span>
            </p>
          )}
        </div>
        {Object.keys(livePrices).length > 0 && (
          <div className="flex flex-wrap items-center justify-end gap-2 text-xs">
            <span className="flex items-center gap-1 text-buy"><span className="inline-block h-2 w-2 animate-pulse rounded-full bg-buy" /> LIVE</span>
            {['BTC/USDT', 'ETH/USDT', 'SOL/USDT'].filter((s) => livePrices[s] != null).map((s) => (
              <span key={s} className="rounded bg-surface px-2 py-1 font-mono text-white">
                {s.split('/')[0]} <span className="text-buy">{livePrices[s]}</span>
              </span>
            ))}
          </div>
        )}
      </header>

      {/* Panneaux : P&L · Risque · Heatmap */}
      <section className="mb-6 grid gap-4 md:grid-cols-3">
        <div className="rounded-xl border border-border bg-surface p-4">
          <div className="text-xs text-muted">P&amp;L latent</div>
          <div className={`text-2xl font-bold ${(pnl?.total_pnl ?? 0) >= 0 ? 'text-buy' : 'text-sell'}`}>
            {pnl ? `${pnl.total_pnl >= 0 ? '+' : ''}${pnl.total_pnl} USDT` : '—'}
          </div>
          <div className="text-xs text-muted">
            {pnl ? `${pnl.pnl_pct}% · ${pnl.positions.length} position(s)` : ''}
          </div>
        </div>
        <div className="rounded-xl border border-border bg-surface p-4">
          <div className="text-xs text-muted">Exposition</div>
          <div className={`text-2xl font-bold ${risk && !risk.ok ? 'text-sell' : 'text-white'}`}>
            {risk ? `${risk.exposure_pct}%` : '—'}
          </div>
          <div className="text-xs text-muted">
            {risk ? `plafond ${risk.max_exposure_pct}% · ${risk.daily_signals}/${risk.max_daily_signals} signaux` : ''}
          </div>
          {risk?.breaches?.map((b) => (
            <div key={b} className="mt-1 text-[10px] text-sell">⚠ {b}</div>
          ))}
        </div>
        <div className="rounded-xl border border-border bg-surface p-4">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-xs text-muted">Heatmap 24h {heatMix ? '(multi-marchés)' : '(watchlist)'}</span>
            <button
              onClick={() => setHeatMix((v) => !v)}
              className="rounded border border-border px-2 py-0.5 text-[10px] text-muted hover:bg-background"
            >
              {heatMix ? 'Watchlist' : 'Multi-marchés'}
            </button>
          </div>
          <div className="flex flex-wrap gap-1">
            {heat.map((h) => (
              <span
                key={h.symbol}
                title={`${h.symbol} · ${h.asset_class ?? ''} · ${h.price}`}
                className={`rounded px-2 py-1 text-[11px] font-mono ${h.change_pct >= 0 ? 'bg-buy-soft text-buy' : 'bg-sell-soft text-sell'}`}
              >
                {h.symbol.split('/')[0]} {h.change_pct >= 0 ? '+' : ''}
                {h.change_pct}%
              </span>
            ))}
          </div>
        </div>
      </section>

      <section className="mb-6 space-y-3 rounded-xl border border-border bg-surface p-4">
        {/* Marchés + sessions */}
        <div className="flex flex-wrap items-center gap-2">
          <MarketSelector value={mktClass} onChange={setMktClass} />
          <SessionPicker value={session} onChange={setSession} className="ml-2" />
        </div>

        {/* Paires cliquables -> charge le chart */}
        <SymbolPicker
          symbols={symbols}
          value={asset}
          onChange={(sym) => { setAsset(sym); setSelected(null); }}
        />

        {/* Saisie libre + timeframe + génération */}
        <div className="flex flex-wrap items-end gap-3 border-t border-border/50 pt-3">
          <div>
            <label className="mb-1 block text-xs text-muted">Actif sélectionné</label>
            <input
              list="symbol-catalog"
              value={asset}
              onChange={(e) => setAsset(e.target.value.toUpperCase())}
              placeholder="BTC/USDT…"
              className="rounded-lg border border-border bg-background px-3 py-2 font-mono outline-none focus:border-accent"
            />
            <datalist id="symbol-catalog">
              {symbols.map((s) => <option key={s.symbol} value={s.symbol} />)}
            </datalist>
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted">Timeframe</label>
            <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}
              className="rounded-lg border border-border bg-background px-3 py-2 outline-none focus:border-accent">
              {TIMEFRAMES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted" title="Curseur fiabilité ↔ quantité : strict = moins de signaux mais mieux filtrés ; agressif = plus de BUY/SELL mais plus de faux signaux">
              Sévérité des filtres
            </label>
            <div className="flex gap-1">
              {[
                { id: 'strict', label: '🛡️ Strict' },
                { id: 'balanced', label: '⚖️ Équilibré' },
                { id: 'aggressive', label: '⚡ Agressif' },
              ].map((mo) => (
                <button key={mo.id} onClick={() => { api.setSignalMode(mo.id).then(() => setSigMode(mo.id)); }}
                  className={`rounded-lg border px-2.5 py-2 text-xs ${sigMode === mo.id ? 'border-accent bg-accent/10 text-white' : 'border-border text-muted hover:bg-surface'}`}>
                  {mo.label}
                </button>
              ))}
            </div>
          </div>
          <button onClick={generate} disabled={loading}
            className="rounded-lg bg-brand-gradient px-5 py-2 font-semibold text-background shadow-glow transition hover:brightness-110 disabled:opacity-50">
            {loading ? 'Analyse...' : 'Générer un signal'}
          </button>
          {error && <p className="text-sm text-sell">{error}</p>}
        </div>
        <AgentProgress active={loading} />
      </section>

      <section className="mb-6">
        <Chart asset={asset} timeframe={timeframe} signal={selected} />
      </section>

      {signals.length === 0 ? (
        <p className="text-center text-muted">Aucun signal pour le moment. Générez votre premier signal.</p>
      ) : (
        <>
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-white">
              Signaux récents <span className="text-muted">({uniqueSignals.length} actif{uniqueSignals.length > 1 ? 's' : ''})</span>
            </h2>
            <button onClick={clearHistory} className="rounded-lg border border-sell/40 px-3 py-1 text-xs text-sell hover:bg-sell/10">
              Vider l&apos;historique
            </button>
          </div>
          <motion.div
            variants={staggerContainer}
            initial="initial"
            animate="animate"
            className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3"
          >
            {visibleSignals.map((s, i) => (
              <motion.div
                key={s.id ?? i}
                variants={staggerItem}
                onClick={() => {
                  setSelected(s);
                  setAsset(s.asset);
                }}
                className={`cursor-pointer text-left transition ${selected?.id === s.id ? 'ring-2 ring-accent rounded-xl' : ''}`}
              >
                <SignalCard s={s} />
              </motion.div>
            ))}
          </motion.div>
          {uniqueSignals.length > 6 && (
            <div className="mt-4 text-center">
              <button onClick={() => setShowAll((v) => !v)} className="rounded-lg border border-border px-4 py-1.5 text-sm text-muted hover:bg-surface hover:text-white">
                {showAll ? 'Réduire' : `Voir tout (${uniqueSignals.length})`}
              </button>
            </div>
          )}
        </>
      )}

      <p className="mt-8 text-center text-[11px] text-muted">
        ⚠️ Aide à la décision, pas un conseil en investissement. Le trading comporte un risque élevé de perte.
      </p>
    </main>
  );
}
