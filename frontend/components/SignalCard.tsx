'use client';

/**
 * Signal Card — composant central de l'UI (cf. cahier des charges §5.3).
 * Niveaux, confiance, indicateurs, multi-timeframe, vérification fiabilité + calculateur de position.
 */
import { useState } from 'react';
import { api, type Signal } from '@/lib/api';

type Verdict = { verdict: string; passed: number; total: number; checks: { label: string; pass: boolean; value: any }[]; backtest: any; interpretation?: string };

export function SignalCard({ s }: { s: Signal }) {
  const isBuy = s.direction === 'BUY';
  const isSell = s.direction === 'SELL';
  const badge = isBuy ? 'bg-buy-soft text-buy' : isSell ? 'bg-sell-soft text-sell' : 'bg-border text-muted';
  const tps = [s.take_profit_1, s.take_profit_2, s.take_profit_3].filter((t) => t != null) as number[];
  const m = s.metrics ?? {};
  // Stratégie du desk : explication rédigée + note de fiabilité -5..+5 (aucun score technique brut).
  const pb = (m.playbook ?? {}) as {
    narrative?: string; reliability_score?: number; reliability?: string;
    layers?: Record<string, { label: string; score_5: number; reliability: string; bias: number;
      factors: { label: string; value: string; reading: string; score: number; weight_pct: number; reliability: string }[] }>;
  };
  const reliability = pb.reliability_score ?? 0;
  // À défaut de narration (playbook indisponible), on nettoie la justification de ses chiffres
  // d'arbitrage : la carte de prédiction ne montre que du texte compréhensible.
  const explanation =
    pb.narrative ??
    (s.rationale ?? '')
      .split('\n')
      .filter((l) => !/score\s*[+-]?\d|consensus\s*\d|ADX\s*\d/i.test(l))
      .join('\n')
      .trim();

  const [verdict, setVerdict] = useState<Verdict | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [vErr, setVErr] = useState<string | null>(null);

  const [capital, setCapital] = useState(10000);
  const [riskPct, setRiskPct] = useState(1);

  const [placing, setPlacing] = useState(false);
  const [tradeMsg, setTradeMsg] = useState<string | null>(null);
  const [tradeErr, setTradeErr] = useState<string | null>(null);

  async function verify() {
    setVerifying(true);
    setVErr(null);
    try {
      setVerdict(await api.verifySignal(s));
    } catch (e: any) {
      setVErr(e.message?.includes('402') ? 'Vérification réservée au plan Pro+' : e.message ?? 'Erreur');
    } finally {
      setVerifying(false);
    }
  }

  // Calculateur de position (math pure, instantané).
  const riskPerUnit = Math.abs(s.entry - s.stop_loss);
  const riskAmount = (capital * riskPct) / 100;
  const size = riskPerUnit > 0 ? riskAmount / riskPerUnit : 0;
  const positionValue = size * s.entry;
  const tradable = s.direction !== 'HOLD' && riskPerUnit > 0;

  // Lance un trade PAPIER à partir de ce signal (symbole + direction + SL/TP + taille calculée).
  async function tradePaper() {
    setPlacing(true);
    setTradeErr(null);
    setTradeMsg(null);
    try {
      const conns = await api.brokers();
      const paper = conns.find((c) => c.mode === 'paper') ?? (await api.connectBroker('paper', 'paper'));
      const qty = Number(size.toFixed(6));
      if (!qty || qty <= 0) throw new Error('Taille de position nulle — ajuste capital/risque.');
      await api.placeOrder(paper.id, s.asset, s.direction === 'BUY' ? 'buy' : 'sell', qty, s.stop_loss, s.take_profit_1);
      setTradeMsg(`✅ Trade papier ouvert : ${s.direction} ${qty} ${s.asset}. Suivi dans Paper Trading / Portefeuille.`);
    } catch (e: any) {
      setTradeErr(e.message ?? 'Erreur lors de l’ouverture du trade');
    } finally {
      setPlacing(false);
    }
  }

  const vStyle: Record<string, string> = {
    strong: 'border-buy/40 bg-buy/10 text-buy',
    moderate: 'border-yellow-500/40 bg-yellow-500/10 text-yellow-200',
    weak: 'border-sell/40 bg-sell/10 text-sell',
    skip: 'border-border bg-surface text-muted',
  };
  const vLabel: Record<string, string> = {
    strong: '✅ Signal solide', moderate: '⚠️ Signal moyen — prudence', weak: '🔴 Signal faible — éviter', skip: '⏸️ HOLD — pas de trade',
  };

  return (
    <div className="w-full max-w-lg rounded-xl border border-border bg-surface p-5 shadow-lg">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="font-mono text-lg font-semibold">{s.asset}</span>
          {s.timeframe && (
            <span className="rounded bg-background px-2 py-0.5 text-[11px] font-medium text-muted" title="Unité de temps de cette prédiction">
              ⏱ {s.timeframe}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {s.high_conviction && (
            <span className="rounded-md bg-buy/20 px-2 py-1 text-[10px] font-bold text-buy"
              title="Setup complet de la stratégie : tendance validée, entrée déclenchée, stop et objectifs posés sur des niveaux">
              ★ HAUTE CONVICTION
            </span>
          )}
          <span className={`rounded-md px-3 py-1 text-sm font-bold ${badge}`}>{s.direction}</span>
        </div>
      </div>
      {/* HOLD utile : montre le biais sous-jacent bloqué + ce qui manque pour valider */}
      {s.direction === 'HOLD' && m.blocked_direction && (
        <div className="mt-2 rounded-lg border border-yellow-500/30 bg-yellow-500/10 px-3 py-2 text-xs text-yellow-200">
          Signal potentiel : <b className={m.blocked_direction === 'BUY' ? 'text-buy' : 'text-sell'}>{m.blocked_direction}</b>{' '}
          — bloqué par les filtres de fiabilité (détail dans l&apos;analyse). Mode moins strict = plus de signaux, plus de risque.
        </div>
      )}

      {s.mtf && s.mtf.total > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-muted">
          <span>Multi-timeframe :</span>
          {Object.entries(s.mtf.details).map(([tf, dir]) => (
            <span key={tf} className={`rounded px-1.5 py-0.5 ${dir === 'BUY' ? 'bg-buy/15 text-buy' : dir === 'SELL' ? 'bg-sell/15 text-sell' : 'bg-border text-muted'}`}>
              {tf} {dir}
            </span>
          ))}
          <span className="ml-1">({s.mtf.aligned}/{s.mtf.total} alignés)</span>
        </div>
      )}

      <div className="mt-4 grid grid-cols-2 gap-3 text-sm">
        <Field label="Entrée" value={s.entry} />
        <Field label="Stop-Loss" value={s.stop_loss} className="text-sell" />
        {tps[0] != null && <Field label="TP 1" value={tps[0]} className="text-buy" />}
        {tps[1] != null && <Field label="TP 2" value={tps[1]} className="text-buy" />}
        {tps[2] != null && <Field label="TP 3" value={tps[2]} className="text-buy" />}
        <Field label="R/R" value={`1 : ${s.risk_reward}`} />
      </div>

      {/* Fiabilité : une SEULE note lisible, de -5 à +5. Aucun score technique brut. */}
      <div className="mt-4 rounded-lg border border-border bg-background/40 px-3 py-2">
        <div className="flex items-center justify-between">
          <span className="text-xs text-muted">Fiabilité du trade</span>
          <span className={`rounded px-2 py-0.5 text-sm font-bold ${
            reliability > 0 ? 'bg-buy/15 text-buy' : reliability < 0 ? 'bg-sell/15 text-sell' : 'bg-border text-muted'
          }`}>
            {reliability > 0 ? `+${reliability}` : reliability}/5{pb.reliability ? ` · ${pb.reliability}` : ''}
          </span>
        </div>
        <p className="mt-1 text-[10px] text-muted">
          De +1 à +5 pour un achat, de −1 à −5 pour une vente, 0 quand aucun trade n&apos;est justifié.
        </p>
      </div>

      {s.risk_warning && (
        <p className="mt-3 rounded-lg border border-yellow-500/30 bg-yellow-500/10 px-3 py-2 text-xs text-yellow-200">{s.risk_warning}</p>
      )}

      {/* Vérification de fiabilité */}
      <div className="mt-4">
        <button onClick={verify} disabled={verifying}
          className="w-full rounded-lg border border-accent/50 bg-accent/10 px-3 py-2 text-sm font-medium text-white hover:bg-accent/20 disabled:opacity-50">
          {verifying ? 'Backtest en cours…' : '🔎 Vérifier ce signal (backtest + checklist)'}
        </button>
        {vErr && <p className="mt-2 text-xs text-sell">{vErr}</p>}
        {verdict && (
          <div className={`mt-2 rounded-lg border p-3 ${vStyle[verdict.verdict]}`}>
            <div className="flex items-center justify-between text-sm font-semibold">
              <span>{vLabel[verdict.verdict]}</span>
              <span>{verdict.passed}/{verdict.total} critères</span>
            </div>
            {verdict.interpretation && <p className="mt-1 text-[11px] opacity-95">{verdict.interpretation}</p>}
            {verdict.backtest && (
              <p className="mt-1 text-[10px] opacity-70">
                (Détail backtest : {verdict.backtest.trades} trades · {verdict.backtest.win_rate}% · PF {verdict.backtest.profit_factor} · DD {verdict.backtest.max_drawdown_pct}%)
              </p>
            )}
            <ul className="mt-2 space-y-0.5 text-[11px]">
              {verdict.checks.map((c, i) => (
                <li key={i} className="flex items-center justify-between gap-2">
                  <span>{c.pass ? '✓' : '✗'} {c.label}</span>
                  <span className="font-mono opacity-80">{String(c.value)}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* Calculateur de position */}
      {tradable && (
        <div className="mt-4 rounded-lg border border-border bg-background/40 p-3">
          <p className="mb-2 text-xs font-semibold text-white">Calculateur de position</p>
          <div className="flex flex-wrap items-end gap-3 text-xs">
            <label className="text-muted">Capital ($)
              <input type="number" value={capital} onChange={(e) => setCapital(Math.max(0, +e.target.value))}
                className="mt-1 block w-28 rounded border border-border bg-surface px-2 py-1 font-mono text-white" />
            </label>
            <label className="text-muted">Risque (%)
              <input type="number" step="0.5" value={riskPct} onChange={(e) => setRiskPct(Math.max(0, +e.target.value))}
                className="mt-1 block w-20 rounded border border-border bg-surface px-2 py-1 font-mono text-white" />
            </label>
          </div>
          <div className="mt-2 grid grid-cols-2 gap-1 text-[11px]">
            <Stat label="Montant risqué" value={`${riskAmount.toFixed(2)} $`} />
            <Stat label="Taille position" value={size.toFixed(4)} />
            <Stat label="Valeur position" value={`${positionValue.toFixed(2)} $`} />
            <Stat label="Risque/unité" value={riskPerUnit.toFixed(4)} />
          </div>
          <p className="mt-1 text-[10px] text-muted">Tu risques {riskAmount.toFixed(0)} $ ({riskPct}% du capital) si le stop est touché.</p>

          {/* Ouvrir le trade directement en PAPIER (sans ressaisir dans Paper Trading). */}
          <button onClick={tradePaper} disabled={placing}
            className={`mt-3 w-full rounded-lg px-3 py-2 text-sm font-medium text-white disabled:opacity-50 ${s.direction === 'BUY' ? 'bg-buy' : 'bg-sell'}`}>
            {placing ? 'Ouverture…' : `📈 Trader en paper (${s.direction} ${size.toFixed(4)} ${s.asset.split('/')[0]})`}
          </button>
          {tradeMsg && <p className="mt-2 text-xs text-buy">{tradeMsg} <a href="/wallet" className="underline">Voir le portefeuille →</a></p>}
          {tradeErr && <p className="mt-2 text-xs text-sell">{tradeErr}</p>}
        </div>
      )}

      {Object.keys(m).length > 0 && (
        <div className="mt-4">
          <p className="mb-2 text-xs font-semibold text-white">Indicateurs techniques</p>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-3">
            {m.trend && <Metric label="Tendance" value={m.trend} />}
            {m.rsi != null && <Metric label="RSI(14)" value={`${m.rsi} (${m.rsi_state ?? ''})`} tone={m.rsi < 30 ? 'buy' : m.rsi > 70 ? 'sell' : ''} />}
            {m.macd && <Metric label="MACD" value={m.macd.state} tone={m.macd.state === 'haussier' ? 'buy' : 'sell'} />}
            {m.adx != null && <Metric label="ADX" value={`${m.adx} (${m.adx_state ?? ''})`} />}
            {m.stochastic && <Metric label="Stoch %K" value={`${m.stochastic.k} (${m.stochastic.state})`} />}
            {m.ema20 != null && <Metric label="EMA20/50" value={`${m.ema20} / ${m.ema50}`} />}
            {m.bollinger && <Metric label="Bollinger" value={m.bollinger.position} />}
            {m.atr_pct != null && <Metric label="Volatilité (ATR)" value={`${m.atr_pct}%`} />}
            {m.vs_vwap && <Metric label="VWAP" value={m.vs_vwap} />}
            {m.obv_trend && <Metric label="OBV" value={m.obv_trend} />}
            {m.support != null && <Metric label="Support" value={m.support} tone="buy" />}
            {m.resistance != null && <Metric label="Résistance" value={m.resistance} tone="sell" />}
          </div>
        </div>
      )}

      {/* Pourquoi cette décision : explication rédigée, argument par argument. */}
      <details className="mt-4" open>
        <summary className="cursor-pointer text-xs font-semibold text-white">
          Pourquoi {s.direction} ? — explication complète
        </summary>
        <p className="mt-2 whitespace-pre-line text-[11.5px] leading-relaxed text-gray-300">{explanation}</p>
      </details>

      {/* Score de fiabilité de chaque indicateur, unité de temps par unité de temps. */}
      {pb.layers && (
        <details className="mt-3">
          <summary className="cursor-pointer text-xs font-semibold text-white">
            Score de fiabilité de chaque indicateur
          </summary>
          <p className="mt-1 text-[10px] text-muted">
            +1 à +5 = argument d&apos;achat · −1 à −5 = argument de vente · 0 = ne tranche pas.
          </p>
          <div className="mt-2 space-y-2">
            {(['monthly', 'daily', 'h4', 'm15'] as const).map((k) => {
              const layer = pb.layers?.[k];
              if (!layer) return null;
              return (
                <div key={k}>
                  <div className="flex items-center justify-between text-[11px]">
                    <span className="font-semibold capitalize text-white/85">{layer.label}</span>
                    <span className={layer.score_5 > 0 ? 'text-buy' : layer.score_5 < 0 ? 'text-sell' : 'text-muted'}>
                      {layer.score_5 > 0 ? '+' : ''}{layer.score_5}/5 · {layer.reliability}
                    </span>
                  </div>
                  <ul className="mt-0.5 space-y-0.5">
                    {layer.factors.filter((f) => f.weight_pct > 0).map((f, i) => (
                      <li key={i} className="flex items-start justify-between gap-2 text-[11px]">
                        <span className="text-muted">{f.label} <span className="text-white/70">({f.value})</span></span>
                        <span className={`shrink-0 font-semibold ${f.score > 0 ? 'text-buy' : f.score < 0 ? 'text-sell' : 'text-muted'}`}>
                          {f.score > 0 ? '+' : ''}{f.score}/5
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              );
            })}
          </div>
        </details>
      )}

      {s.id && (
        <a href={`/signal/${s.id}`}
          className="mt-3 block w-full rounded-lg border border-accent/40 px-3 py-2 text-center text-sm text-accent hover:bg-accent/10">
          🔍 Consulter la prédiction complète (pourquoi {s.direction} ?) →
        </a>
      )}
      <p className="mt-3 text-[10px] text-muted">Timeframe : {s.timeframe} · Aide à la décision, pas un conseil en investissement.</p>
    </div>
  );
}

function Field({ label, value, className = '' }: { label: string; value: string | number; className?: string }) {
  return (
    <div>
      <div className="text-xs text-muted">{label}</div>
      <div className={`font-mono ${className}`}>{value}</div>
    </div>
  );
}

function Metric({ label, value, tone = '' }: { label: string; value: string | number; tone?: string }) {
  const color = tone === 'buy' ? 'text-buy' : tone === 'sell' ? 'text-sell' : 'text-white';
  return (
    <div className="flex justify-between gap-2 border-b border-border/40 py-0.5">
      <span className="text-muted">{label}</span>
      <span className={`font-mono ${color}`}>{value}</span>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-muted">{label}</span>
      <span className="font-mono text-white">{value}</span>
    </div>
  );
}
