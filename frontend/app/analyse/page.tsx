'use client';

/**
 * ANALYSE QUOTIDIENNE DES MARCHÉS — l'avis du modèle sur le forex et l'or, HORS stratégie du desk.
 *
 * À ne pas confondre avec les « Trades du jour », qui répondent à une autre question : « y a-t-il
 * un trade conforme à la MÉTHODE, maintenant ? ». Ici le modèle donne sa LECTURE de chaque marché,
 * sans que la stratégie puisse opposer son veto — sinon ce second regard ne ferait que répéter la
 * stratégie, et n'apporterait rien.
 *
 * Chaque avis est affiché avec TOUT ce qui l'a produit : le détail agent par agent, la pesée du
 * Master, les indicateurs mesurés. Un avis sans son raisonnement n'est pas vérifiable.
 */

import { useCallback, useEffect, useState } from 'react';
import { api, type DailyAnalysis, type MarketOpinion } from '@/lib/api';
import { Button, Card, PageHeader } from '@/components/ui';

const STANCE_COLOR: Record<string, string> = {
  BUY: 'text-buy',
  SELL: 'text-sell',
  HOLD: 'text-muted',
};

function Stat({ label, value, tone }: { label: string; value: string; tone?: 'buy' | 'sell' }) {
  return (
    <div className="rounded-lg border border-border bg-background/40 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
      <div className={`text-sm font-semibold ${
        tone === 'buy' ? 'text-buy' : tone === 'sell' ? 'text-sell' : 'text-white'
      }`}>{value}</div>
    </div>
  );
}

/** Une valeur d'indicateur, rendue lisible quelle que soit sa forme (nombre, objet, texte). */
function renderMetric(value: unknown): string {
  if (value == null) return '—';
  if (typeof value === 'number') return String(Math.round(value * 1e6) / 1e6);
  if (typeof value === 'boolean') return value ? 'oui' : 'non';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function OpinionCard({ o }: { o: MarketOpinion }) {
  const [open, setOpen] = useState(false);

  // Un symbole sans données n'est pas masqué : on le montre avec son motif, plutôt que de laisser
  // croire qu'il n'existe pas ou, pire, de lui inventer un avis.
  if (o.error) {
    return (
      <Card className="p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="font-mono text-white">{o.symbol}</span>
          <span className="text-xs text-warn">Aucun avis — {o.error}</span>
        </div>
      </Card>
    );
  }

  const dirColor = STANCE_COLOR[o.direction ?? 'HOLD'] ?? 'text-muted';
  const metricEntries = Object.entries(o.metrics ?? {}).filter(
    ([k]) => !['levels_source', 'target_pips', 'stop_pips', 'pips_label'].includes(k),
  );

  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div className="flex items-baseline gap-3">
          <span className="font-mono text-base text-white">{o.symbol}</span>
          <span className={`text-sm font-semibold uppercase ${dirColor}`}>{o.stance}</span>
          <span className="text-xs text-muted">{o.conviction}</span>
        </div>
        <div className="flex items-center gap-3 text-xs text-muted">
          <span>Confiance <span className="text-white">{o.confidence}/100</span></span>
          <span>Consensus <span className="text-white">{o.consensus_pct} %</span></span>
          {o.price != null && <span>Prix <span className="text-white">{o.price}</span></span>}
        </div>
      </div>

      <p className="mt-2 text-[12.5px] leading-relaxed text-gray-300">{o.rationale}</p>

      <button
        onClick={() => setOpen((v) => !v)}
        className="mt-2 text-[11px] text-accent underline underline-offset-2"
      >
        {open ? 'Masquer le détail' : "Voir ce qui a produit cet avis (agents, pesée, indicateurs)"}
      </button>

      {open && (
        <div className="mt-3 space-y-3">
          {/* Le vote de chaque agent — la matière première de l'avis. */}
          <div>
            <h4 className="mb-1 text-xs font-semibold text-white">Ce que dit chaque agent</h4>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[520px] text-[11px]">
                <thead>
                  <tr className="text-[10px] uppercase tracking-wide text-muted">
                    <th className="pb-1 text-left font-medium">Agent</th>
                    <th className="pb-1 text-right font-medium">Score</th>
                    <th className="pb-1 text-right font-medium">Confiance</th>
                    <th className="pb-1 text-left font-medium">Justification</th>
                  </tr>
                </thead>
                <tbody>
                  {(o.agents ?? []).map((a) => (
                    <tr key={a.name} className="border-t border-border/40 align-top">
                      <td className="py-1 pr-2 font-medium text-white/90">{a.name}</td>
                      <td className={`py-1 pr-2 text-right ${a.score > 0 ? 'text-buy' : a.score < 0 ? 'text-sell' : 'text-muted'}`}>
                        {a.score > 0 ? '+' : ''}{Math.round(a.score * 100) / 100}
                      </td>
                      <td className="py-1 pr-2 text-right text-muted">
                        {Math.round((a.confidence ?? 0) * 100)} %
                      </td>
                      <td className="py-1 text-muted">{a.rationale}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* La PESÉE du Master : comment ces votes deviennent une décision. */}
          {o.master && (
            <div className="rounded-lg border border-border bg-background/40 p-3">
              <h4 className="mb-1 text-xs font-semibold text-white">Comment la décision est prise</h4>
              <p className="text-[11px] leading-relaxed text-muted">
                Score combiné <span className="text-white">{o.master.score}</span> — au-dessus de{' '}
                <span className="text-white">+{o.master.threshold}</span> c&apos;est haussier, en
                dessous de <span className="text-white">−{o.master.threshold}</span> c&apos;est
                baissier, entre les deux le modèle ne tranche pas.
                {o.master.conflict && (
                  <span className="text-warn"> Des agents se contredisent : la conviction est réduite.</span>
                )}
              </p>
              {o.master.weights_used && (
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {Object.entries(o.master.weights_used).map(([name, w]) => (
                    <span key={name} className="rounded bg-background px-2 py-0.5 font-mono text-[10px] text-muted">
                      {name} {Math.round(w * 100) / 100}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Les indicateurs MESURÉS, tels quels. */}
          {metricEntries.length > 0 && (
            <div>
              <h4 className="mb-1 text-xs font-semibold text-white">Indicateurs mesurés</h4>
              <div className="flex flex-wrap gap-1.5">
                {metricEntries.map(([k, v]) => (
                  <span key={k} className="rounded border border-border bg-background px-2 py-0.5 font-mono text-[10px] text-muted">
                    {k} <span className="text-white/80">{renderMetric(v)}</span>
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Niveaux indicatifs — clairement nommés comme tels. */}
          {o.levels && o.direction !== 'HOLD' && (
            <p className="text-[11px] leading-relaxed text-muted">
              Échelle du mouvement envisagé (INDICATIVE, calculée sur l&apos;ATR — ce ne sont pas
              des ordres à passer, la stratégie du desk garde seule la décision de trader) : entrée{' '}
              <span className="text-white">{o.levels.entry}</span>, stop{' '}
              <span className="text-sell">{o.levels.stop_loss}</span>, objectif{' '}
              <span className="text-buy">{o.levels.take_profit_1}</span> (R/R 1:{o.levels.risk_reward}).
            </p>
          )}
        </div>
      )}
    </Card>
  );
}

export default function AnalysePage() {
  const [data, setData] = useState<DailyAnalysis | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.dailyAnalysis());
      setError(null);
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      await api.runDailyAnalysis();
      await load();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const s = data?.summary;

  return (
    <div className="space-y-6 p-8">
      <PageHeader
        title="Analyse quotidienne des marchés"
        subtitle="L'avis du modèle sur le forex et l'or, produit chaque jour EN DEHORS de la stratégie du desk."
      />

      <Card className="border-accent/40 bg-accent/5 p-4">
        <p className="text-sm text-white">Pourquoi cette page est séparée des « Trades du jour »</p>
        <p className="mt-1 text-[11.5px] leading-relaxed text-muted">
          La stratégie du desk répond à une seule question : « y a-t-il un trade conforme à la
          méthode, maintenant ? ». Elle refuse presque tout, et c&apos;est son rôle. Cette page
          répond à l&apos;autre question : <strong className="text-white/90">quelle lecture le
          modèle fait-il de chaque marché aujourd&apos;hui ?</strong> Elle est produite par les
          mêmes agents, mais <strong className="text-white/90">sans le playbook</strong> — son droit
          de veto ne s&apos;applique pas ici. Les deux peuvent diverger : c&apos;est précisément
          l&apos;intérêt d&apos;un second regard.
        </p>
      </Card>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="text-xs text-muted">
          {data?.available ? (
            <>
              Analyse du <span className="text-white">{data.date}</span>
              {data.generated_at && (
                <> · produite à {new Date(data.generated_at).toLocaleTimeString('fr-FR')}</>
              )}
              {data.duration_s != null && <> · calcul {data.duration_s} s</>}
              {data.stale && (
                <span className="ml-2 rounded bg-warn-soft/30 px-2 py-0.5 text-warn">
                  Pas encore mise à jour aujourd&apos;hui
                </span>
              )}
            </>
          ) : (
            'Aucune analyse pour l’instant.'
          )}
        </div>
        <Button size="sm" onClick={run} loading={busy}>
          {busy ? 'Analyse en cours…' : 'Relancer l’analyse'}
        </Button>
      </div>

      {error && <p className="text-sell">{error}</p>}

      {!data?.available ? (
        <Card className="p-6 text-sm text-muted">
          {data?.note ?? 'Chargement…'}
          {data?.universe && (
            <p className="mt-2 text-[11px]">
              Instruments suivis : <span className="font-mono">{data.universe.join(' · ')}</span>
            </p>
          )}
        </Card>
      ) : (
        <>
          {s && (
            <Card className="p-4">
              <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
                <Stat label="Instruments analysés" value={String(s.analysed)} />
                <Stat label="Haussiers" value={String(s.bullish ?? 0)} tone="buy" />
                <Stat label="Baissiers" value={String(s.bearish ?? 0)} tone="sell" />
                <Stat label="Sans direction" value={String(s.neutral ?? 0)} />
              </div>
              <p className="mt-2 text-[11.5px] leading-relaxed text-gray-300">{s.note}</p>
            </Card>
          )}

          <div className="space-y-3">
            {(data.opinions ?? []).map((o) => <OpinionCard key={o.symbol} o={o} />)}
          </div>

          <div className="space-y-1 text-[11px] leading-relaxed text-muted">
            <p>ⓘ {data.method}</p>
            <p>⚠️ {data.disclaimer}</p>
          </div>
        </>
      )}
    </div>
  );
}
