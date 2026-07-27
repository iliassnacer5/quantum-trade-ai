'use client';

/**
 * RAPPORT DE BACKTEST DE LA STRATÉGIE DU DESK — la page qui explique les chiffres.
 *
 * Le backtest complet (toutes les paires forex + or, toute la profondeur d'historique) est lancé en
 * ligne de commande ou par la boucle hebdomadaire ; cette page en présente le résultat avec ce qui
 * manque toujours à un tableau de chiffres : la MÉTHODE, les LIMITES, et la lecture de chaque
 * indicateur. Un backtest sans ses hypothèses n'est pas une mesure, c'est une illustration.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  api,
  type PlaybookBacktest,
  type PlaybookBacktestMetrics,
  type PlaybookBacktestPass,
  type PlaybookPairRank,
} from '@/lib/api';
import { Button, Card, PageHeader, RouteTabs, PROVE_TABS, Segmented, Table, THead, TBody, TR, TH, TD } from '@/components/ui';

/** Lecture pédagogique de chaque métrique — au survol du libellé. */
const GLOSSARY: Record<string, string> = {
  trades: "Nombre de fois où la cascade complète a été satisfaite et où une position aurait été ouverte.",
  win_rate: "Part des trades sortis en gain. Seul, ce chiffre ne dit rien : 40 % de réussite avec un R/R 1:3 est rentable, 70 % avec un R/R 1:0,5 ne l'est pas.",
  expectancy_r: "Gain moyen par trade, exprimé en multiples du risque (R). C'est LA mesure qui compte : elle combine le taux de réussite et le rapport gain/perte. Positive = la stratégie gagne de l'argent sur la durée.",
  profit_factor: "Somme des gains ÷ somme des pertes. Au-dessus de 1,5 le résultat est solide ; en dessous de 1, la stratégie perd. « ∞ » signifie aucun trade perdant sur l'échantillon.",
  max_drawdown_r: "Pire série de pertes cumulées depuis un sommet, en R. C'est ce qu'il faut être capable d'encaisser psychologiquement et financièrement.",
  avg_planned_rr: "Rapport gain visé / risque pris au moment d'entrer, avant de savoir ce qui s'est passé.",
  avg_win_r: "Ce que rapporte un trade gagnant en moyenne.",
  avg_loss_r: "Ce que coûte un trade perdant en moyenne. Proche de −1 R = les stops sont respectés.",
  secured_rate: "Part des trades ayant atteint +2R, donc dont le stop a été remonté pour verrouiller ce gain.",
  avg_bars_held: "Durée moyenne de détention, en bougies de l'unité de temps d'entrée.",
  expired: "Trades sortis au marché faute d'avoir touché stop ou objectif dans le temps imparti.",
};

function Metric({ label, value, tone, help }: { label: string; value: string; tone?: 'buy' | 'sell'; help?: string }) {
  return (
    <div className="rounded-lg border border-border bg-background/40 px-3 py-2" title={help}>
      <div className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-muted">
        {label}
        {help && <span className="cursor-help text-muted/60">ⓘ</span>}
      </div>
      <div className={`text-base font-semibold ${tone === 'buy' ? 'text-buy' : tone === 'sell' ? 'text-sell' : 'text-white'}`}>
        {value}
      </div>
    </div>
  );
}

function MetricsGrid({ m }: { m: PlaybookBacktestMetrics }) {
  return (
    <div className="grid grid-cols-2 gap-2 md:grid-cols-4 lg:grid-cols-6">
      <Metric label="Trades" value={`${m.trades}`} help={GLOSSARY.trades} />
      <Metric label="Gagnants" value={`${m.wins}`} tone="buy" />
      <Metric label="Perdants" value={`${m.losses}`} tone="sell" />
      <Metric label="Réussite" value={`${m.win_rate} %`} help={GLOSSARY.win_rate} />
      <Metric
        label="Espérance / trade"
        value={`${m.expectancy_r > 0 ? '+' : ''}${m.expectancy_r} R`}
        tone={m.expectancy_r > 0 ? 'buy' : 'sell'}
        help={GLOSSARY.expectancy_r}
      />
      <Metric label="Profit factor" value={`${m.profit_factor ?? '∞'}`} help={GLOSSARY.profit_factor} />
      <Metric label="R/R moyen visé" value={`1:${m.avg_planned_rr}`} help={GLOSSARY.avg_planned_rr} />
      <Metric label="Gain moyen" value={`${m.avg_win_r} R`} tone="buy" help={GLOSSARY.avg_win_r} />
      <Metric label="Perte moyenne" value={`${m.avg_loss_r} R`} tone="sell" help={GLOSSARY.avg_loss_r} />
      <Metric label="Pire série" value={`${m.max_drawdown_r} R`} tone="sell" help={GLOSSARY.max_drawdown_r} />
      <Metric label="Stop sécurisé" value={`${m.secured_rate} %`} help={GLOSSARY.secured_rate} />
      <Metric label="Détention" value={`${m.avg_bars_held} bougies`} help={GLOSSARY.avg_bars_held} />
    </div>
  );
}

function Ranking({ rows, minTrades }: { rows: PlaybookPairRank[]; minTrades: number }) {
  const rated = rows.filter((r) => r.rank != null);
  const unrated = rows.filter((r) => r.rank == null);
  return (
    <>
      <Table>
        <THead>
          <TR>
            <TH>#</TH><TH>Paire</TH><TH className="text-right">Trades</TH>
            <TH className="text-right">G / P</TH><TH className="text-right">Réussite</TH>
            <TH className="text-right">R/R visé</TH><TH className="text-right">Espérance</TH>
            <TH className="text-right">PF</TH><TH className="text-right">Pire série</TH>
            <TH>Verdict</TH>
          </TR>
        </THead>
        <TBody>
          {rated.map((r) => (
            <TR key={r.symbol}>
              <TD className="text-muted">{r.rank}</TD>
              <TD className="font-mono text-white">{r.symbol}</TD>
              <TD className="text-right text-muted">{r.trades}</TD>
              <TD className="text-right"><span className="text-buy">{r.wins}</span> / <span className="text-sell">{r.losses}</span></TD>
              <TD className="text-right">{r.win_rate} %</TD>
              <TD className="text-right text-muted">1:{r.avg_planned_rr}</TD>
              <TD className={`text-right font-semibold ${r.expectancy_r > 0 ? 'text-buy' : 'text-sell'}`}>
                {r.expectancy_r > 0 ? '+' : ''}{r.expectancy_r} R
              </TD>
              <TD className="text-right text-muted">{r.profit_factor ?? '∞'}</TD>
              <TD className="text-right text-sell">{r.max_drawdown_r} R</TD>
              <TD className="text-[11px] text-white/70">{r.verdict}</TD>
            </TR>
          ))}
        </TBody>
      </Table>
      {unrated.length > 0 && (
        <p className="mt-2 text-[11px] text-muted">
          <strong className="text-white/80">Non classées</strong> ({unrated.map((u) => `${u.symbol} — ${u.trades} trade(s)`).join(' · ')}) :
          moins de {minTrades} trades. Une paire mesurée sur 2 ou 3 trades n&apos;est pas mesurée du
          tout — on préfère le dire plutôt que de lui donner un rang qui n&apos;a pas de sens.
        </p>
      )}
    </>
  );
}

function GroupTable({ title, data, note, minTrades }: {
  title: string; data: Record<string, PlaybookBacktestMetrics>; note: string; minTrades: number;
}) {
  const rows = Object.entries(data ?? {}).filter(([, m]) => m.trades > 0)
    .sort((a, b) => b[1].expectancy_r - a[1].expectancy_r);
  if (rows.length === 0) return null;
  return (
    <Card className="p-4">
      <h4 className="text-sm font-semibold text-white">{title}</h4>
      <p className="mt-0.5 text-[11px] text-muted">{note}</p>
      <div className="mt-2 space-y-1 text-xs">
        {rows.map(([k, m]) => (
          <div key={k} className="flex items-center justify-between border-t border-border/60 py-1">
            <span className="text-white/85">
              {k} {m.trades < minTrades && <span className="text-muted">(échantillon faible)</span>}
            </span>
            <span className="text-muted">
              {m.trades} trades · {m.win_rate} % ·{' '}
              <span className={m.expectancy_r > 0 ? 'text-buy' : 'text-sell'}>
                {m.expectancy_r > 0 ? '+' : ''}{m.expectancy_r} R
              </span>
            </span>
          </div>
        ))}
      </div>
    </Card>
  );
}

function PassSection({ pass, title, method }: { pass: PlaybookBacktestPass; title: string; method: string }) {
  return (
    <section className="space-y-3">
      <div>
        <h3 className="text-base font-semibold text-white">{title}</h3>
        <p className="text-[11px] text-muted">{method}</p>
        <p className="mt-0.5 text-[11px] text-muted">
          {pass.pairs_tested} paire(s) · {pass.years_covered} an(s) d&apos;historique · déclencheur
          évalué en {pass.entry_timeframe} · calcul en {pass.duration_s} s
          {pass.failures.length > 0 && (
            <> · <span className="text-warn">{pass.failures.length} paire(s) sans données : {pass.failures.map((f) => f.symbol).join(', ')}</span></>
          )}
        </p>
      </div>

      {pass.overall.trades === 0 ? (
        <Card className="p-5 text-sm text-muted">
          Aucun trade conforme sur cette passe : la cascade complète n&apos;a jamais été entièrement
          satisfaite. C&apos;est un résultat en soi, pas une panne.
        </Card>
      ) : (
        <>
          <MetricsGrid m={pass.overall} />
          <Card className="p-4">
            <h4 className="mb-1 text-sm font-semibold text-white">Classement des paires par fiabilité</h4>
            <p className="mb-2 text-[11px] text-muted">
              Classées sur l&apos;espérance en R. C&apos;est le bon critère : le seul taux de réussite
              serait flatteur (beaucoup de petits gains, quelques grosses pertes).
            </p>
            <Ranking rows={pass.ranking} minTrades={pass.min_trades} />
          </Card>

          <div className="grid gap-3 lg:grid-cols-3">
            <GroupTable title="Par déclencheur d'entrée" data={pass.by_trigger} minTrades={pass.min_trades}
              note="Repli sur MA / zone d'or, cassure confirmée, ou divergence." />
            <GroupTable title="Par fenêtre de session" data={pass.by_session} minTrades={pass.min_trades}
              note="Ouverture de Londres, chevauchement Londres/NY, ou hors fenêtre." />
            <GroupTable title="Par sens" data={pass.by_direction} minTrades={pass.min_trades}
              note="Achats contre ventes — un déséquilibre net peut trahir un biais de la période." />
          </div>

          {(pass.losers_profile.findings?.length ?? 0) > 0 && (
            <Card variant="danger" className="p-4">
              <h4 className="text-sm font-semibold text-white">
                Ce que les trades stoppés ont en commun ({pass.losers_profile.sample} perdants)
              </h4>
              <p className="mt-0.5 text-[11px] text-muted">
                On compare le profil moyen des perdants à celui des gagnants, facteur par facteur.
                Un écart net désigne une condition de marché où la méthode ne fonctionne pas —
                c&apos;est là qu&apos;il faut ajouter un filtre, et nulle part ailleurs.
              </p>
              <ul className="mt-2 space-y-0.5 text-[11px] text-white/80">
                {pass.losers_profile.findings!.map((f, i) => <li key={i}>• {f}</li>)}
              </ul>
              {pass.losers_profile.comparisons && (
                <div className="mt-3 space-y-1 text-[11px]">
                  {Object.values(pass.losers_profile.comparisons).map((c) => (
                    <div key={c.label} className="flex items-center justify-between border-t border-border/40 py-1">
                      <span className="text-muted">{c.label}</span>
                      <span>
                        <span className="text-sell">{c.perdants}</span>
                        <span className="text-muted"> vs </span>
                        <span className="text-buy">{c.gagnants}</span>
                        <span className={`ml-2 ${Math.abs(c.ecart_pct) >= 12 ? 'text-warn' : 'text-muted'}`}>
                          {c.ecart_pct > 0 ? '+' : ''}{c.ecart_pct} %
                        </span>
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          )}
        </>
      )}
    </section>
  );
}

export default function BacktestReportPage() {
  const [report, setReport] = useState<PlaybookBacktest | null>(null);
  const [view, setView] = useState('scope');
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

  useEffect(() => { void load(); }, [load]);

  // Le backtest tourne en ARRIÈRE-PLAN côté serveur : tant qu'il tourne, on suit son avancement.
  // Sans ça, l'utilisateur ne saurait pas si la page est figée ou si le calcul progresse.
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
      await load();                       // récupère l'état « en cours »
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const pass = view === 'fidelity' ? report?.fidelity : report?.scope;

  return (
    <div className="space-y-6 p-8">
      <PageHeader
        title="Rapport de backtest — stratégie du desk"
        subtitle="La cascade complète rejouée sur l'historique réel du forex et de l'or, avec sa méthode et ses limites."
      />
      <RouteTabs items={PROVE_TABS} />

      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted">
          {report?.strategy ?? 'Chargement…'}
          {report?.date && <> · dernier passage le <span className="text-white/80">{report.date}</span></>}
        </p>
        <Button size="sm" onClick={run} loading={busy || running} disabled={running}>
          {running ? 'Backtest en cours…' : busy ? 'Démarrage…' : 'Relancer le backtest'}
        </Button>
      </div>

      {error && <p className="text-sell">{error}</p>}

      {running && report?.run_state && (
        <Card className="border-accent/40 bg-accent/5 p-4">
          <p className="text-sm font-semibold text-accent">
            ⏳ Backtest en cours — {report.run_state.phase ?? 'préparation'}
          </p>
          <p className="mt-0.5 text-[11px] text-muted">
            Démarré il y a {Math.round(report.run_state.elapsed_s ?? 0)} s. Le calcul tourne côté
            serveur, en arrière-plan : tu peux quitter cette page, il continuera. Elle se met à jour
            toute seule toutes les 5 secondes.
          </p>
          <p className="mt-1 text-[11px] text-muted">
            Un backtest complet (14 paires × 2 passes × 2 ans d&apos;historique) prend une dizaine
            de minutes. Les chiffres affichés ci-dessous restent ceux du passage précédent tant
            qu&apos;il n&apos;est pas terminé.
          </p>
        </Card>
      )}

      {/* MÉTHODE — un backtest sans ses hypothèses n'est pas une mesure. */}
      <Card className="p-4">
        <h2 className="text-sm font-semibold text-white">Comment ces chiffres sont obtenus</h2>
        <ul className="mt-2 space-y-1 text-[11.5px] leading-relaxed text-gray-300">
          <li>
            • <strong className="text-white/90">Walk-forward strict.</strong> À chaque bougie évaluée,
            le setup est reconstruit avec les SEULES données disponibles à cet instant. Aucun regard
            vers le futur, ni pour le contexte, ni pour l&apos;entrée.
          </li>
          <li>
            • <strong className="text-white/90">Hypothèse défavorable systématique.</strong> Quand le
            stop et l&apos;objectif tombent dans la même bougie, le stop est considéré touché en
            premier : on ignore l&apos;ordre réel des ticks, et un backtest ne doit jamais
            s&apos;accorder le bénéfice du doute.
          </li>
          <li>
            • <strong className="text-white/90">Une position à la fois par paire</strong>, comme en
            réel — pas d&apos;empilement qui gonflerait artificiellement le nombre de trades.
          </li>
          <li>
            • <strong className="text-white/90">La sécurisation à +2R est rejouée</strong> : dès que
            le trade a parcouru deux fois son risque, le stop est remonté sur ce niveau et n&apos;en
            redescend plus.
          </li>
          <li>
            • <strong className="text-white/90">Données réelles uniquement.</strong> Une paire dont
            l&apos;historique n&apos;est pas disponible est écartée et signalée — jamais remplacée
            par des bougies simulées.
          </li>
        </ul>
      </Card>

      {/* LIMITES — annoncées avant les résultats, pas en note de bas de page. */}
      {report?.data_limits && (
        <Card variant="danger" className="p-4">
          <h2 className="text-sm font-semibold text-white">Limites de ces mesures</h2>
          <p className="mt-1 text-[11.5px] leading-relaxed text-gray-300">{report.data_limits.note}</p>
          <p className="mt-2 text-[11px] text-muted">
            Profondeur réellement disponible chez les fournisseurs gratuits :
            <strong className="text-white/80"> 15 min → {report.data_limits.m15_days} jours</strong> ·
            <strong className="text-white/80"> 1 h → {report.data_limits.h1_years} ans</strong> ·
            <strong className="text-white/80"> journalier → {report.data_limits.daily_years} ans</strong>.
            Un backtest sur 5 ans avec une entrée en 15 min est donc impossible sans une source
            d&apos;archives minute payante.
          </p>
        </Card>
      )}

      {!report?.available ? (
        <Card className="p-6 text-sm text-muted">
          {report?.note ?? 'Chargement du rapport…'}
          {report?.universe && (
            <p className="mt-2 text-[11px]">
              Univers prévu : {report.universe.join(' · ')}
            </p>
          )}
        </Card>
      ) : (
        <>
          {report.conclusion && (
            <Card className="border-accent/40 bg-accent/5 p-4">
              <h2 className="text-sm font-semibold text-accent">Conclusion</h2>
              <p className="mt-1 text-sm text-white">{report.conclusion.headline}</p>
              <ul className="mt-2 space-y-1 text-[11.5px] leading-relaxed text-gray-300">
                {report.conclusion.lines.map((l, i) => <li key={i}>• {l}</li>)}
              </ul>
              {Object.keys(report.conclusion.markets ?? {}).length > 0 && (
                <div className="mt-3 grid gap-2 sm:grid-cols-2">
                  {Object.entries(report.conclusion.markets).map(([name, m]) => (
                    <div key={name} className="rounded-lg border border-border bg-background/40 p-3">
                      <div className="text-xs font-semibold text-white">{name}</div>
                      <div className="mt-0.5 text-[11px] text-muted">
                        {m.trades} trades sur {m.pairs} instrument(s) · {m.win_rate} % de réussite ·{' '}
                        <span className={m.expectancy_r > 0 ? 'text-buy' : 'text-sell'}>
                          {m.expectancy_r > 0 ? '+' : ''}{m.expectancy_r} R
                        </span>
                        {m.best && <> · meilleur : <span className="font-mono text-white/80">{m.best}</span></>}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          )}

          <Segmented
            value={view}
            onChange={setView}
            options={[
              { value: 'scope', label: `Portée — 1 h (${report.scope?.overall.trades ?? 0} trades)` },
              { value: 'fidelity', label: `Fidélité — 15 min (${report.fidelity?.overall.trades ?? 0} trades)` },
            ]}
          />

          {pass && view === 'scope' && (
            <PassSection
              pass={pass}
              title="Passe PORTÉE — déclencheur évalué en 1 heure"
              method="Toute la profondeur d'historique disponible. C'est elle qui donne le recul statistique et le classement des paires : c'est la mesure de référence."
            />
          )}
          {pass && view === 'fidelity' && (
            <PassSection
              pass={pass}
              title="Passe FIDÉLITÉ — vrai déclencheur 15 minutes"
              method="Le déclencheur réel de la stratégie, mais limité aux ~80 jours de 15 min réellement disponibles. Sert de contrôle : le passage au 15 min confirme-t-il ce que la passe portée a mesuré ?"
            />
          )}

          <p className="text-[11px] text-muted">
            Un résultat passé ne prédit pas le futur. Ces chiffres décrivent ce qu&apos;aurait donné
            la méthode sur la période testée, avec les hypothèses décrites plus haut — rien de plus.
          </p>
        </>
      )}
    </div>
  );
}
