'use client';

import { useEffect, useState } from 'react';
import { api, JournalEntry, JournalInsights, PlanInfo } from '@/lib/api';
import { PageHeader, Button } from '@/components/ui';

export default function JournalPage() {
  const [plan, setPlan] = useState<PlanInfo | null>(null);
  const [entries, setEntries] = useState<JournalEntry[]>([]);
  const [insights, setInsights] = useState<JournalInsights | null>(null);
  const [explanations, setExplanations] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const [e, i] = await Promise.all([api.journalList(), api.journalInsights()]);
      setEntries(e);
      setInsights(i);
    } catch (err: any) {
      setError(err.message ?? 'Erreur');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    api.myPlan().then(setPlan).catch(() => {});
    load();
  }, []);

  async function close(id: string, outcome: string) {
    const pnlStr = prompt(`P&L réalisé pour ce trade (${outcome}) ?`, '0');
    if (pnlStr === null) return;
    await api.journalClose(id, outcome, parseFloat(pnlStr) || 0);
    load();
  }

  async function explain(id: string) {
    setExplanations((m) => ({ ...m, [id]: 'Analyse en cours…' }));
    const r = await api.journalExplain(id);
    setExplanations((m) => ({ ...m, [id]: r.explanation }));
  }

  async function autoResolve() {
    await api.journalAutoResolve();
    load();
  }

  const locked = plan && !plan.features.journal;
  if (locked)
    return (
      <div className="p-8">
        <div className="rounded-xl border border-yellow-500/30 bg-yellow-500/10 p-6 text-yellow-200">
          Le Journal d&apos;apprentissage est réservé au plan <b>Pro</b>.{' '}
          <a href="/plans" className="underline">Mettre à niveau</a>
        </div>
      </div>
    );

  if (loading) return <div className="p-8 text-white">Chargement…</div>;

  const badge = (o: string) =>
    o === 'win' ? 'bg-buy/20 text-buy' : o === 'loss' ? 'bg-sell/20 text-sell' : 'bg-muted/20 text-muted';

  return (
    <div className="p-8 space-y-6">
      <PageHeader
        title="Journal & Apprentissage"
        actions={
          <Button variant="secondary" size="sm" onClick={autoResolve}>
            Résoudre les signaux ouverts
          </Button>
        }
      />

      {error && <p className="text-sell">{error}</p>}

      {insights && (
        <section className="grid grid-cols-2 gap-4 md:grid-cols-4">
          {[
            ['Trades clôturés', insights.stats.closed],
            ['Taux de réussite', `${insights.stats.win_rate}%`],
            ['P&L cumulé', insights.stats.total_pnl],
            ['Ouverts', insights.stats.open],
          ].map(([label, val]) => (
            <div key={label} className="rounded-xl border border-border bg-surface p-4">
              <p className="text-xs text-muted">{label}</p>
              <p className="mt-1 text-xl text-white">{val}</p>
            </div>
          ))}
        </section>
      )}

      {insights && (
        <section className="rounded-xl border border-border bg-surface p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-white">Apprentissage — fiabilité par agent</h2>
            <span className="text-xs text-muted">{insights.trades_learned ?? 0} trade(s) appris</span>
          </div>
          {insights.reliability_source === 'training' && (
            <p className="mb-2 text-[11px] text-muted">
              ⓘ Aucun signal généré via le dashboard pour l&apos;instant : la fiabilité ci-dessous
              vient du walk-forward nocturne de la stratégie (positions démo), pas d&apos;un vote
              d&apos;agents en direct.
            </p>
          )}
          {insights.reliability && insights.reliability.length > 0 ? (
            <div className="space-y-2">
              {insights.reliability.map((r) => (
                <div key={r.agent} className="flex items-center gap-3 text-xs">
                  <span className="w-24 capitalize text-gray-200">{r.agent}</span>
                  <div className="h-2 flex-1 overflow-hidden rounded bg-background">
                    <div
                      className={`h-full ${r.low_sample ? 'bg-muted' : r.hit_rate >= 50 ? 'bg-buy' : 'bg-sell'}`}
                      style={{ width: `${Math.min(100, r.hit_rate)}%` }}
                    />
                  </div>
                  <span className="w-14 text-right text-white">{r.hit_rate}%</span>
                  <span className="w-16 text-right text-muted" title={r.low_sample ? 'Échantillon faible : taux peu fiable statistiquement' : ''}>
                    n={r.samples}{r.low_sample ? ' ⚠️' : ''}
                  </span>
                  <span className={`w-14 text-right ${r.multiplier >= 1 ? 'text-buy' : 'text-sell'}`}>×{r.multiplier.toFixed(2)}</span>
                </div>
              ))}
              <p className="mt-2 text-[11px] text-muted">
                Le poids de chaque agent (×) s&apos;ajuste selon sa réussite passée — l&apos;effet se renforce avec le volume de trades.
                C&apos;est ce que le Master applique pour des signaux plus fiables.
              </p>
              {insights.reliability.some((r) => r.low_sample) && (
                <p className="text-[11px] text-yellow-300/80">
                  ⚠️ « n » faible (&lt;10) = taux encore peu fiable : quelques trades suffisent à afficher 0% ou 100% par hasard.
                  Le multiplicateur reste volontairement prudent tant que le volume est faible.
                </p>
              )}
            </div>
          ) : (
            <p className="text-xs text-muted">
              Pas encore assez de trades clôturés. Génère des signaux depuis le dashboard (ou
              laisse l&apos;auto-entrée ouvrir des positions démo, puis attends le prochain
              entraînement nocturne) : l&apos;apprentissage démarre dès quelques trades et s&apos;affine ensuite.
            </p>
          )}
        </section>
      )}

      <section className="space-y-3">
        <h2 className="text-lg font-semibold text-white">Trades ({entries.length})</h2>
        {entries.length === 0 && <p className="text-muted">Aucune entrée. Génère un signal depuis le dashboard, ou laisse l&apos;auto-entrée ouvrir une position démo.</p>}
        {entries.map((e) => {
          const isPlaybook = e.source === 'playbook';
          return (
          <div key={e.id} className="rounded-xl border border-border bg-surface p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-3">
                <span className="font-medium text-white">{e.symbol}</span>
                <span className="text-sm text-muted">{e.direction}</span>
                <span className={`rounded px-2 py-0.5 text-xs ${badge(e.outcome)}`}>{e.outcome}</span>
                {isPlaybook && (
                  <span className="rounded bg-accent/15 px-2 py-0.5 text-xs text-accent" title="Position ouverte par la stratégie (auto-entrée ou « Ouvrir en démo »)">
                    playbook
                  </span>
                )}
                {e.pnl != null && <span className="text-sm text-gray-300">P&L {e.pnl}</span>}
                {e.pips != null && (
                  <span className={`rounded px-2 py-0.5 font-mono text-xs ${e.pips >= 0 ? 'bg-buy/15 text-buy' : 'bg-sell/15 text-sell'}`}
                    title={`${e.pips >= 0 ? 'Gagné' : 'Perdu'} ${Math.abs(e.pips)} ${e.pips_label ?? 'pips'}`}>
                    {e.pips >= 0 ? '+' : ''}{e.pips} pips
                  </span>
                )}
              </div>
              {/* Un trade playbook se ferme depuis Paper Trading (« Clôturer maintenant » /
                  « Modifier SL/TP ») — ce n'est pas un enregistrement du journal des signaux,
                  clore ou expliquer ici échouerait silencieusement. */}
              {isPlaybook ? (
                <a href="/execution" className="text-xs text-accent underline underline-offset-2">
                  Gérer dans Paper Trading →
                </a>
              ) : (
                <div className="flex gap-2">
                  {e.outcome === 'open' && (
                    <>
                      <button onClick={() => close(e.id, 'win')} className="rounded border border-buy/40 px-2 py-1 text-xs text-buy hover:bg-buy/10">
                        Gain
                      </button>
                      <button onClick={() => close(e.id, 'loss')} className="rounded border border-sell/40 px-2 py-1 text-xs text-sell hover:bg-sell/10">
                        Perte
                      </button>
                    </>
                  )}
                  <button onClick={() => explain(e.id)} className="rounded border border-border px-2 py-1 text-xs text-gray-200 hover:bg-[#1A1A1A]">
                    Expliquer (IA)
                  </button>
                </div>
              )}
            </div>
            {/* Heure d'ENTRÉE (et de sortie si le trade est fermé) : un journal sans horodatage
                ne permet ni de rattacher un trade à une séance, ni d'en juger la durée. */}
            {e.created_at && (
              <p className="mt-1 text-xs text-muted">
                Entrée le <span className="text-gray-300">{new Date(e.created_at).toLocaleString('fr-FR')}</span>
                {e.closed_at && <> · sortie le <span className="text-gray-300">{new Date(e.closed_at).toLocaleString('fr-FR')}</span></>}
              </p>
            )}
            {explanations[e.id] && (
              <p className="mt-3 whitespace-pre-wrap rounded-lg bg-[#1A1A1A] p-3 text-sm text-gray-300">{explanations[e.id]}</p>
            )}
          </div>
          );
        })}
      </section>
    </div>
  );
}
