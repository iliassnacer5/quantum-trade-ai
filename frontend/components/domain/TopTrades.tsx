'use client';

/**
 * Les trades du jour issus de la STRATÉGIE du desk.
 *
 * Trois étapes, côté backend, appliquées à tous les marchés (forex, métaux, indices, actions,
 * crypto) :
 *   1. TENDANCE — six indicateurs (EMA 50/200, structure HH/HL, SuperTrend, MACD, RSI, volume) sur
 *      D1, 4 h, 1 h et 15 min. L'accord du 4 h et du 1 h suffit à la valider (le journalier pèse
 *      dans le score mais n'est pas obligatoire). Une fois validée, elle est FIGÉE.
 *   2. ENTRÉE — en 15 min, autorisée dès qu'au moins trois confirmations pondérées se rejoignent
 *      (zones d'offre/demande, cassure de structure, figures, supports/résistances, Fibonacci…).
 *   3. SORTIES — stop posé sur le niveau qui invaliderait le scénario, objectif devant le premier
 *      obstacle réel et d'au moins 50 pips, avec un R/R entre 1:2 et 1:3.
 *
 * Principe d'affichage : AUCUN score technique brut. Chaque facteur porte un score de fiabilité
 * lisible de -5 à +5 (positif = argument d'achat, négatif = argument de vente), l'explication est
 * rédigée en français, et le trade se termine par un score de fiabilité global.
 *
 * AUCUN CLIC N'EST NÉCESSAIRE :
 * - la page lit un instantané pré-calculé par le serveur -> elle se rafraîchit toutes les 10 s
 *   sans jamais attendre un recalcul complet ;
 * - les setups « armés » sont ouverts AUTOMATIQUEMENT en compte démo dès que leur déclencheur
 *   15 min se forme. Les boutons ne servent qu'à forcer la main.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  api,
  type AutoEntryStatus,
  type PaperExecutionReport,
  type PlaybookFactor,
  type PlaybookLayer,
  type PlaybookTrade,
  type TopTrades as TopTradesData,
} from '@/lib/api';
import { refreshLabel, useAutoRefresh } from '@/lib/useAutoRefresh';
import { Button } from '@/components/ui';

/** L'instantané est déjà prêt côté serveur : on peut donc relire toutes les 10 secondes. */
const REFRESH_MS = 10_000;

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

function CardTradeButton({ symbol, direction }: { symbol: string; direction: 'BUY' | 'SELL' | 'NO_TRADE' }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<PaperExecutionReport | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function run() {
    setBusy(true);
    setErr(null);
    setResult(null);
    try {
      // La stratégie est RECALCULÉE côté serveur au moment du clic : les niveaux affichés dans la
      // carte peuvent avoir quelques secondes ; on n'ouvre jamais sur des chiffres périmés.
      setResult(await api.executePlaybookSymbol(symbol));
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  const opened = result?.opened[0];
  return (
    <div className="mt-3 border-t border-border pt-3">
      <Button
        size="sm"
        variant={direction === 'BUY' ? 'buy' : direction === 'SELL' ? 'sell' : 'secondary'}
        onClick={run}
        loading={busy}
        className="w-full"
      >
        {busy ? 'Ouverture…' : `📈 Trader en démo — ${symbol}`}
      </Button>
      {err && <p className="mt-1.5 text-[11px] text-sell">{err}</p>}
      {opened && (
        <p className="mt-1.5 text-[11px] text-buy">
          ✅ Ouvert : {opened.side.toUpperCase()} {opened.qty} @ {opened.entry} · SL {opened.stop_loss} ·
          TP {opened.take_profit} · R/R 1:{opened.risk_reward}.{' '}
          <a href="/execution" className="underline">Voir dans Paper Trading →</a>
        </p>
      )}
      {result && !opened && (
        <p className="mt-1.5 text-[11px] text-yellow-300/90">
          ⏭️ {result.summary}
        </p>
      )}
    </div>
  );
}

function TradeCard({ trade, rank, autoEntry }: { trade: PlaybookTrade; rank: number; autoEntry: boolean }) {
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
          {/* Un setup ARMÉ n'a pas de note de TRADE (il n'y a pas de trade) : on affiche alors la
              fiabilité de son CONTEXTE, qui est ce qui a réellement été validé. */}
          {ready ? (
            <ScoreBadge score={trade.reliability_score} label={trade.reliability} />
          ) : (
            <ScoreBadge score={trade.context_reliability} label={`contexte ${trade.context_reliability_label}`} />
          )}
          <span className={`rounded px-2 py-0.5 text-xs font-bold ${buy ? 'bg-buy/20 text-buy' : 'bg-sell/20 text-sell'}`}>
            {trade.direction}
          </span>
        </div>
      </div>

      <div className="mt-1 flex flex-wrap gap-1">
        {ready ? (
          <span className="rounded bg-buy/15 px-2 py-0.5 text-[10px] font-bold text-buy">
            ✅ EXÉCUTABLE — déclencheur 15 min actif
          </span>
        ) : (
          <span className="rounded bg-yellow-500/15 px-2 py-0.5 text-[10px] font-bold text-yellow-300">
            {autoEntry
              ? '🟡 ARMÉ — entrée AUTOMATIQUE dès le déclencheur 15 min'
              : '🟡 ARMÉ — contexte validé, en attente du déclencheur 15 min'}
          </span>
        )}
        {trade.edge && trade.edge.trades > 0 && (
          <span
            title={trade.edge.status}
            className={`rounded px-2 py-0.5 text-[10px] font-bold ${
              trade.edge.score > 0 ? 'bg-buy/10 text-buy' : 'bg-sell/10 text-sell'
            }`}
          >
            📊 {trade.edge.win_rate}% sur {trade.edge.trades} trades rejoués
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
            Stop sur la <span className="text-white/80">{trade.stop_basis}</span>
            {trade.horizon_label && <> · horizon estimé <span className="text-white/80">{trade.horizon_label}</span></>}
          </p>
          <p className="mt-0.5 text-[11px] text-muted">
            Objectif : <span className="text-white/80">{trade.target_basis}</span>
            {trade.target_level != null && (
              <> · niveau 1 h à <span className="font-mono text-white/80">{trade.target_level}</span></>
            )}
          </p>
        </>
      ) : (
        <p className="mt-3 text-xs text-muted">
          Les étapes 1 à 3 sont validées (mensuel + journalier, puis 4 h). L&apos;entrée ne se prend
          qu&apos;en 15 min : {trade.reasons[0] ?? 'déclencheur non formé'}.
          {autoEntry && (
            <> <span className="text-yellow-300">
              Tu n&apos;as rien à faire : la position s&apos;ouvrira toute seule en démo à la seconde
              où le déclencheur se formera.
            </span></>
          )}
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

      <CardTradeButton symbol={trade.symbol} direction={trade.direction} />
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

/** Bandeau d'auto-entrée : ce que le robot surveille, et ce qu'il a ouvert pendant ton absence. */
function AutoEntryBanner({ status, onReset }: { status: AutoEntryStatus; onReset: () => void }) {
  const [resetting, setResetting] = useState(false);
  const [done, setDone] = useState<string | null>(null);

  async function reset() {
    // Action destructive sur le journal DÉMO : on demande confirmation, et on dit exactement ce
    // qui va être touché plutôt qu'un « êtes-vous sûr ? » qui n'informe personne.
    const ok = window.confirm(
      'Remettre l’auto-entrée à zéro ?\n\n' +
      '• les positions DÉMO encore ouvertes seront neutralisées (issue « reset », P&L à zéro, ' +
      'exclues des statistiques) ;\n' +
      '• l’historique des entrées automatiques sera effacé, ce qui remet aussi le délai ' +
      'anti-doublon à zéro ;\n' +
      '• les trades déjà clôturés sont CONSERVÉS, et aucune position réelle n’est touchée.',
    );
    if (!ok) return;
    setResetting(true);
    try {
      const res = await api.resetAutoEntry();
      setDone(res.note);
      onReset();
    } catch (e: any) {
      setDone(`Échec de la remise à zéro : ${e.message}`);
    } finally {
      setResetting(false);
    }
  }

  if (!status.enabled) {
    return (
      <div className="rounded-xl border border-border bg-surface px-4 py-2 text-xs text-muted">
        🤖 Auto-entrée désactivée — les setups armés attendent une ouverture manuelle.
      </div>
    );
  }
  return (
    <div className="rounded-xl border border-accent/40 bg-accent/5 px-4 py-3 text-sm">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="font-semibold text-accent">
          🤖 Auto-entrée active — compte DÉMO · vérification toutes les {status.interval_seconds} s
        </p>
        <Button size="sm" variant="danger" onClick={reset} loading={resetting}>
          Remettre à zéro
        </Button>
      </div>
      <p className="mt-0.5 text-[11px] text-muted">
        Tu n&apos;as rien à cliquer : dès qu&apos;un setup armé voit son déclencheur 15 min se former,
        la position est ouverte automatiquement avec son stop et son objectif. Aucun argent réel
        n&apos;est engagé.
      </p>
      <p className="mt-0.5 text-[11px] text-muted">
        {status.pair_gating === false
          ? 'Aucun verdict de paire ne peut s’y opposer : tout déclencheur formé part.'
          : 'Filtre de verdict actif : seules les paires 🟢 sont auto-tradées.'}
        {status.cooldown_min
          ? ` Un même symbole/sens ne se reprend pas avant ${status.cooldown_min} min (anti-doublon).`
          : ''}
      </p>
      {status.watching.length > 0 && (
        <p className="mt-1 text-[11px] text-muted">
          Sous surveillance : {status.watching.map((w) => `${w.symbol} ${w.direction}`).join(' · ')}
        </p>
      )}
      {/* Exécutable à l'écran mais refusé au dernier passage : sans cette ligne, ça ressemble à
          une panne du robot alors qu'un garde-fou (portefeuille, corrélation, anti-doublon) a
          fait exactement ce qu'on lui a demandé. */}
      {(status.blocked?.length ?? 0) > 0 && (
        <div className="mt-1.5 rounded border border-warn/30 bg-warn-soft/20 px-2 py-1.5">
          <p className="text-[11px] font-medium text-white/85">
            Prêt(s) mais non ouvert(s) au dernier passage :
          </p>
          <ul className="mt-0.5 space-y-0.5 text-[11px] text-muted">
            {status.blocked!.map((b, i) => (
              <li key={i}>{b.symbol} — {b.reason}</li>
            ))}
          </ul>
        </div>
      )}
      {status.recent.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-[11px] text-buy">
          {status.recent.slice(0, 4).map((e) => <li key={e.id}>{e.message}</li>)}
        </ul>
      )}
      {done && <p className="mt-2 text-[11px] leading-relaxed text-white/80">{done}</p>}
    </div>
  );
}

export function TopTrades() {
  const [data, setData] = useState<TopTradesData | null>(null);
  const [autoEntry, setAutoEntry] = useState<AutoEntryStatus | null>(null);
  const [firstLoad, setFirstLoad] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<PaperExecutionReport | null>(null);
  const [executing, setExecuting] = useState(false);
  const [recomputing, setRecomputing] = useState(false);

  // Lecture de l'INSTANTANÉ déjà calculé par le serveur (pas de `refresh`) : la réponse arrive en
  // quelques millisecondes, ce qui permet de relire toutes les 10 s sans jamais bloquer la page.
  const load = useCallback(async () => {
    try {
      const [trades, status] = await Promise.all([api.topTrades(false, 0), api.autoEntry()]);
      setData(trades);
      setAutoEntry(status);
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

  /** Force un RECALCUL complet côté serveur (le rafraîchissement normal ne fait que relire). */
  async function recomputeNow() {
    setRecomputing(true);
    setError(null);
    try {
      setData(await api.topTrades(true, 0));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setRecomputing(false);
    }
  }

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
  const autoOn = Boolean(autoEntry?.enabled ?? data?.auto_entry);

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold text-white">
            🎯 Trades du jour — stratégie du desk
            {data?.picks?.length ? <span className="ml-2 text-sm font-normal text-muted">
              {data.picks.length} setup{data.picks.length > 1 ? 's' : ''} conforme{data.picks.length > 1 ? 's' : ''}
            </span> : null}
          </h2>
          <p className="text-xs text-muted">
            Tendance mesurée sur D1, 4 h, 1 h et 15 min puis figée →{' '}
            <strong className="text-white/80">entrée en 15 min uniquement</strong>, sur au moins
            trois confirmations pondérées → stop sur le niveau qui invalide le scénario, objectifs
            devant le premier obstacle réel · R/R entre 1:2 et 1:3.
          </p>
          <p className="mt-0.5 text-[11px] text-muted">
            La liste n’est plus plafonnée : TOUS les setups conformes sont affichés, classés du plus
            fiable au moins fiable. La stratégie ne dit pas « les cinq meilleurs », elle dit « ceux
            qui remplissent les trois étapes ».
          </p>
          <p className="mt-0.5 text-[11px] text-muted">
            <span className={auto.refreshing ? 'text-accent' : ''}>🔄 Mise à jour automatique</span> ·{' '}
            {refreshLabel(auto)}
            {data?.age_seconds != null && (
              <span className={data.stale ? 'text-sell' : ''}>
                {' '}· stratégie calculée il y a {Math.round(data.age_seconds)} s
                {data.stale && ' (donnée périmée)'}
              </span>
            )}
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" size="sm" onClick={recomputeNow} loading={recomputing}>
            {recomputing ? '…' : 'Recalculer maintenant'}
          </Button>
          <Button size="sm" onClick={execute} loading={executing}>
            {executing ? '…' : readyCount > 0 ? `Ouvrir en démo (${readyCount})` : 'Ouvrir en démo'}
          </Button>
        </div>
      </div>

      {autoEntry && <AutoEntryBanner status={autoEntry} onReset={() => void load()} />}
      {data && <SessionBanner data={data} />}
      {error && <p className="text-sell">{error}</p>}
      {report && <ExecutionReport report={report} />}
      {firstLoad && <p className="text-muted">Application de la stratégie sur les 4 unités de temps…</p>}

      {data && (
        <>
          <p className="text-xs text-muted">
            {data.scanned} symbole(s) passés à la stratégie · {data.ready} exécutable(s) maintenant
            {data.armed != null && <> · {data.armed} armé(s)</>}
          </p>
          {data.picks.length === 0 ? (
            <div className="rounded-xl border border-border bg-surface p-6 text-muted">{data.note}</div>
          ) : (
            <>
              <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
                {data.picks.map((p, i) => (
                  <TradeCard key={p.symbol} trade={p} rank={p.rank ?? i + 1} autoEntry={autoOn} />
                ))}
              </div>
              <p className="text-xs text-muted">{data.note}</p>
            </>
          )}
          <p className="text-[11px] text-muted">
            Aucun trade n&apos;est garanti gagnant : ces setups sont ceux qui respectent intégralement
            la stratégie, classés par fiabilité mesurée sur l&apos;historique rejoué. Aide à la
            décision, pas un conseil en investissement.
          </p>
        </>
      )}
    </section>
  );
}
