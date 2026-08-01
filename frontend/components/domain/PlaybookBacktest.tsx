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
  type OpportunityRate,
  type PlaybookBacktest as Report,
  type PlaybookBacktestPass,
  type PlaybookMarketTop,
  type PlaybookPairRank,
  type VolumeLevers,
} from '@/lib/api';
import { Button } from '@/components/ui';

/**
 * Profit factor lisible — « ∞ » et « — » ne veulent PAS dire la même chose.
 *
 * L'API rend `profit_factor: null` dans deux situations opposées, que seul `trades` sépare :
 * - `trades > 0` : la paire n'a AUCUN trade perdant, le profit factor est infini (le meilleur
 *   résultat possible) ;
 * - `trades === 0` : rien n'a été mesuré, il n'y a pas de profit factor du tout.
 *
 * Un `?? '∞'` nu affichait donc « ∞ » — le meilleur score qui soit — sur les lignes non classées
 * qui n'ont produit aucun trade. On distingue les deux : « — » pour l'absence de mesure.
 */
function formatPF(pf: number | null | undefined, trades: number): string {
  if (!trades) return '—';
  return pf == null ? '∞' : `${pf}`;
}

/** Taux de réussite lisible : « — » quand aucun trade n'a été mesuré (jamais « 0 % »). */
function formatWinRate(winRate: number | null | undefined, trades: number): string {
  if (!trades || winRate == null) return '—';
  return `${winRate} %`;
}

/**
 * FRÉQUENCE D'OPPORTUNITÉ — « combien de trades par jour », répondu par la mesure.
 *
 * Deux familles de leviers, et elles ne se valent pas du tout : élargir l'univers balayé ajoute des
 * trades sans toucher à aucune règle (l'espérance par trade est inchangée, seul le gain total
 * monte) ; desserrer un seuil ajoute par construction des trades PIRES que la moyenne actuelle,
 * puisque ce sont exactement ceux que le seuil écartait. On les affiche séparément pour qu'on ne
 * puisse pas les confondre.
 */
function FrequencyBlock({ freq, levers }: { freq: OpportunityRate; levers?: VolumeLevers }) {
  const proj = levers?.free.projection_trades_per_day ?? freq.projection ?? {};
  return (
    <section className="rounded-xl border border-border bg-surface p-4 space-y-3">
      <h3 className="text-sm font-semibold text-white">📅 Combien de trades cet univers produit</h3>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Par symbole et par jour" value={String(freq.avg_trades_per_day_per_symbol ?? '—')} />
        <Stat
          label="Soit un trade tous les"
          value={freq.days_between_trades_per_symbol ? `${freq.days_between_trades_per_symbol} j` : '—'}
        />
        <Stat label="Sur cet univers, par jour" value={String(freq.universe_trades_per_day ?? '—')} tone="buy" />
        <Stat label="Par semaine" value={String(freq.universe_trades_per_week ?? '—')} tone="buy" />
      </div>
      <p className="text-[11px] leading-relaxed text-muted">{freq.note}</p>

      {Object.keys(proj).length > 0 && (
        <div>
          <div className="mb-1 text-[11px] font-medium text-white">
            Ce que produirait un univers plus large — au taux mesuré, sans assouplir aucune règle
          </div>
          <div className="flex flex-wrap gap-2">
            {Object.entries(proj).map(([n, v]) => (
              <span key={n} className="rounded-lg border border-border bg-background px-2.5 py-1 font-mono text-[11px] text-muted">
                {n} symboles → <span className="text-white">{v}</span> trade(s)/jour
              </span>
            ))}
          </div>
        </div>
      )}

      {levers && (
        <>
          {levers.free.best_producers.length > 0 && (
            <div>
              <div className="mb-1 text-[11px] font-medium text-white">
                Les symboles qui produisent le plus d’opportunités
              </div>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[520px] text-[11px]">
                  <thead>
                    <tr className="text-[10px] uppercase tracking-wide text-muted">
                      <th className="pb-1 text-left font-medium">Symbole</th>
                      <th className="pb-1 text-right font-medium">Cadence</th>
                      <th className="pb-1 text-right font-medium">Espérance</th>
                      <th className="pb-1 text-right font-medium">R / mois</th>
                    </tr>
                  </thead>
                  <tbody>
                    {levers.free.best_producers.map((p) => (
                      <tr key={p.symbol} className="border-t border-border/40">
                        <td className="py-1 font-mono text-white">{p.symbol}</td>
                        <td className="py-1 text-right text-muted">
                          {p.days_between_trades ? `1 / ${p.days_between_trades} j` : '—'}
                        </td>
                        <td className={`py-1 text-right ${p.expectancy_r > 0 ? 'text-buy' : 'text-sell'}`}>
                          {p.expectancy_r > 0 ? '+' : ''}{p.expectancy_r} R
                        </td>
                        <td className="py-1 text-right text-white">{p.r_per_month ?? '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Les leviers PAYANTS, avec ce qu'ils coûtent — jamais présentés comme des réglages
              à monter d'un cran sans conséquence. */}
          <details className="rounded-lg border border-sell/30 bg-sell/5 p-3">
            <summary className="cursor-pointer text-[11px] font-medium text-sell">
              Assouplir un seuil pour avoir plus de trades — ce que chacun coûte
            </summary>
            <p className="mt-2 text-[11px] leading-relaxed text-muted">{levers.note}</p>
            <ul className="mt-2 space-y-2">
              {levers.costly.map((c) => (
                <li key={c.setting} className="rounded bg-background px-2 py-1.5">
                  <div className="font-mono text-[11px] text-white">
                    {c.setting} <span className="text-muted">= {String(c.current)} → {c.direction}</span>
                  </div>
                  <div className="text-[11px] text-muted">Effet : {c.effect}</div>
                  <div className="text-[11px] text-warn">Risque : {c.risk}</div>
                </li>
              ))}
            </ul>
            <p className="mt-2 text-[11px] text-muted">
              Aucun n’est appliqué automatiquement. Le banc d’essai les rejoue sur les mêmes données
              (<span className="font-mono">POST /api/backtest/playbook/volume-ab/run</span>) et n’en
              déclare un adoptable que si le gain <b className="text-white">total</b> monte sans
              dégrader le profit factor ni le drawdown.
            </p>
          </details>

          <p className="text-[11px] leading-relaxed text-muted">
            ⓘ {levers.free_secondary.why}
          </p>
        </>
      )}
    </section>
  );
}

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

/**
 * LES 10 MEILLEURS INSTRUMENTS DE CHAQUE MARCHÉ.
 *
 * Un classement global mélange EUR/USD et NVDA, dont ni la volatilité ni les horaires ne se
 * comparent : le marché le plus volatil finit par occuper tout le haut du tableau et les autres
 * deviennent invisibles. On classe donc À L'INTÉRIEUR de chaque marché, sur le même critère —
 * l'espérance en R.
 */
function MarketTops({ tops }: { tops: Record<string, PlaybookMarketTop> }) {
  const blocks = Object.values(tops).filter((b) => b.top.length > 0);
  if (blocks.length === 0) return null;
  return (
    <section className="rounded-xl border border-border bg-surface p-4 space-y-3">
      <div>
        <h3 className="text-sm font-semibold text-white">🏆 Les 10 meilleurs de chaque marché</h3>
        <p className="mt-0.5 text-[11px] leading-relaxed text-muted">
          Classés sur l&apos;espérance en R, marché par marché. Un instrument sous le seuil
          d&apos;échantillon n&apos;apparaît pas : on ne met pas en tête ce qu&apos;on n&apos;a pas
          mesuré. Une espérance négative reste affichée telle quelle — « meilleur du marché » ne
          veut pas dire « rentable ».
        </p>
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        {blocks.map((b) => (
          <div key={b.market} className="rounded-lg border border-border/60 bg-background/40 p-3">
            <div className="flex items-baseline justify-between gap-2">
              <h4 className="text-xs font-semibold text-white">{b.label}</h4>
              <span className="text-[10px] text-muted">
                {b.profitable}/{b.top.length} rentables · {b.unrated} non mesuré(s)
              </span>
            </div>
            <div className="mt-2 overflow-x-auto">
              <table className="w-full min-w-[380px] text-[11px]">
                <thead>
                  <tr className="text-[10px] uppercase tracking-wide text-muted">
                    <th className="pb-1 text-left font-medium">#</th>
                    <th className="pb-1 text-left font-medium">Instrument</th>
                    <th className="pb-1 text-right font-medium">Trades</th>
                    <th className="pb-1 text-right font-medium">Réussite</th>
                    <th className="pb-1 text-right font-medium">Espérance</th>
                    <th className="pb-1 text-right font-medium">PF</th>
                  </tr>
                </thead>
                <tbody>
                  {b.top.map((r) => (
                    <tr key={r.symbol} className="border-t border-border/40">
                      <td className="py-1 pr-2 text-muted">{r.market_rank}</td>
                      <td className="py-1 pr-2 font-mono text-white/90">{r.symbol}</td>
                      <td className="py-1 pr-2 text-right text-muted">{r.trades}</td>
                      <td className="py-1 pr-2 text-right text-white/80">{formatWinRate(r.win_rate, r.trades)}</td>
                      <td className={`py-1 pr-2 text-right ${r.expectancy_r > 0 ? 'text-buy' : 'text-sell'}`}>
                        {r.expectancy_r > 0 ? '+' : ''}{r.expectancy_r} R
                      </td>
                      <td className="py-1 text-right text-muted">{formatPF(r.profit_factor, r.trades)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="mt-1.5 text-[10px] leading-relaxed text-muted">{b.note}</p>
          </div>
        ))}
      </div>
    </section>
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
              <td className="py-1.5 pr-2 text-right text-white/80">{formatWinRate(r.win_rate, r.trades)}</td>
              <td className="py-1.5 pr-2 text-right text-muted">1:{r.avg_planned_rr}</td>
              <td className={`py-1.5 pr-2 text-right ${r.expectancy_r > 0 ? 'text-buy' : 'text-sell'}`}>
                {r.expectancy_r > 0 ? '+' : ''}{r.expectancy_r} R
              </td>
              <td className="py-1.5 pr-2 text-right text-muted">{formatPF(r.profit_factor, r.trades)}</td>
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
            🧪 Backtest de la stratégie — forex, actions, indices, métaux &amp; crypto
          </h2>
          <p className="text-xs text-muted">
            La stratégie complète rejouée sur l&apos;historique réel, en walk-forward strict : à
            chaque bougie, seules les données déjà disponibles sont utilisées. Plusieurs
            instruments par marché, puis les 10 meilleurs de chacun.
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
          {/* LES MEILLEURS PAR MARCHÉ — la première chose qu'on veut lire après un backtest :
              où cette stratégie marche-t-elle, et sur quels instruments précisément. */}
          {report.conclusion?.market_tops && (
            <MarketTops tops={report.conclusion.market_tops} />
          )}

          {/* FRÉQUENCE : la réponse chiffrée à « combien de trades par jour », et le seul levier
              pour en avoir davantage qui ne coûte rien en qualité. */}
          {report.conclusion?.frequency?.symbols ? (
            <FrequencyBlock
              freq={report.conclusion.frequency}
              levers={report.conclusion.volume_levers}
            />
          ) : null}

          {/* Passe LONGUE : soit ses chiffres, soit la raison pour laquelle elle ne mesure rien —
              jamais une section vide présentée comme une mesure. */}
          {report.conclusion?.long && !report.conclusion.long.measurable && (
            <div className="rounded-xl border border-warn/40 bg-warn-soft/20 p-4">
              <p className="text-sm font-semibold text-white">
                Passe LONGUE (5 ans) — non mesurable, et ce n’est pas une panne
              </p>
              <p className="mt-1 text-[11.5px] leading-relaxed text-muted">{report.conclusion.long.note}</p>
            </div>
          )}
          {report.long && report.conclusion?.long?.measurable && (
            <PassBlock
              pass={report.long}
              title="Passe LONGUE — 5 ans, échelle swing"
              note="Contexte mensuel + hebdomadaire, entrée journalière : la seule façon d'atteindre cinq ans de données réelles. Filtre un cran moins sévère que la production, faute d'une quatrième unité de temps sur cette profondeur."
            />
          )}

          {report.scope && (
            <PassBlock
              pass={report.scope}
              title="Passe PORTÉE — déclencheur évalué en 1 h"
              note="Toute la profondeur d'historique disponible, au réglage de PRODUCTION. C'est elle qui donne le recul statistique, le classement des paires et les verdicts qui gouvernent l'auto-entrée."
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
