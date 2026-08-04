'use client';

import { useEffect, useState } from 'react';
import {
  api, PlaybookTrade, StrategyNote, StrategySpec, StrategyStep, WalkForward,
} from '@/lib/api';
import { MarketSelector } from '@/components/domain';
import { PageHeader, RouteTabs, PROVE_TABS } from '@/components/ui';

const VERDICT_STYLE: Record<string, string> = {
  robuste: 'text-buy', fragile: 'text-yellow-400', non_prouve: 'text-sell', insuffisant: 'text-muted',
};

/** Les sections de la page, dans l'ordre — sert aussi de sommaire cliquable. */
const SECTIONS = [
  ['principes', 'Ce à quoi la méthode obéit'],
  ['boucles', 'Ce qui tourne en permanence'],
  ['deroule', 'Le déroulé, de A à Z'],
  ['outils', 'Comment le graphique est lu'],
  ['checklist', 'La checklist du moteur'],
  ['risque', 'Ce qui protège le capital'],
  ['execution', 'De la décision à l’ordre'],
  ['selection', 'Comment les trades sont choisis'],
  ['perimetre', 'Où et quand elle s’applique'],
  ['glossaire', 'Ce que mesure chaque indicateur'],
  ['refus', 'Ce que la stratégie refuse de faire'],
  ['appliquer', 'L’appliquer à un symbole'],
] as const;

/**
 * La page ne présente qu'UNE stratégie : celle du desk. Elle la décrit de A à Z, en lisant la
 * SPÉCIFICATION servie par le backend (`/api/agents/strategy`) — donc les seuils réellement
 * appliqués par le moteur, jamais une copie qui finirait par décrire une version disparue.
 *
 * Elle ne s'arrête pas aux grands principes : les règles exactes de chaque indicateur, les
 * cadences des boucles de fond, la façon dont un ordre est dimensionné et les garde-fous qui
 * peuvent l'annuler y figurent aussi. Une stratégie qu'on ne peut pas vérifier ligne à ligne
 * n'est pas discutable, et une règle qu'on ne peut pas discuter finit par ne plus être appliquée.
 */
export default function StrategyPage() {
  const [spec, setSpec] = useState<StrategySpec | null>(null);
  const [market, setMarket] = useState('forex');
  const [symbol, setSymbol] = useState('EUR/USD');
  const [symbols, setSymbols] = useState<{ symbol: string }[]>([]);
  const [setup, setSetup] = useState<PlaybookTrade | null>(null);
  const [wf, setWf] = useState<WalkForward | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [autoTrade, setAutoTrade] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggleAutoTrade() {
    const r = await api.setAutoTrade(!autoTrade);
    setAutoTrade(r.auto_trade);
  }

  useEffect(() => {
    api.strategySpec().then(setSpec).catch((e) => setError(e.message));
    api.autoTrade().then((d) => setAutoTrade(d.auto_trade)).catch(() => {});
  }, []);

  useEffect(() => {
    api.symbols(undefined, market)
      .then((d) => {
        setSymbols(d.results);
        if (d.results.length && !d.results.some((r) => r.symbol === symbol)) setSymbol(d.results[0].symbol);
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [market]);

  async function analyse() {
    setBusy('setup');
    setError(null);
    try {
      setSetup(await api.playbook(symbol));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  }

  async function validate() {
    setBusy('wf');
    setError(null);
    try {
      setWf(await api.walkForward(symbol, '4h'));
    } catch (e: any) {
      setError(e.message?.includes('402') ? 'Réservé au plan Pro+ (backtesting).' : e.message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="p-8 space-y-6">
      <PageHeader
        title="La stratégie du desk"
        subtitle={spec?.one_liner ?? 'Une seule méthode, appliquée à tous les marchés.'}
      />
      <RouteTabs items={PROVE_TABS} />

      {error && <div className="rounded-lg border border-sell/40 bg-sell/10 p-3 text-sm text-sell">{error}</div>}
      {!spec && !error && <p className="text-sm text-muted">Chargement de la spécification…</p>}

      {spec && (
        <>
          {/* Sommaire : la page décrit la méthode entière, elle est longue par nature. */}
          <nav className="flex flex-wrap gap-2 rounded-xl border border-border bg-surface p-3">
            {SECTIONS.map(([id, label]) => (
              <a key={id} href={`#${id}`}
                className="rounded-lg border border-border/60 bg-background px-2.5 py-1 text-xs text-muted hover:border-primary/60 hover:text-white">
                {label}
              </a>
            ))}
          </nav>

          {/* --- Ce à quoi la méthode obéit --- */}
          <Section id="principes" title="Ce à quoi la méthode obéit"
            aside={spec.veto ? 'Droit de veto actif — aucun agent ne la contourne' : 'Veto inactif'}>
            <p className="text-sm leading-relaxed text-muted">{spec.name}</p>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {spec.principles.map((p) => (
                <div key={p.title} className="rounded-lg border border-border/60 bg-background p-3">
                  <div className="text-sm font-medium text-white">{p.title}</div>
                  <p className="mt-1 text-xs leading-relaxed text-muted">{p.body}</p>
                </div>
              ))}
            </div>
          </Section>

          {/* --- Les boucles de fond : la stratégie n'est pas une page qu'on rafraîchit --- */}
          <Section id="boucles" title="Ce qui tourne en permanence">
            <div className="space-y-2">
              {spec.pipeline.loops.map((l) => (
                <div key={l.name} className="rounded-lg border border-border/60 bg-background p-3">
                  <div className="flex flex-wrap items-baseline gap-2">
                    <span className="text-sm font-medium text-white">{l.name}</span>
                    <span className="rounded bg-primary/15 px-2 py-0.5 font-mono text-2xs text-primary">
                      {l.cadence}
                    </span>
                  </div>
                  <p className="mt-1 text-xs leading-relaxed text-muted">{l.body}</p>
                </div>
              ))}
            </div>

            <div className="overflow-x-auto">
              <table className="w-full min-w-[34rem] text-left text-xs">
                <thead className="text-2xs uppercase tracking-wide text-muted">
                  <tr>
                    <th className="py-1.5 pr-3">Unité de temps</th>
                    <th className="py-1.5 pr-3">Rôle dans la décision</th>
                    <th className="py-1.5 pr-3">Bougies chargées</th>
                    <th className="py-1.5 pr-3">Minimum exigé</th>
                    <th className="py-1.5">Cache</th>
                  </tr>
                </thead>
                <tbody>
                  {spec.pipeline.timeframes.map((t) => (
                    <tr key={t.tf} className="border-t border-border/60">
                      <td className="py-1.5 pr-3 font-mono text-white">{t.tf}</td>
                      <td className="py-1.5 pr-3 leading-relaxed text-muted">{t.role}</td>
                      <td className="py-1.5 pr-3 font-mono text-muted">{t.candles}</td>
                      <td className="py-1.5 pr-3 font-mono text-muted">{t.min_candles}</td>
                      <td className="py-1.5 font-mono text-muted">{fmtSeconds(t.cache_s)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <ul className="space-y-1">
              {spec.pipeline.notes.map((n, i) => (
                <li key={i} className="flex gap-2 text-xs leading-relaxed text-muted">
                  <span className="text-primary">▸</span><span>{n}</span>
                </li>
              ))}
            </ul>
          </Section>

          {/* --- Le déroulé, étape par étape --- */}
          <section id="deroule" className="scroll-mt-6 space-y-4">
            <h2 className="text-lg font-semibold text-white">Le déroulé, de A à Z</h2>
            {spec.steps.map((step) => <StepCard key={step.n} step={step} />)}
          </section>

          {/* --- Les outils de lecture, communs aux trois étapes --- */}
          <Section id="outils" title="Comment le graphique est lu"
            aside="les mêmes outils servent à valider la tendance, à choisir le moment, et à poser le stop">
            <div className="grid gap-3 md:grid-cols-2">
              {spec.toolbox.map((t) => (
                <div key={t.name} className="rounded-lg border border-border/60 bg-background p-3">
                  <div className="text-sm font-medium text-white">{t.name}</div>
                  <div className="mt-0.5 text-2xs uppercase tracking-wide text-primary">{t.purpose}</div>
                  <p className="mt-1.5 text-xs leading-relaxed text-muted">{t.body}</p>
                </div>
              ))}
            </div>
          </Section>

          {/* --- La checklist : ce qui bloque, et ce qui informe --- */}
          <Section id="checklist" title="La checklist du moteur"
            aside={`${spec.checklist.filter((c) => c.blocking).length} cases sur ${spec.checklist.length} refusent réellement le trade`}>
            <div className="space-y-1.5">
              {spec.checklist.map((c) => (
                <div key={c.n} className="flex flex-wrap items-start gap-x-3 gap-y-1 rounded-lg bg-background px-3 py-2">
                  <span className="font-mono text-2xs text-muted/60">étape {c.step}</span>
                  <span className="min-w-[16rem] flex-1 text-xs font-medium text-white">{c.label}</span>
                  <span className={`rounded px-1.5 py-0.5 text-[10px] ${
                    c.blocking ? 'bg-sell/20 text-sell' : 'bg-border/60 text-muted'}`}>
                    {c.blocking ? 'bloquante' : 'informative'}
                  </span>
                  <span className="w-full text-2xs leading-relaxed text-muted">{c.why}</span>
                </div>
              ))}
            </div>
            <p className="text-xs leading-relaxed text-muted">{spec.checklist_note}</p>
          </Section>

          {/* --- Gestion du risque --- */}
          <Section id="risque" title="Ce qui protège le capital">
            <dl className="grid gap-3 md:grid-cols-2">
              {([
                ['Taille de position', spec.risk.sizing],
                ['Corrélation', spec.risk.correlation],
                ['Gel après pertes', spec.risk.freeze],
                ['Filtre des paires auto-tradées', spec.risk.gating],
                ['Volatilité', spec.risk.volatility],
              ] as const).map(([label, body]) => (
                <div key={label} className="rounded-lg border border-border/60 bg-background p-3">
                  <dt className="text-xs uppercase tracking-wide text-muted">{label}</dt>
                  <dd className="mt-1 text-xs leading-relaxed text-white">{body}</dd>
                </div>
              ))}
            </dl>
          </Section>

          {/* --- De la décision à l'ordre : c'est là que se joue chaque trade réel --- */}
          <Section id="execution" title="De la décision à l’ordre">
            <div>
              <h3 className="mb-1.5 text-sm font-medium text-white">Comment la position est dimensionnée</h3>
              <ul className="space-y-1">
                {spec.execution.sizing.map((line, i) => (
                  <li key={i} className="flex gap-2 text-xs leading-relaxed text-muted">
                    <span className="text-primary">▸</span><span>{line}</span>
                  </li>
                ))}
              </ul>
            </div>

            <div>
              <h3 className="mb-1.5 text-sm font-medium text-white">
                Ce qui peut annuler un ordre alors même que le setup est valide
              </h3>
              <div className="space-y-1.5">
                {spec.execution.guards.map((g) => (
                  <div key={g.label} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-lg bg-background px-3 py-2">
                    <span className="min-w-[13rem] text-xs font-medium text-white">{g.label}</span>
                    <span className="flex-1 text-2xs leading-relaxed text-muted">{g.body}</span>
                  </div>
                ))}
              </div>
            </div>

            <Note label="Ce que chaque position emporte dans le journal" body={spec.execution.journal} />
            <Note label="Aucun refus n’est silencieux" body={spec.execution.no_silent_refusal} />
            <div className="rounded-lg border border-warn/30 bg-warn/5 p-3 text-xs leading-relaxed text-muted">
              {spec.execution.paper_only}
            </div>
          </Section>

          {/* --- Sélection et classement --- */}
          <Section id="selection" title="Comment les trades sont choisis, puis classés">
            <div className="grid gap-3 md:grid-cols-3">
              {spec.selection.tiers.map((t) => (
                <div key={t.label} className="rounded-lg border border-border/60 bg-background p-3">
                  <div className="text-xs font-semibold uppercase tracking-wide text-primary">{t.label}</div>
                  <p className="mt-1 text-xs leading-relaxed text-muted">{t.body}</p>
                </div>
              ))}
            </div>
            <div>
              <h3 className="mb-1.5 text-sm font-medium text-white">Ordre de classement</h3>
              <ol className="space-y-1">
                {spec.selection.ranking.map((r, i) => (
                  <li key={i} className="text-xs leading-relaxed text-muted">{r}</li>
                ))}
              </ol>
            </div>
            <Note label="Plancher de fiabilité" body={spec.selection.floor} />
            <Note label="Aucun quota" body={spec.selection.no_quota} />
            <Note label="Ordre de balayage" body={spec.selection.universe_order} />
          </Section>

          {/* --- Périmètre --- */}
          <Section id="perimetre" title="Où et quand elle s’applique">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Fact label="Marchés" value={spec.scope.markets.join(' · ')} />
              <Fact
                label="Symboles balayés"
                value={`${spec.scope.universe_size}${spec.scope.universe_capped ? ' (plafonné)' : ' — tout le catalogue'}`}
              />
              <Fact label="Horaires" value={spec.scope.hours} />
              <Fact label="Trades proposés" value={String(spec.scope.proposals_per_day)} />
              <Fact label="Tendance mesurée sur" value={spec.scope.trend_timeframes.join(' · ')} />
              <Fact
                label="Entrée"
                value={`${spec.scope.entry_timeframe} (confirmation ${spec.scope.confirm_timeframe})`}
              />
              <Fact label="Fiabilité minimale" value={`${spec.scope.min_reliability}/5`} />
              <Fact
                label="Entrée automatique"
                value={spec.scope.auto_entry ? 'Active — compte démo' : 'Désactivée'}
              />
            </div>
            {spec.scope.universe_note && (
              <p className="text-xs leading-relaxed text-muted">{spec.scope.universe_note}</p>
            )}
            <p className="text-xs leading-relaxed text-muted">{spec.scope.why_all_markets}</p>
            {spec.scope.auto_entry && (
              <p className="text-xs text-warn">{spec.scope.auto_entry_mode}</p>
            )}
          </Section>

          {/* --- Glossaire : un score ne doit jamais être un chiffre opaque --- */}
          <Section id="glossaire" title="Ce que mesure chaque indicateur">
            <div className="grid gap-3 md:grid-cols-2">
              {spec.glossary.map((g) => (
                <div key={g.term} className="rounded-lg border border-border/60 bg-background p-3">
                  <div className="text-sm font-medium text-white">{g.term}</div>
                  <p className="mt-1 text-xs leading-relaxed text-muted">{g.body}</p>
                </div>
              ))}
            </div>
          </Section>

          {/* --- Honnêteté des données : la partie qu'on ne met jamais en avant, et qui compte --- */}
          <Section id="refus" title="Ce que la stratégie refuse de faire">
            <ul className="space-y-2">
              {spec.data_honesty.map((line, i) => (
                <li key={i} className="flex gap-2 text-xs leading-relaxed text-muted">
                  <span className="text-warn">•</span>
                  <span>{line}</span>
                </li>
              ))}
            </ul>
          </Section>
        </>
      )}

      {/* --- Application à un symbole --- */}
      <section id="appliquer" className="scroll-mt-6 space-y-4 rounded-xl border border-border bg-surface p-5">
        <h2 className="text-lg font-semibold text-white">L’appliquer maintenant à un symbole</h2>
        <div className="flex flex-wrap items-center gap-3">
          <label className="text-sm text-muted">Marché</label>
          <MarketSelector value={market} onChange={setMarket} includeAll={false} label={null} />
          <label className="text-sm text-muted">Symbole</label>
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}
            className="rounded-lg border border-border bg-background px-3 py-1.5 font-mono text-sm text-white">
            {(symbols.length ? symbols : [{ symbol: 'EUR/USD' }]).map((s) => (
              <option key={s.symbol} value={s.symbol}>{s.symbol}</option>
            ))}
          </select>
          <button onClick={analyse} disabled={busy === 'setup'}
            className="rounded-lg bg-primary px-4 py-1.5 text-sm font-medium text-white disabled:opacity-50">
            {busy === 'setup' ? 'Analyse…' : 'Analyser maintenant'}
          </button>
          <button onClick={validate} disabled={busy === 'wf'}
            className="rounded-lg border border-border px-4 py-1.5 text-sm text-white disabled:opacity-50">
            {busy === 'wf' ? 'Validation…' : 'Valider (walk-forward)'}
          </button>
          <label className="ml-auto flex items-center gap-2 text-sm text-muted">
            <input type="checkbox" checked={autoTrade} onChange={toggleAutoTrade} className="accent-primary" />
            Forward test automatique (compte démo)
          </label>
        </div>

        {setup && (
          <div className="space-y-3 border-t border-border pt-4">
            <div className="flex flex-wrap items-center gap-3">
              <span className={`rounded-lg px-3 py-1 text-sm font-semibold ${
                setup.direction === 'BUY' ? 'bg-buy/15 text-buy'
                  : setup.direction === 'SELL' ? 'bg-sell/15 text-sell' : 'bg-background text-muted'}`}>
                {setup.direction}
              </span>
              <span className="font-mono text-sm text-white">{setup.symbol}</span>
              {setup.ready && setup.entry != null && (
                <span className="font-mono text-xs text-muted">
                  entrée {setup.entry} · SL {setup.stop_loss} · TP1 {setup.take_profit_1}
                  {setup.take_profit_2 != null && ` · TP2 ${setup.take_profit_2}`}
                  {' · '}R/R 1:{setup.risk_reward?.toFixed(2)}
                </span>
              )}
            </div>
            {setup.narrative && (
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-background p-4 text-xs leading-relaxed text-muted">
                {setup.narrative}
              </pre>
            )}
          </div>
        )}

        {wf && (
          <div className="border-t border-border pt-4 text-sm">
            <span className="text-muted">Validation out-of-sample : </span>
            <span className={VERDICT_STYLE[wf.verdict] ?? 'text-muted'}>{wf.label}</span>
            <span className="ml-2 font-mono text-xs text-muted">
              {wf.total_trades} trades · régularité {wf.consistency} · PF {wf.avg_profit_factor}
            </span>
          </div>
        )}
      </section>
    </div>
  );
}

/** Une étape : son résumé, ses facteurs pondérés, ses règles exactes, et ce qui la rend BLOQUANTE. */
function StepCard({ step }: { step: StrategyStep }) {
  return (
    <article className="rounded-xl border border-border bg-surface p-5 space-y-4">
      <header className="flex flex-wrap items-baseline gap-3">
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/20 font-mono text-xs font-bold text-primary">
          {step.n}
        </span>
        <h3 className="text-base font-semibold text-white">{step.title}</h3>
        {step.timeframes?.length ? (
          <span className="rounded bg-background px-2 py-0.5 font-mono text-2xs text-muted">
            {step.timeframes.join(' · ')}
          </span>
        ) : null}
        {step.mode && (
          <span className="rounded bg-background px-2 py-0.5 text-2xs text-muted">mode « {step.mode} »</span>
        )}
      </header>

      <p className="text-sm leading-relaxed text-muted">{step.summary}</p>
      {step.mode_explained && <p className="text-xs leading-relaxed text-muted">{step.mode_explained}</p>}

      {/* Les facteurs et leur poids : c'est là que se voit ce qui décide vraiment. */}
      {step.inputs?.length ? (
        <div className="space-y-1.5">
          {step.inputs.map((f) => (
            <div key={f.key} className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg bg-background px-3 py-2">
              <span className="min-w-[13rem] text-xs font-medium text-white">
                {f.label}
                {f.strong && <span className="ml-1.5 rounded bg-buy/20 px-1.5 py-0.5 text-[10px] text-buy">forte</span>}
              </span>
              {f.weight != null && (
                <span className="flex items-center gap-2">
                  <span className="h-1.5 w-24 overflow-hidden rounded-full bg-border">
                    <span
                      className="block h-full rounded-full bg-primary"
                      style={{ width: `${Math.min(100, (f.weight / 1.0) * 100)}%` }}
                    />
                  </span>
                  <span className="font-mono text-2xs text-muted">
                    {f.weight_pct != null ? `${f.weight_pct} %` : `poids ${f.weight}`}
                  </span>
                </span>
              )}
              <span className="flex-1 text-2xs text-muted">{f.role}</span>
            </div>
          ))}
        </div>
      ) : null}

      {step.timeframe_weights && (
        <p className="text-2xs text-muted">
          Poids des unités de temps dans le score global —{' '}
          {Object.entries(step.timeframe_weights).map(([k, v]) => `${k} ${Math.round(v * 100)} %`).join(' · ')}.
          Le 15 min ne pèse presque rien : il sert au TIMING, pas à la direction.
        </p>
      )}

      {/* La MÉCANIQUE exacte : ce que chaque outil doit voir, et combien il vaut alors. Sans elle,
          un poids affiché ne dit pas ce qui a réellement fait le score. */}
      {step.detail?.length ? (
        <div className="space-y-2 rounded-lg border border-border/60 bg-background/40 p-3">
          <div className="text-xs font-medium text-white/80">Les règles exactes, telles que le moteur les applique</div>
          {step.detail.map((d) => <Note key={d.label} label={d.label} body={d.body} plain />)}
        </div>
      ) : null}

      {step.stop_candidates?.length ? (
        <div>
          <div className="mb-1 text-xs font-medium text-white">Où le stop est posé, par ordre de préférence</div>
          <ol className="space-y-1">
            {step.stop_candidates.map((c, i) => (
              <li key={i} className="flex gap-2 text-xs leading-relaxed text-muted">
                <span className="font-mono text-muted/60">{i + 1}.</span><span>{c}</span>
              </li>
            ))}
          </ol>
          {step.stop_rule && <p className="mt-2 text-xs leading-relaxed text-muted">{step.stop_rule}</p>}
        </div>
      ) : null}

      {/* La fiabilité de chaque type de niveau : c'est elle qui départage, pas la distance. */}
      {step.stop_weights?.length ? (
        <div className="space-y-1.5">
          <div className="text-xs font-medium text-white">Fiabilité de chaque type de niveau porteur de stop</div>
          {step.stop_weights.map((w) => (
            <div key={w.label} className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg bg-background px-3 py-2">
              <span className="min-w-[15rem] text-xs font-medium text-white">{w.label}</span>
              <span className="flex items-center gap-2">
                <span className="h-1.5 w-24 overflow-hidden rounded-full bg-border">
                  <span className="block h-full rounded-full bg-primary"
                    style={{ width: `${Math.min(100, w.weight * 100)}%` }} />
                </span>
                <span className="font-mono text-2xs text-muted">{w.weight.toFixed(2)}</span>
              </span>
              <span className="flex-1 text-2xs leading-relaxed text-muted">{w.why}</span>
            </div>
          ))}
        </div>
      ) : null}

      {step.rules?.length ? (
        <ul className="space-y-1">
          {step.rules.map((r, i) => (
            <li key={i} className="flex gap-2 text-xs leading-relaxed text-muted">
              <span className="text-primary">▸</span><span>{r}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {/* Ce qui BLOQUE : la partie la plus importante d'une stratégie, celle qui dit non. */}
      {step.blocking?.length ? (
        <div className="rounded-lg border border-sell/30 bg-sell/5 p-3">
          <div className="mb-1.5 text-xs font-medium text-sell">Conditions bloquantes — aucun trade si l’une échoue</div>
          <ul className="space-y-1">
            {step.blocking.map((b, i) => (
              <li key={i} className="flex gap-2 text-xs leading-relaxed text-muted">
                <span className="text-sell">✕</span><span>{b}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* Ce qui est CALCULÉ et affiché mais qui ne bloque plus (décision du 28/07/2026) — sans
          cette distinction, un ❌ ici se lirait à tort comme un refus de trade. */}
      {step.informative?.length ? (
        <div className="rounded-lg border border-border bg-background/40 p-3">
          <div className="mb-1.5 text-xs font-medium text-white/80">
            Calculées et affichées, mais NON bloquantes — un ❌ ici informe, il ne refuse rien
          </div>
          <ul className="space-y-1">
            {step.informative.map((b, i) => (
              <li key={i} className="flex gap-2 text-xs leading-relaxed text-muted">
                <span className="text-muted">ⓘ</span><span>{b}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {step.scale_explained && (
        <p className="rounded-lg bg-background p-3 text-xs leading-relaxed text-muted">{step.scale_explained}</p>
      )}
      {step.not_used && (
        <p className="rounded-lg border border-warn/30 bg-warn/5 p-3 text-xs leading-relaxed text-muted">
          {step.not_used}
        </p>
      )}
      {step.measured && (
        <p className="text-xs leading-relaxed text-muted">📏 {step.measured}</p>
      )}
    </article>
  );
}

/** Une section ancrée : le sommaire y renvoie, l'ancre reste lisible sous l'en-tête. */
function Section({ id, title, aside, children }: {
  id: string; title: string; aside?: string; children: React.ReactNode;
}) {
  return (
    <section id={id} className="scroll-mt-6 space-y-4 rounded-xl border border-border bg-surface p-5">
      <div className="flex flex-wrap items-baseline gap-3">
        <h2 className="text-lg font-semibold text-white">{title}</h2>
        {aside && <span className="text-xs text-muted">{aside}</span>}
      </div>
      {children}
    </section>
  );
}

/** Un paragraphe titré. `plain` retire le cadre quand il est déjà dans un encadré. */
function Note({ label, body, plain }: StrategyNote & { plain?: boolean }) {
  return (
    <div className={plain ? '' : 'rounded-lg border border-border/60 bg-background p-3'}>
      <div className="text-xs font-medium text-white">{label}</div>
      <p className="mt-1 text-xs leading-relaxed text-muted">{body}</p>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <div className="mt-1 text-sm text-white">{value}</div>
    </div>
  );
}

/** Durées de cache lisibles : « 60 s », « 15 min », « 6 h » — jamais 21600. */
function fmtSeconds(s: number): string {
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${Math.round(s / 3600)} h`;
}
