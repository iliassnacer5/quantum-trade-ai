'use client';

/**
 * Supervision des agents — et surtout : CE QU'ILS ONT APPRIS.
 *
 * Chaque nuit, la stratégie du desk est rejouée sur l'historique réel (walk-forward). On en tire,
 * pour chaque agent, la justesse MESURÉE de ses arguments, le multiplicateur de poids qui en
 * découle, et une fiche d'expertise : trois règles opératoires issues de ses propres chiffres.
 *
 * Rien n'est affiché ici qui ne soit mesuré. Tant que l'entraînement n'a pas tourné, on le dit
 * franchement plutôt que d'afficher des scores flatteurs.
 */

import { useCallback, useEffect, useState } from 'react';
import { api, type AgentStatus, type TrainingReport } from '@/lib/api';
import { refreshLabel, useAutoRefresh } from '@/lib/useAutoRefresh';
import { Button, PageHeader } from '@/components/ui';

const REFRESH_MS = 10_000;

/** Multiplicateur de poids : au-dessus de 1, l'agent a eu raison plus souvent que le hasard. */
function MultiplierBadge({ value }: { value: number }) {
  const tone = value > 1.02 ? 'bg-buy/15 text-buy' : value < 0.98 ? 'bg-sell/15 text-sell' : 'bg-border text-muted';
  return <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${tone}`}>poids ×{value}</span>;
}

function MetricsRow({ label, m }: { label: string; m: { trades: number; win_rate: number; expectancy_r: number; profit_factor: number | null } }) {
  return (
    <tr className="border-t border-border/60">
      <td className="py-1.5 pr-3 font-mono text-white/90">{label}</td>
      <td className="py-1.5 pr-3 text-right text-muted">{m.trades}</td>
      <td className="py-1.5 pr-3 text-right text-white/80">{m.win_rate}%</td>
      <td className={`py-1.5 pr-3 text-right ${m.expectancy_r > 0 ? 'text-buy' : 'text-sell'}`}>
        {m.expectancy_r > 0 ? '+' : ''}{m.expectancy_r} R
      </td>
      <td className="py-1.5 text-right text-muted">{m.profit_factor ?? '—'}</td>
    </tr>
  );
}

function MetricsTable({ title, data, note }: { title: string; data: Record<string, any>; note?: string }) {
  const rows = Object.entries(data ?? {}).sort((a, b) => b[1].expectancy_r - a[1].expectancy_r);
  if (rows.length === 0) return null;
  return (
    <div className="rounded-xl border border-border bg-surface p-4">
      <h3 className="text-sm font-semibold text-white">{title}</h3>
      {note && <p className="mt-0.5 text-[11px] text-muted">{note}</p>}
      <div className="mt-2 overflow-x-auto">
        <table className="w-full min-w-[420px] text-xs">
          <thead>
            <tr className="text-[10px] uppercase tracking-wide text-muted">
              <th className="pb-1 text-left font-medium">Clé</th>
              <th className="pb-1 text-right font-medium">Trades</th>
              <th className="pb-1 text-right font-medium">Réussite</th>
              <th className="pb-1 text-right font-medium">Espérance</th>
              <th className="pb-1 text-right font-medium">PF</th>
            </tr>
          </thead>
          <tbody>{rows.map(([k, m]) => <MetricsRow key={k} label={k} m={m} />)}</tbody>
        </table>
      </div>
    </div>
  );
}

export default function AgentsPage() {
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [training, setTraining] = useState<TrainingReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, t] = await Promise.all([api.agentsStatus(), api.training()]);
      setStatus(s);
      setTraining(t);
      setError(null);
    } catch (e: any) {
      setError(e.message);
      throw e;
    } finally {
      setLoading(false);
    }
  }, []);

  const auto = useAutoRefresh(load, REFRESH_MS);

  useEffect(() => {
    void load();
  }, [load]);

  async function runTraining() {
    setBusy(true);
    setError(null);
    try {
      setTraining(await api.runTraining());
      void load();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <div className="p-8 text-white">Chargement…</div>;

  const strat = status?.strategy;
  const trained = training?.trained;

  return (
    <div className="space-y-6 p-8">
      <PageHeader
        title="Agents IA — supervision et entraînement"
        subtitle={
          `Tous les agents appliquent LA MÊME stratégie : tendance 4 h + 1 h (le journalier ` +
          `pèse sans être obligatoire) → entrée 15 min · ` +
          `objectif ≥ ${strat?.min_target_pips ?? 50} pips · ` +
          `R/R 1:${strat?.min_risk_reward ?? 2} à 1:${strat?.max_risk_reward ?? 3} · ` +
          `stop sécurisé à +${strat?.secure_at_r ?? 2}R.`
        }
        actions={
          <Button size="sm" onClick={runTraining} loading={busy}>
            {busy ? 'Entraînement en cours…' : 'Lancer un entraînement'}
          </Button>
        }
      />
      <p className="text-[11px] text-muted">{refreshLabel(auto)}</p>

      {error && <p className="text-sell">{error}</p>}

      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <div className="rounded-xl border border-border bg-surface p-4">
          <h2 className="text-xs text-muted">Statut système</h2>
          <div className="mt-1 flex items-center gap-2">
            <div className={`h-3 w-3 rounded-full ${status?.status === 'online' ? 'bg-buy' : 'bg-sell'}`} />
            <span className="text-lg capitalize text-white">{status?.status ?? 'inconnu'}</span>
          </div>
        </div>
        <div className="rounded-xl border border-border bg-surface p-4">
          <h2 className="text-xs text-muted">Moteur LLM</h2>
          <div className="mt-1 flex items-center gap-2">
            <div className={`h-3 w-3 rounded-full ${status?.llm_enabled ? 'bg-buy' : 'bg-yellow-500'}`} />
            <span className="text-lg text-white">
              {status?.llm_enabled ? 'Activé (hybride)' : 'Déterministe'}
            </span>
          </div>
        </div>
        <div className="rounded-xl border border-border bg-surface p-4">
          <h2 className="text-xs text-muted">Auto-entrée</h2>
          <div className="mt-1 flex items-center gap-2">
            <div className={`h-3 w-3 rounded-full ${strat?.auto_entry ? 'bg-buy' : 'bg-border'}`} />
            <span className="text-lg text-white">{strat?.auto_entry ? 'Active' : 'Désactivée'}</span>
          </div>
          <p className="mt-0.5 text-[11px] text-muted">
            {strat?.auto_entry
              ? `Ouverture automatique en compte ${strat.auto_entry_mode ?? 'paper'} dès le déclencheur 15 min.`
              : 'Les setups armés attendent une ouverture manuelle.'}
          </p>
        </div>
      </div>

      {/* ---- Entraînement du jour ---- */}
      <section className="space-y-3">
        <h2 className="text-lg font-semibold text-white">🎓 Entraînement quotidien sur la stratégie</h2>
        {!trained ? (
          <div className="rounded-xl border border-border bg-surface p-6 text-sm text-muted">
            {training?.note ??
              "L'entraînement n'a pas encore tourné. Les agents utilisent leurs poids de base — " +
                'aucune statistique mesurée n\'est affichée tant qu\'elle n\'existe pas.'}
          </div>
        ) : (
          <>
            <div className="rounded-xl border border-accent/40 bg-accent/5 p-4 text-sm">
              <p className="text-white">
                Walk-forward du {training?.date} — <strong>{training?.trades}</strong> trades rejoués
                sur <strong>{training?.symbols_trained}</strong> symboles en {training?.duration_s} s.
              </p>
              {training?.overall && (
                <p className="mt-1 text-xs text-muted">
                  Réussite mesurée <span className="text-white/85">{training.overall.win_rate}%</span> ·
                  espérance{' '}
                  <span className={training.overall.expectancy_r > 0 ? 'text-buy' : 'text-sell'}>
                    {training.overall.expectancy_r > 0 ? '+' : ''}{training.overall.expectancy_r} R
                  </span>{' '}
                  par trade · profit factor {training.overall.profit_factor ?? '—'}. La stratégie est
                  rejouée sans jamais regarder le futur : à chaque bougie 15 min, seules les données
                  déjà disponibles sont utilisées.
                </p>
              )}
              <p className="mt-1 text-[11px] text-muted">
                Un résultat mesuré n&apos;est pas une promesse : il décrit le passé rejoué, avec ses
                hypothèses (le stop l&apos;emporte quand stop et objectif tombent dans la même bougie).
              </p>
            </div>

            <div className="grid gap-3 lg:grid-cols-2">
              <MetricsTable
                title="Par symbole"
                data={training?.by_symbol ?? {}}
                note={`Seules les lignes d'au moins ${training?.min_trades} trades servent au classement des trades du jour.`}
              />
              <MetricsTable
                title="Par déclencheur 15 min"
                data={training?.by_trigger ?? {}}
                note="Repli sur MA / zone d'or, cassure confirmée, ou divergence."
              />
              <MetricsTable
                title="Par fenêtre de session"
                data={training?.by_session ?? {}}
                note="Ouverture de Londres, ouverture de New York, chevauchement, hors fenêtre."
              />
              {training?.factor_competence && Object.keys(training.factor_competence).length > 0 && (
                <div className="rounded-xl border border-border bg-surface p-4">
                  <h3 className="text-sm font-semibold text-white">Justesse de chaque facteur</h3>
                  <p className="mt-0.5 text-[11px] text-muted">
                    À quelle fréquence l&apos;argument de ce facteur était le bon, quand le trade
                    s&apos;est réellement joué.
                  </p>
                  <ul className="mt-2 space-y-1 text-xs">
                    {Object.entries(training.factor_competence)
                      .sort((a, b) => b[1].accuracy - a[1].accuracy)
                      .map(([key, f]) => (
                        <li key={key} className="flex items-center justify-between border-t border-border/60 py-1">
                          <span className="text-white/85">{key}</span>
                          <span className={f.accuracy >= 50 ? 'text-buy' : 'text-sell'}>
                            {f.accuracy}% <span className="text-muted">({f.observations} obs.)</span>
                          </span>
                        </li>
                      ))}
                  </ul>
                </div>
              )}
            </div>
          </>
        )}
      </section>

      {/* ---- Agents ---- */}
      <section className="space-y-3">
        <h2 className="text-lg font-semibold text-white">
          Agents actifs ({status?.agents.length ?? 0})
        </h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {status?.agents.map((agent) => (
            <div key={agent.name} className="flex flex-col rounded-xl border border-border bg-surface p-4">
              <div className="flex items-start justify-between gap-2">
                <h3 className="font-medium capitalize text-white">{agent.name}</h3>
                {agent.competence && <MultiplierBadge value={agent.competence.multiplier} />}
              </div>
              <p className="mt-1 text-xs text-muted">{agent.desc}</p>

              {agent.competence ? (
                <p className="mt-2 text-[11px] text-white/70">
                  🎯 Justesse mesurée{' '}
                  <span className={agent.competence.accuracy >= 50 ? 'text-buy' : 'text-sell'}>
                    {agent.competence.accuracy}%
                  </span>{' '}
                  sur {agent.competence.observations} observations de cette stratégie
                  (entraînement du {agent.competence.trained_on}).
                </p>
              ) : (
                <p className="mt-2 text-[11px] text-muted">
                  Pas encore de compétence mesurée sur cette stratégie.
                </p>
              )}

              {agent.expertise && (
                <p className="mt-2 whitespace-pre-line rounded bg-background/60 p-2 text-[11px] leading-relaxed text-gray-300">
                  {agent.expertise}
                </p>
              )}

              <div className="mt-auto flex flex-col gap-1 pt-3">
                <span className="truncate font-mono text-[10px] text-muted">{agent.model}</span>
                <span className="w-fit rounded bg-buy/10 px-2 py-1 text-xs text-buy">Actif</span>
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
