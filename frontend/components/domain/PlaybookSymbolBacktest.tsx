'use client';

/**
 * BACKTEST DE LA STRATÉGIE DU DESK SUR UN SEUL INSTRUMENT.
 *
 * À ne pas confondre avec le MOTEUR de backtest de la même page, qui teste une configuration
 * générique (capital, risque, agents LLM). Ici c'est LA stratégie — tendance multi-unités de temps,
 * confluence d'entrée, sorties posées sur des niveaux — rejouée telle qu'elle trade, en
 * walk-forward strict, sur le symbole choisi.
 *
 * Le backtest tourne en ARRIÈRE-PLAN : il appelle `playbook.build` des milliers de fois et
 * bloquerait la requête HTTP. On le démarre, puis on suit son avancement.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  api,
  type PlaybookBacktestMarkets,
  type PlaybookBacktestMetrics,
  type PlaybookSymbolBacktest as Report,
} from '@/lib/api';
import { Button, Card } from '@/components/ui';

function Stat({ label, value, tone, help }: {
  label: string; value: string; tone?: 'buy' | 'sell'; help?: string;
}) {
  return (
    <div className="rounded-lg border border-border bg-background/40 px-3 py-2" title={help}>
      <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
      <div className={`text-sm font-semibold ${
        tone === 'buy' ? 'text-buy' : tone === 'sell' ? 'text-sell' : 'text-white'
      }`}>
        {value}
      </div>
    </div>
  );
}

/** Ventilation (par déclencheur, par session, par sens) — les lignes sous le seuil d'échantillon
 *  sont montrées mais signalées : une espérance sur 2 trades ne conclut rien. */
function Breakdown({ title, rows, minTrades }: {
  title: string; rows: Record<string, PlaybookBacktestMetrics>; minTrades: number;
}) {
  const entries = Object.entries(rows).sort((a, b) => b[1].expectancy_r - a[1].expectancy_r);
  if (entries.length === 0) return null;
  return (
    <div>
      <h4 className="mb-1 text-xs font-semibold text-white">{title}</h4>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[420px] text-[11px]">
          <thead>
            <tr className="text-[10px] uppercase tracking-wide text-muted">
              <th className="pb-1 text-left font-medium">Catégorie</th>
              <th className="pb-1 text-right font-medium">Trades</th>
              <th className="pb-1 text-right font-medium">Réussite</th>
              <th className="pb-1 text-right font-medium">Espérance</th>
              <th className="pb-1 text-right font-medium">PF</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(([key, m]) => (
              <tr key={key} className="border-t border-border/40">
                <td className="py-1 pr-2 text-white/90">
                  {key}
                  {m.trades < minTrades && (
                    <span className="ml-1 text-[10px] text-muted">(échantillon court)</span>
                  )}
                </td>
                <td className="py-1 pr-2 text-right text-muted">{m.trades}</td>
                <td className="py-1 pr-2 text-right text-white/80">{m.win_rate}%</td>
                <td className={`py-1 pr-2 text-right ${m.expectancy_r > 0 ? 'text-buy' : 'text-sell'}`}>
                  {m.expectancy_r > 0 ? '+' : ''}{m.expectancy_r} R
                </td>
                <td className="py-1 text-right text-muted">{m.profit_factor ?? '∞'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function PlaybookSymbolBacktest() {
  const [markets, setMarkets] = useState<PlaybookBacktestMarkets | null>(null);
  const [market, setMarket] = useState('forex');
  const [symbol, setSymbol] = useState('EUR/USD');
  const [entryTf, setEntryTf] = useState('1h');
  const [step, setStep] = useState(4);
  const [report, setReport] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.playbookBacktestMarkets()
      .then(setMarkets)
      .catch((e) => setError(e.message));
  }, []);

  // Changer de marché ne doit pas laisser un symbole qui n'y appartient pas.
  const symbols = markets?.markets.find((m) => m.market === market)?.symbols ?? [];
  useEffect(() => {
    if (symbols.length && !symbols.includes(symbol)) setSymbol(symbols[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [market, markets]);

  const load = useCallback(async () => {
    try {
      setReport(await api.playbookSymbolBacktest(symbol, entryTf));
      setError(null);
    } catch (e: any) {
      setError(e.message);
    }
  }, [symbol, entryTf]);

  // Dès qu'on change de symbole ou d'unité d'entrée, on affiche le dernier résultat connu POUR CE
  // couple — jamais celui du précédent, qui donnerait à lire des chiffres pour le mauvais actif.
  useEffect(() => { setReport(null); void load(); }, [load]);

  const running = report?.run_state?.running ?? false;
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => void load(), 3_000);
    return () => clearInterval(id);
  }, [running, load]);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      await api.runPlaybookSymbolBacktest(symbol, entryTf, step);
      await load();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const o = report?.overall;
  const tfNote = markets?.entry_timeframes.find((t) => t.tf === entryTf);

  return (
    <Card className="space-y-4 p-6">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold text-white">
            🎯 Backtester LA STRATÉGIE sur un instrument
          </h2>
          <p className="mt-0.5 max-w-3xl text-xs leading-relaxed text-muted">
            La stratégie du desk telle qu&apos;elle trade — tendance multi-unités de temps,
            confluence d&apos;entrée en 15 min, stop et objectif posés sur des niveaux — rejouée sur
            un seul instrument, en walk-forward strict. Mêmes réglages que la production et que le
            backtest hebdomadaire : les chiffres sont donc directement comparables.
          </p>
        </div>
      </div>

      <div className="flex flex-wrap items-end gap-4">
        <div>
          <label className="mb-1 block text-xs text-muted">Marché</label>
          <select
            value={market}
            onChange={(e) => setMarket(e.target.value)}
            className="rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:border-accent"
          >
            {markets?.markets.map((m) => (
              <option key={m.market} value={m.market}>{m.label} ({m.count})</option>
            ))}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted">Instrument</label>
          <select
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            className="rounded-lg border border-border bg-background px-3 py-2 font-mono text-sm outline-none focus:border-accent"
          >
            {symbols.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted">Unité d&apos;entrée</label>
          <select
            value={entryTf}
            onChange={(e) => setEntryTf(e.target.value)}
            className="rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:border-accent"
          >
            {markets?.entry_timeframes.map((t) => (
              <option key={t.tf} value={t.tf}>{t.label}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted">Pas d&apos;évaluation</label>
          <select
            value={step}
            onChange={(e) => setStep(Number(e.target.value))}
            className="rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:border-accent"
          >
            <option value={1}>1 bougie — le plus fin (lent)</option>
            <option value={2}>2 bougies</option>
            <option value={4}>4 bougies — équilibré</option>
            <option value={8}>8 bougies — rapide</option>
          </select>
        </div>
        <Button size="sm" onClick={run} loading={busy || running} disabled={running}>
          {running ? 'Backtest en cours…' : busy ? 'Démarrage…' : 'Backtester ce symbole'}
        </Button>
      </div>
      {tfNote && <p className="text-[11px] text-muted">ⓘ {tfNote.note}.</p>}

      {error && <p className="text-sell">{error}</p>}

      {!report?.available ? (
        <div className="rounded-lg border border-border bg-background/40 p-4 text-sm text-muted">
          {report?.note ?? 'Chargement…'}
        </div>
      ) : (
        <div className="space-y-4">
          <div className="rounded-lg border border-accent/40 bg-accent/5 p-3">
            <p className="text-sm font-semibold text-white">
              {report.symbol} · {report.market_label} · déclencheur évalué en {report.entry_timeframe}
            </p>
            <p className="mt-0.5 text-[11px] text-muted">
              {report.coverage || '—'} · {report.years_covered} an(s) évalué(s) ·{' '}
              {report.bars_evaluated} bougie(s) parcourue(s) · calcul {report.duration_s} s
            </p>
            <p className="mt-1 text-[11.5px] leading-relaxed text-gray-300">{report.verdict}</p>
          </div>

          {o && o.trades > 0 && (
            <>
              <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
                <Stat label="Trades" value={`${o.trades}`} />
                <Stat label="Gagnants" value={`${o.wins}`} tone="buy" />
                <Stat label="Perdants" value={`${o.losses}`} tone="sell" />
                <Stat label="Réussite" value={`${o.win_rate} %`} />
                <Stat
                  label="Espérance / trade"
                  value={`${o.expectancy_r > 0 ? '+' : ''}${o.expectancy_r} R`}
                  tone={o.expectancy_r > 0 ? 'buy' : 'sell'}
                  help="Gain moyen par trade en multiples du risque. La seule mesure qui combine taux de réussite ET rapport gain/perte."
                />
                <Stat
                  label="Profit factor"
                  value={`${o.profit_factor ?? '∞'}`}
                  help="Somme des gains ÷ somme des pertes. Sous 1, la stratégie perd sur cet instrument."
                />
                <Stat label="Gain total" value={`${o.total_r} R`} tone={o.total_r > 0 ? 'buy' : 'sell'} />
                <Stat label="Pire série" value={`${o.max_drawdown_r} R`} tone="sell" />
                <Stat label="R/R moyen planifié" value={`1:${o.avg_planned_rr}`} />
                <Stat label="Objectif moyen" value={`${o.avg_reward_pips ?? '—'} pips`} />
                <Stat label="Stop moyen" value={`${o.avg_risk_pips ?? '—'} pips`} />
                <Stat label="Détention moyenne" value={`${o.avg_bars_held} bougies`} />
                <Stat label="Gain moyen gagnant" value={`${o.avg_win_r} R`} tone="buy" />
                <Stat label="Perte moyenne" value={`${o.avg_loss_r} R`} tone="sell" />
                <Stat label="Stop sécurisé (+2R)" value={`${o.secured_rate} %`} />
                <Stat
                  label="Cadence"
                  value={report.days_between_trades ? `1 / ${report.days_between_trades} j` : '—'}
                  help="Un trade tous les N jours sur cet instrument."
                />
              </div>

              <div className="grid gap-4 lg:grid-cols-3">
                <Breakdown title="Par déclencheur" rows={report.by_trigger ?? {}} minTrades={report.min_trades ?? 8} />
                <Breakdown title="Par fenêtre de session" rows={report.by_session ?? {}} minTrades={report.min_trades ?? 8} />
                <Breakdown title="Par sens" rows={report.by_direction ?? {}} minTrades={report.min_trades ?? 8} />
              </div>

              {(report.losers_profile?.findings?.length ?? 0) > 0 && (
                <div className="rounded-lg border border-sell/30 bg-sell/5 p-3">
                  <h4 className="text-xs font-semibold text-white">
                    Ce que les trades stoppés ont en commun ({report.losers_profile!.sample} perdants)
                  </h4>
                  <ul className="mt-1 space-y-0.5 text-[11px] text-muted">
                    {report.losers_profile!.findings!.map((f, i) => <li key={i}>• {f}</li>)}
                  </ul>
                </div>
              )}

              {(report.secure_ab?.note || report.tp_management_ab?.note) && (
                <div className="space-y-1 rounded-lg border border-border bg-background/40 p-3 text-[11px] leading-relaxed text-muted">
                  {report.secure_ab?.note && <p>🔒 {report.secure_ab.note}</p>}
                  {report.tp_management_ab?.note && <p>🎯 {report.tp_management_ab.note}</p>}
                </div>
              )}

              {/* Le JOURNAL des trades : un backtest qui ne montre pas ses trades ne se vérifie pas. */}
              <details className="rounded-lg border border-border bg-background/40 p-3">
                <summary className="cursor-pointer text-xs font-semibold text-white">
                  Les {report.trades?.length ?? 0} trades rejoués, un par un
                </summary>
                <div className="mt-2 max-h-96 overflow-auto">
                  <table className="w-full min-w-[720px] text-[11px]">
                    <thead className="sticky top-0 bg-background">
                      <tr className="text-[10px] uppercase tracking-wide text-muted">
                        <th className="pb-1 text-left font-medium">Date</th>
                        <th className="pb-1 text-left font-medium">Sens</th>
                        <th className="pb-1 text-left font-medium">Déclencheur</th>
                        <th className="pb-1 text-right font-medium">Entrée</th>
                        <th className="pb-1 text-right font-medium">Stop</th>
                        <th className="pb-1 text-right font-medium">Objectif</th>
                        <th className="pb-1 text-right font-medium">R/R</th>
                        <th className="pb-1 text-left font-medium">Issue</th>
                        <th className="pb-1 text-right font-medium">R</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(report.trades ?? []).map((t, i) => (
                        <tr key={i} className="border-t border-border/40">
                          <td className="py-1 pr-2 text-muted">
                            {new Date(t.at).toLocaleDateString('fr-FR')}
                          </td>
                          <td className={`py-1 pr-2 ${t.direction === 'BUY' ? 'text-buy' : 'text-sell'}`}>
                            {t.direction}
                          </td>
                          <td className="py-1 pr-2 text-white/70">{t.trigger}</td>
                          <td className="py-1 pr-2 text-right font-mono text-white/80">{t.entry}</td>
                          <td className="py-1 pr-2 text-right font-mono text-muted">{t.stop_loss}</td>
                          <td className="py-1 pr-2 text-right font-mono text-muted">{t.target}</td>
                          <td className="py-1 pr-2 text-right text-muted">1:{t.planned_rr}</td>
                          <td className="py-1 pr-2 text-white/70">{t.exit_reason || t.outcome}</td>
                          <td className={`py-1 text-right font-medium ${t.r > 0 ? 'text-buy' : 'text-sell'}`}>
                            {t.r > 0 ? '+' : ''}{t.r}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            </>
          )}

          <p className="text-[11px] leading-relaxed text-muted">
            Hypothèse prudente : quand le stop et l&apos;objectif tombent dans la même bougie, le
            stop est considéré touché en premier — on ne sait pas dans quel ordre le prix a circulé
            à l&apos;intérieur de la bougie, et un backtest ne doit jamais s&apos;accorder le
            bénéfice du doute. Résultats passés ≠ résultats futurs.
          </p>
        </div>
      )}
    </Card>
  );
}
