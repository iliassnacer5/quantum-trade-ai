'use client';

/**
 * Les trades du jour issus de la STRATÉGIE du desk.
 *
 * Cascade appliquée côté backend :
 *   1. Mensuel + Journalier -> tendance de fond + supports/résistances majeurs
 *   2. Journalier -> RSI14, MA20/MA50, volume, tendance VWAP, divergences RSI/MACD, Fibonacci
 *   3. 4 h -> mêmes facteurs, confirmation
 *   4. 15 min -> déclencheur d'entrée (seule unité de temps d'entrée)
 * puis R/R dans la bande 1,2–1,3 et objectif ≥ 200 pips.
 *
 * Principe d'affichage : AUCUN score technique brut. Chaque facteur porte un score de fiabilité
 * lisible de -5 à +5 (positif = argument d'achat, négatif = argument de vente), l'explication est
 * rédigée en français, et le trade se termine par un score de fiabilité global.
 *
 * La page se rafraîchit TOUTE SEULE : la stratégie est recalculée à intervalle régulier.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  api,
  type PaperExecutionReport,
  type PlaybookFactor,
  type PlaybookLayer,
  type PlaybookTrade,
  type TopTrades as TopTradesData,
} from '@/lib/api';
import { refreshLabel, useAutoRefresh } from '@/lib/useAutoRefresh';
import { Button } from '@/components/ui';

/** Recalcul complet de la stratégie toutes les 2 minutes (le cache serveur absorbe le coût). */
const REFRESH_MS = 120_000;

const LAYER_ORDER = ['monthly', 'daily', 'h4', 'm15'] as const;
const LAYER_TITLES: Record<string, string> = {
  monthly: 'Mensuel — la tendance de fond',
  daily: 'Journalier — la tendance du moment',
  h4: '4 heures — la confirmation',
  m15: '15 minutes — le moment d’entrée',
};

/** Score de fiabilité -5..+5 sous forme de pastille lisible. */
function ScoreBadge({ score, label }: { score: number; label?: string }) {
  const tone =
    score > 0 ? 'bg-buy/15 text-buy' : score < 0 ? 'bg-sell/15 text-sell' : 'bg-border text-muted';
  return (
    <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-bold ${tone}`}>
      {score > 0 ? `+${score}` : score}/5{label ? ` · ${label}` : ''}
    </span>
  );
}

function SessionBanner({ data }: { data: TopTradesData }) {
  const s = data.session;
  const tone = s.overlap
    ? 'border-buy/50 bg-buy/10 text-buy'
    : s.prime
      ? 'border-accent/50 bg-accent/10 text-accent'
      : 'border-border bg-surface text-muted';
  return (
    <div className={`rounded-xl border px-4 py-3 text-sm ${tone}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">{s.overlap ? '🔥' : s.prime ? '🎯' : '🕒'} {s.label}</span>
        <span className="text-xs opacity-80">· {s.utc_time}</span>
        {s.active_labels.length > 0 && (
          <span className="text-xs opacity-80">· sessions ouvertes : {s.active_labels.join(', ')}</span>
        )}
      </div>
      {!s.prime && s.next_window && (
        <p className="mt-1 text-xs opacity-80">
          Prochaine fenêtre à forte valeur : {s.next_window.label} dans {s.next_window.starts_in_minutes} min
          ({s.next_window.window_utc}).
        </p>
      )}
    </div>
  );
}

/** Un facteur : ce qu'il vaut, ce qu'il veut dire, et son score de fiabilité. */
function FactorRow({ f }: { f: PlaybookFactor }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-t border-border/60 py-2 first:border-t-0">
      <div className="flex items-start justify-between gap-2">
        <button onClick={() => setOpen(!open)} className="text-left text-[12px] font-semibold text-white/90 hover:text-accent">
          {f.label} <span className="font-normal text-muted">— {f.value}</span>
          <span className="ml-1 text-[10px] text-accent">{open ? '▾' : '▸'}</span>
        </button>
        {f.weight_pct > 0 ? <ScoreBadge score={f.score} label={f.reliability} />
          : <span className="shrink-0 text-[10px] text-muted">{f.reliability}</span>}
      </div>
      <p className="mt-0.5 text-[11px] text-muted">{f.reading}</p>
      {open && f.explain && (
        <p className="mt-1.5 rounded bg-background/60 p-2 text-[11px] leading-relaxed text-gray-400">
          ℹ️ {f.explain}
        </p>
      )}
    </div>
  );
}

/** Une unité de temps : sa lecture, sa fiabilité, puis ses facteurs. */
function LayerBlock({ id, layer }: { id: string; layer: PlaybookLayer }) {
  const sense = layer.bias > 0 ? 'haussière' : layer.bias < 0 ? 'baissière' : 'neutre';
  return (
    <div className="rounded-lg border border-border bg-background/40 p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-sm font-semibold text-white">{LAYER_TITLES[id] ?? layer.label}</span>
        <span className="flex items-center gap-2">
          <span className={layer.bias > 0 ? 'text-buy' : layer.bias < 0 ? 'text-sell' : 'text-muted'}>
            lecture {sense}
          </span>
          <ScoreBadge score={layer.score_5} label={layer.reliability} />
        </span>
      </div>
      <div className="mt-2">
        {layer.factors?.map((f, i) => <FactorRow key={i} f={f} />)}
      </div>
    </div>
  );
}

/** Une étape de la méthode, dépliable pour comprendre ce qu'elle vérifie. */
function ChecklistRow({ c }: { c: PlaybookTrade['checklist'][number] }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="border-t border-border/60 py-1.5 first:border-t-0">
      <button onClick={() => setOpen(!open)} className="w-full text-left">
        <span className="mr-1">{c.pass ? '✅' : '❌'}</span>
        <span className="text-[12px] text-white/85">{c.label}</span>
        {c.explain && <span className="ml-1 text-[10px] text-accent">{open ? '▾' : '▸'}</span>}
      </button>
      {open && c.explain && (
        <p className="mt-1 whitespace-pre-line rounded bg-background/60 p-2 text-[11px] leading-relaxed text-gray-400">
          {c.explain}
        </p>
      )}
    </li>
  );
}

function TradeCard({ trade, rank }: { trade: PlaybookTrade; rank: number }) {
  const [tab, setTab] = useState<'none' | 'why' | 'steps' | 'factors'>('none');
  const ready = trade.tier === 'ready';
  const buy = trade.direction === 'BUY';
  return (
    <div className={`rounded-xl border bg-surface p-4 ${ready ? 'border-buy/40' : 'border-yellow-500/30'}`}>
      <div className="flex items-center justify-between">
        <span className="font-mono font-semibold text-white">
          <span className="mr-2 text-muted">#{rank}</span>{trade.symbol}
        </span>
        <div className="flex items-center gap-1.5">
          <ScoreBadge score={trade.reliability_score} label={trade.reliability} />
          <span className={`rounded px-2 py-0.5 text-xs font-bold ${buy ? 'bg-buy/20 text-buy' : 'bg-sell/20 text-sell'}`}>
            {trade.direction}
          </span>
        </div>
      </div>

      <div className="mt-1">
        {ready ? (
          <span className="rounded bg-buy/15 px-2 py-0.5 text-[10px] font-bold text-buy">
            ✅ EXÉCUTABLE — déclencheur 15 min actif
          </span>
        ) : (
          <span className="rounded bg-yellow-500/15 px-2 py-0.5 text-[10px] font-bold text-yellow-300">
            🟡 ARMÉ — contexte validé, en attente du déclencheur 15 min
          </span>
        )}
      </div>

      {ready ? (
        <>
          <div className="mt-3 grid grid-cols-2 gap-1 text-xs text-muted">
            <span>Entrée : <span className="font-mono text-white">{trade.entry}</span></span>
            <span>R/R : <span className="text-buy">1 : {trade.risk_reward}</span></span>
            <span>Stop : <span className="font-mono text-sell">{trade.stop_loss}</span> ({trade.risk_pips} {trade.pips_label})</span>
            <span>TP1 : <span className="font-mono text-buy">{trade.take_profit_1}</span> ({trade.reward_pips})</span>
            <span>TP2 : <span className="font-mono text-white/80">{trade.take_profit_2}</span></span>
            <span>TP3 : <span className="font-mono text-white/80">{trade.take_profit_3}</span></span>
          </div>
          <p className="mt-1.5 text-[11px] text-muted">
            Stop placé sur la <span className="text-white/80">{trade.stop_basis}</span>
            {trade.horizon_days != null && <> · horizon estimé <span className="text-white/80">~{trade.horizon_days} jour(s)</span></>}
          </p>
        </>
      ) : (
        <p className="mt-3 text-xs text-muted">
          Les étapes 1 à 3 sont validées (mensuel + journalier, puis 4 h). L&apos;entrée ne se prend
          qu&apos;en 15 min : {trade.reasons[0] ?? 'déclencheur non formé'}.
        </p>
      )}

      {trade.trigger && <p className="mt-2 text-[11px] text-accent">Déclencheur 15 min : {trade.trigger}</p>}
      {(trade.levels?.major_support || trade.levels?.major_resistance) && (
        <p className="mt-1 text-[11px] text-muted">
          Niveaux majeurs — support {trade.levels.major_support ?? '—'} · résistance {trade.levels.major_resistance ?? '—'}
        </p>
      )}

      <div className="mt-3 flex flex-wrap gap-2 border-t border-border pt-2 text-xs">
        {([['why', 'Pourquoi ce choix ?'], ['steps', 'Les 4 étapes'], ['factors', 'Les indicateurs']] as const).map(
          ([key, label]) => (
            <button
              key={key}
              onClick={() => setTab(tab === key ? 'none' : key)}
              className={`rounded px-2 py-1 ${tab === key ? 'bg-accent/15 text-accent' : 'text-accent hover:underline'}`}
            >
              {label}
            </button>
          ),
        )}
      </div>

      {tab === 'why' && (
        <p className="mt-2 whitespace-pre-line rounded bg-background/60 p-3 text-[11.5px] leading-relaxed text-gray-300">
          {trade.narrative}
        </p>
      )}

      {tab === 'steps' && (
        <ol className="mt-2">
          {trade.checklist.map((c, i) => <ChecklistRow key={i} c={c} />)}
        </ol>
      )}

      {tab === 'factors' && (
        <div className="mt-2 space-y-2">
          <p className="text-[11px] text-muted">
            Chaque indicateur reçoit un score de fiabilité : <span className="text-buy">+1 à +5</span> quand
            il plaide pour un achat, <span className="text-sell">−1 à −5</span> quand il plaide pour une vente,
            0 quand il ne tranche pas. Clique sur un indicateur pour savoir ce qu&apos;il mesure.
          </p>
          {LAYER_ORDER.map((k) => {
            const layer = trade.layers?.[k];
            return layer ? <LayerBlock key={k} id={k} layer={layer} /> : null;
          })}
        </div>
      )}
    </div>
  );
}

function ExecutionReport({ report }: { report: PaperExecutionReport }) {
  return (
    <div className="rounded-xl border border-accent/40 bg-accent/5 p-4 text-sm">
      <p className="font-semibold text-white">🧪 Compte démo — {report.summary}</p>
      {report.opened.length > 0 && (
        <ul className="mt-2 space-y-1 text-xs text-muted">
          {report.opened.map((o) => (
            <li key={o.order_id} className="font-mono">
              <span className="text-white">{o.symbol}</span> {o.side.toUpperCase()} {o.qty} @ {o.entry}
              {' · '}SL <span className="text-sell">{o.stop_loss}</span>
              {' · '}TP <span className="text-buy">{o.take_profit}</span>
              {' · '}R/R 1:{o.risk_reward}
              {o.risk_amount != null && <> · risque {o.risk_amount}</>}
              {o.potential_profit != null && <> · gain visé {o.potential_profit}</>}
            </li>
          ))}
        </ul>
      )}
      {report.skipped.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-[11px] text-yellow-300/90">
          {report.skipped.map((s, i) => <li key={i}>⏭️ {s.symbol} non ouvert : {s.reason}</li>)}
        </ul>
      )}
      {report.armed_waiting.length > 0 && (
        <p className="mt-2 text-[11px] text-muted">
          En attente du déclencheur 15 min : {report.armed_waiting.map((a) => `${a.symbol} ${a.direction}`).join(', ')}.
        </p>
      )}
      <p className="mt-2 text-[11px] text-muted">
        Positions ouvertes en <strong>papier</strong> (aucun argent réel). Suivi et clôture
        automatiques au SL/TP — voir <a href="/execution" className="text-accent underline">Paper Trading</a>.
      </p>
    </div>
  );
}

export function TopTrades() {
  const [data, setData] = useState<TopTradesData | null>(null);
  const [firstLoad, setFirstLoad] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<PaperExecutionReport | null>(null);
  const [executing, setExecuting] = useState(false);

  // `refresh: true` -> la stratégie est RECALCULÉE, pas seulement relue depuis le cache du jour.
  const load = useCallback(async () => {
    try {
      setData(await api.topTrades(true, 5));
      setError(null);
    } catch (e: any) {
      setError(e.message);
      throw e;
    } finally {
      setFirstLoad(false);
    }
  }, []);

  const auto = useAutoRefresh(load, REFRESH_MS);

  useEffect(() => {
    void load();
  }, [load]);

  async function execute() {
    setExecuting(true);
    setError(null);
    try {
      setReport(await api.executePlaybook(5));
      void load();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setExecuting(false);
    }
  }

  const readyCount = data?.picks.filter((p) => p.tier === 'ready').length ?? 0;

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold text-white">🎯 Les 5 trades du jour — stratégie du desk</h2>
          <p className="text-xs text-muted">
            Mensuel + Journalier (tendance de fond &amp; niveaux majeurs) → Journalier détaillé →
            4 h → <strong className="text-white/80">entrée en 15 min uniquement</strong> · R/R entre
            1,2 et 1,3 · objectif ≥ 200 pips.
          </p>
          <p className="mt-0.5 text-[11px] text-muted">
            <span className={auto.refreshing ? 'text-accent' : ''}>🔄 Recalcul automatique</span> · {refreshLabel(auto)}
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" size="sm" onClick={auto.refreshNow} loading={auto.refreshing}>
            {auto.refreshing ? '…' : 'Recalculer maintenant'}
          </Button>
          <Button size="sm" onClick={execute} loading={executing}>
            {executing ? '…' : readyCount > 0 ? `Ouvrir en démo (${readyCount})` : 'Ouvrir en démo'}
          </Button>
        </div>
      </div>

      {data && <SessionBanner data={data} />}
      {error && <p className="text-sell">{error}</p>}
      {report && <ExecutionReport report={report} />}
      {firstLoad && <p className="text-muted">Application de la stratégie sur les 4 unités de temps…</p>}

      {data && (
        <>
          <p className="text-xs text-muted">
            {data.scanned} symbole(s) passés à la stratégie · {data.ready} exécutable(s) maintenant
          </p>
          {data.picks.length === 0 ? (
            <div className="rounded-xl border border-border bg-surface p-6 text-muted">{data.note}</div>
          ) : (
            <>
              <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
                {data.picks.map((p, i) => <TradeCard key={p.symbol} trade={p} rank={i + 1} />)}
              </div>
              <p className="text-xs text-muted">{data.note}</p>
            </>
          )}
          <p className="text-[11px] text-muted">
            Aucun trade n&apos;est garanti gagnant : ces setups sont ceux qui respectent intégralement
            la stratégie, classés par fiabilité. Aide à la décision, pas un conseil en investissement.
          </p>
        </>
      )}
    </section>
  );
}
