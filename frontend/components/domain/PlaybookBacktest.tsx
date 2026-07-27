'use client';

/**
 * BACKTEST DE LA STRATÉGIE DU DESK — forex et or, plusieurs paires, plusieurs années.
 *
 * Affiche exactement ce qu'un backtest doit dire, sans enjoliver :
 * - le nombre de trades, gagnants, perdants, le taux de réussite et le R/R moyen ;
 * - le CLASSEMENT des paires par fiabilité mesurée (espérance en R) ;
 * - ce que les trades perdants ont en commun ;
 * - les LIMITES de données, parce qu'un backtest sans ses limites est une illusion.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  api,
  type PlaybookBacktest as Report,
  type PlaybookBacktestPass,
  type PlaybookPairRank,
} from '@/lib/api';
import { Button } from '@/components/ui';

function Stat({ label, value, tone }: { label: string; value: string; tone?: 'buy' | 'sell' }) {
  return (
    <div className="rounded-lg border border-border bg-background/40 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
      <div className={`text-sm font-semibold ${tone === 'buy' ? 'text-buy' : tone === 'sell' ? 'text-sell' : 'text-white'}`}>
        {value}
      </div>
    </div>
  );
}

function RankingTable({ rows }: { rows: PlaybookPairRank[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[760px] text-xs">
        <thead>
          <tr className="text-[10px] uppercase tracking-wide text-muted">
            <th className="pb-1 text-left font-medium">#</th>
            <th className="pb-1 text-left font-medium">Paire</th>
            <th className="pb-1 text-right font-medium">Trades</th>
            <th className="pb-1 text-right font-medium">Gagnants</th>
            <th className="pb-1 text-right font-medium">Perdants</th>
            <th className="pb-1 text-right font-medium">Réussite</th>
            <th className="pb-1 text-right font-medium">R/R moyen</th>
            <th className="pb-1 text-right font-medium">Espérance</th>
            <th className="pb-1 text-right font-medium">PF</th>
            <th className="pb-1 text-left font-medium">Verdict</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.symbol} className="border-t border-border/60">
              <td className="py-1.5 pr-2 text-muted">{r.rank ?? '—'}</td>
              <td className="py-1.5 pr-2 font-mono text-white/90">{r.symbol}</td>
              <td className="py-1.5 pr-2 text-right text-muted">{r.trades}</td>
              <td className="py-1.5 pr-2 text-right text-buy">{r.wins}</td>
              <td className="py-1.5 pr-2 text-right text-sell">{r.losses}</td>
              <td className="py-1.5 pr-2 text-right text-white/80">{r.win_rate}%</td>
              <td className="py-1.5 pr-2 text-right text-muted">1:{r.avg_planned_rr}</td>
              <td className={`py-1.5 pr-2 text-right ${r.expectancy_r > 0 ? 'text-buy' : 'text-sell'}`}>
                {r.expectancy_r > 0 ? '+' : ''}{r.expectancy_r} R
              </td>
              <td className="py-1.5 pr-2 text-right text-muted">{r.profit_factor ?? '∞'}</td>
              <td className="py-1.5 text-[11px] text-white/70">{r.verdict}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PassBlock({ pass, title, note }: { pass: PlaybookBacktestPass; title: string; note: string }) {
  const o = pass.overall;
  return (
    <div className="rounded-xl border border-border bg-surface p-4">
      <h3 className="text-sm font-semibold text-white">{title}</h3>
      <p className="mt-0.5 text-[11px] text-muted">{note}</p>
      <p className="mt-1 text-[11px] text-muted">
        {pass.pairs_tested} paire(s) · {pass.years_covered} an(s) d&apos;historique ·
        déclencheur évalué en {pass.entry_timeframe} · calcul {pass.duration_s} s
      </p>

      {o.trades === 0 ? (
        <p className="mt-3 rounded bg-background/60 p-3 text-xs text-muted">
          Aucun trade conforme à la stratégie sur cette passe : la cascade complète n&apos;a jamais
          été entièrement satisfaite. C&apos;est un résultat, pas une erreur.
        </p>
      ) : (
        <>
          <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-4">
            <Stat label="Trades" value={`${o.trades}`} />
            <Stat label="Gagnants" value={`${o.wins}`} tone="buy" />
            <Stat label="Perdants" value={`${o.losses}`} tone="sell" />
            <Stat label="Réussite" value={`${o.win_rate} %`} />
            <Stat label="R/R moyen planifié" value={`1:${o.avg_planned_rr}`} />
            <Stat
              label="Espérance / trade"
              value={`${o.expectancy_r > 0 ? '+' : ''}${o.expectancy_r} R`}
              tone={o.expectancy_r > 0 ? 'buy' : 'sell'}
            />
            <Stat label="Profit factor" value={`${o.profit_factor ?? '∞'}`} />
            <Stat label="Pire série" value={`${o.max_drawdown_r} R`} tone="sell" />
            <Stat label="Gain moyen gagnant" value={`${o.avg_win_r} R`} tone="buy" />
            <Stat label="Perte moyenne" value={`${o.avg_loss_r} R`} tone="sell" />
            <Stat label="Stop sécurisé (+2R)" value={`${o.secured_rate} %`} />
            <Stat label="Détention moyenne" value={`${o.avg_bars_held} bougies`} />
          </div>

          {pass.ranking.length > 0 && (
            <div className="mt-4">
              <h4 className="mb-1 text-xs font-semibold text-white">
                Classement des paires par fiabilité
              </h4>
              <p className="mb-2 text-[11px] text-muted">
                Classées sur l&apos;espérance en R — la seule mesure qui combine taux de réussite ET
                rapport gain/perte. Une paire sous {pass.min_trades} trades n&apos;est pas notée :
                on ne prétend pas savoir ce qu&apos;on n&apos;a pas mesuré.
              </p>
              <RankingTable rows={pass.ranking} />
            </div>
          )}

          {(pass.losers_profile.findings?.length ?? 0) > 0 && (
            <div className="mt-4 rounded-lg border border-sell/30 bg-sell/5 p-3">
              <h4 className="text-xs font-semibold text-white">
                Ce que les trades stoppés ont en commun ({pass.losers_profile.sample} perdants)
              </h4>
              <ul className="mt-1 space-y-0.5 text-[11px] text-muted">
                {pass.losers_profile.findings!.map((f, i) => <li key={i}>• {f}</li>)}
              </ul>
              {pass.losers_profile.avg_bars_to_stop != null && (
                <p className="mt-1 text-[11px] text-muted">
                  Un perdant est stoppé en moyenne au bout de {pass.losers_profile.avg_bars_to_stop} bougies.
                </p>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function PlaybookBacktestReport() {
  const [report, setReport] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setReport(await api.playbookBacktest());
      setError(null);
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Le backtest tourne en ARRIÈRE-PLAN : on le démarre puis on suit son avancement.
  const running = report?.run_state?.running ?? false;
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => void load(), 5_000);
    return () => clearInterval(id);
  }, [running, load]);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      await api.runPlaybookBacktest();   // démarre et rend la main aussitôt
      await load();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold text-white">
            🧪 Backtest de la stratégie — forex &amp; or
          </h2>
          <p className="text-xs text-muted">
            La cascade complète rejouée sur l&apos;historique réel, en walk-forward strict : à chaque
            bougie, seules les données déjà disponibles sont utilisées.
          </p>
        </div>
        <Button size="sm" onClick={run} loading={busy || running} disabled={running}>
          {running ? 'Backtest en cours…' : busy ? 'Démarrage…' : 'Relancer le backtest'}
        </Button>
      </div>

      {error && <p className="text-sell">{error}</p>}

      {report?.data_limits && (
        <div className="rounded-xl border border-warn/40 bg-warn-soft/30 px-4 py-3 text-[11px] text-muted">
          <strong className="text-white/90">Limites de données (mesurées, pas supposées) :</strong>{' '}
          {report.data_limits.note}
        </div>
      )}

      {!report?.available ? (
        <div className="rounded-xl border border-border bg-surface p-6 text-sm text-muted">
          {report?.note ?? 'Chargement…'}
        </div>
      ) : (
        <>
          {report.conclusion && (
            <div className="rounded-xl border border-accent/40 bg-accent/5 p-4">
              <p className="text-sm font-semibold text-white">{report.conclusion.headline}</p>
              <ul className="mt-2 space-y-1 text-[11.5px] leading-relaxed text-gray-300">
                {report.conclusion.lines.map((l, i) => <li key={i}>• {l}</li>)}
              </ul>
            </div>
          )}
          {report.scope && (
            <PassBlock
              pass={report.scope}
              title="Passe PORTÉE — déclencheur évalué en 1 h"
              note="Toute la profondeur d'historique disponible. C'est elle qui donne le recul statistique et le classement des paires."
            />
          )}
          {report.fidelity && (
            <PassBlock
              pass={report.fidelity}
              title="Passe FIDÉLITÉ — vrai déclencheur 15 min"
              note="Le déclencheur réel de la stratégie, mais sur les ~80 jours de 15 min réellement disponibles. Sert de contrôle de la passe portée."
            />
          )}
          <p className="text-[11px] text-muted">
            Hypothèse prudente appliquée partout : quand le stop et l&apos;objectif tombent dans la
            même bougie, le stop est considéré touché en premier. Un backtest ne doit jamais
            s&apos;accorder le bénéfice du doute. Résultats passés ≠ résultats futurs.
          </p>
        </>
      )}
    </section>
  );
}
