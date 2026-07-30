'use client';

/**
 * 🗺️ CARTE DE L'EDGE — la réponse à « où est-ce que je gagne ? », mesurée et remise à jour chaque nuit.
 * Chaque combo (stratégie × symbole × timeframe) est validé au walk-forward AVEC frais :
 *   🟢 alpha>0 + PF≥1,2 = exploitable (seuls combos autorisés à l'auto-trading papier)
 *   🟡 alpha>0          = à surveiller
 *   🔴 pas d'edge       = à éviter — s'abstenir est une décision.
 */
import { useEffect, useState } from 'react';
import { api, EdgeMap, EdgeRow } from '@/lib/api';
import { PageHeader, Button, Segmented, RouteTabs, PROVE_TABS } from '@/components/ui';
import { EdgeStatusDot, PairVerdictsPanel } from '@/components/domain';

const MARKET_LABEL: Record<string, string> = {
  crypto: '₿ Crypto', forex: '💱 Forex', stock: '📈 Actions', commodity: '🥇 Or & Métaux',
  // Les indices étaient absents de cette table : leur section s'affichait sous la clé brute
  // « index » alors que la stratégie les trade comme les autres.
  index: '📊 Indices',
};

// Couleur d'une cellule de heatmap selon statut + intensité (|alpha|).
function cellStyle(r: EdgeRow | undefined): { className: string; style?: React.CSSProperties } {
  if (!r) return { className: 'bg-background/40 text-muted/40' };
  const intensity = Math.min(1, Math.abs(r.alpha) / 15);
  if (r.status === 'green') return { className: 'text-buy', style: { background: `rgba(29,158,117,${0.15 + intensity * 0.55})` } };
  if (r.status === 'yellow') return { className: 'text-warn', style: { background: `rgba(224,166,60,${0.12 + intensity * 0.4})` } };
  return { className: 'text-sell', style: { background: `rgba(226,75,74,${0.1 + intensity * 0.35})` } };
}

export default function EdgePage() {
  const [data, setData] = useState<EdgeMap | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [view, setView] = useState<'heatmap' | 'table'>('heatmap');

  function load() {
    api.edgeMap().then(setData).catch((e) => setError(e.message));
  }
  useEffect(load, []);

  async function runNow() {
    setRunning(true);
    setError(null);
    try {
      setData(await api.runEdgeSweep());
    } catch (e: any) {
      setError(e.message?.includes('402') ? 'Réservé au plan Pro+.' : e.message);
    } finally {
      setRunning(false);
    }
  }

  const rows = (data?.rows ?? []).filter((r) => !statusFilter || r.status === statusFilter);
  const byMarket = rows.reduce((acc: Record<string, typeof rows>, r) => {
    (acc[r.market] ??= []).push(r);
    return acc;
  }, {});

  return (
    <div className="p-8 space-y-5">
      <PageHeader
        title="🗺️ Carte de l’edge"
        subtitle={
          <>
            Où gagne-t-on <b className="text-white">vraiment</b> ? Chaque combo est validé au walk-forward
            (out-of-sample, frais inclus) et re-mesuré chaque nuit. L’auto-trading papier ne prend que les 🟢.
          </>
        }
        actions={
          <Button size="sm" onClick={runNow} loading={running}>
            {running ? 'Sweep en cours (1-3 min)…' : '🔄 Relancer le sweep'}
          </Button>
        }
      />
      <RouteTabs items={PROVE_TABS} />

      {/* Verdicts par paire de la STRATÉGIE DU DESK (plan 2.7) — c'est eux qui gouvernent
          l'auto-entrée et le sizing, avec les chiffres qui les motivent. */}
      <PairVerdictsPanel />

      {error && <p className="text-sell">{error}</p>}

      {data && (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <span className="rounded-lg border border-buy/40 bg-buy/10 px-3 py-1.5 text-sm text-buy">🟢 {data.greens} exploitables</span>
            <span className="rounded-lg border border-yellow-500/40 bg-yellow-500/10 px-3 py-1.5 text-sm text-yellow-300">🟡 {data.yellows} à surveiller</span>
            <span className="rounded-lg border border-sell/40 bg-sell/10 px-3 py-1.5 text-sm text-sell">🔴 {data.reds} sans edge</span>
            {data.generated_at && <span className="text-xs text-muted">MàJ : {new Date(data.generated_at).toLocaleString('fr-FR')}</span>}
          </div>
          <p className="text-sm text-white">{data.note}</p>

          {/* Ce que LA STRATÉGIE gagne, à distinguer du walk-forward affiché en dessous : celui-ci
              répond « bat-on le buy & hold ? », celle-là « que rapporte la méthode par trade, et à
              quelle cadence ? ». Deux questions différentes, deux colonnes différentes. */}
          {data.playbook_summary && (
            <section className="rounded-xl border border-primary/30 bg-primary/5 p-4">
              <h2 className="mb-1 text-sm font-semibold text-white">
                📐 {data.strategy ?? 'Stratégie du desk'} — ce qu’elle mesure
              </h2>
              {data.playbook_summary.measured ? (
                <div className="flex flex-wrap gap-x-6 gap-y-1 font-mono text-xs text-muted">
                  <span>Symboles mesurés <span className="text-white">{data.playbook_summary.symbols}</span></span>
                  <span>Trades <span className="text-white">{data.playbook_summary.trades}</span></span>
                  <span>
                    Espérance{' '}
                    <span className={(data.playbook_summary.expectancy_r ?? 0) > 0 ? 'text-buy' : 'text-sell'}>
                      {(data.playbook_summary.expectancy_r ?? 0) > 0 ? '+' : ''}{data.playbook_summary.expectancy_r} R
                    </span>
                  </span>
                  <span>Cadence <span className="text-white">{data.playbook_summary.trades_per_day} / jour · {data.playbook_summary.trades_per_week} / semaine</span></span>
                </div>
              ) : null}
              <p className="mt-1 text-[11px] leading-relaxed text-muted">{data.playbook_summary.note}</p>
            </section>
          )}

          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex gap-1">
              {[{ id: '', l: 'Tous' }, { id: 'green', l: '🟢 Verts' }, { id: 'yellow', l: '🟡 Jaunes' }, { id: 'red', l: '🔴 Rouges' }].map((f) => (
                <button key={f.id} onClick={() => setStatusFilter(f.id)}
                  className={`rounded-lg border px-3 py-1 text-sm ${statusFilter === f.id ? 'border-accent bg-accent/10 text-white' : 'border-border text-muted hover:bg-surface'}`}>
                  {f.l}
                </button>
              ))}
            </div>
            <Segmented
              value={view}
              onChange={setView}
              options={[{ value: 'heatmap', label: '▦ Heatmap' }, { value: 'table', label: '☰ Tableau' }]}
            />
          </div>

          {Object.entries(byMarket).map(([mkt, mrows]) => (
            <section key={mkt} className="rounded-xl border border-border bg-surface p-4">
              <h2 className="mb-3 text-sm font-semibold text-white">{MARKET_LABEL[mkt] ?? mkt}</h2>
              {view === 'heatmap' ? (
                <EdgeHeatmap rows={mrows} />
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead className="text-muted">
                      <tr>
                        <th className="py-1">Statut</th><th>Symbole</th><th>TF</th>
                        <th title="Sur-performance du walk-forward face au « buy & hold », frais inclus">Alpha</th>
                        <th>PF</th><th>Réussite</th><th>Trades</th><th>Stabilité</th>
                        <th className="border-l border-border pl-2" title="Espérance de LA STRATÉGIE par trade, en multiples du risque">Espérance</th>
                        <th title="Trades produits par jour sur ce symbole">Cadence</th>
                        <th title="Espérance × cadence : ce que ce symbole rapporte par mois, en R">R / mois</th>
                      </tr>
                    </thead>
                    <tbody>
                      {mrows.map((r, i) => (
                        <tr key={i} className={`border-t border-border/40 ${r.status === 'green' ? 'bg-buy/5' : ''}`}>
                          <td className="py-1.5"><EdgeStatusDot status={r.status} /></td>
                          <td className="font-mono text-white">{r.symbol}{!r.data_real && ' ⚠︎'}</td>
                          <td className="text-muted">{r.timeframe}</td>
                          <td className={r.alpha >= 0 ? 'text-buy' : 'text-sell'}>{r.alpha >= 0 ? '+' : ''}{r.alpha}%</td>
                          <td className={r.pf >= 1 ? 'text-buy' : 'text-sell'}>{r.pf}</td>
                          <td className="text-white">{r.win}%</td>
                          <td className="text-muted">{r.trades}</td>
                          <td className="text-muted">{r.green_streak ? `🟢×${r.green_streak}` : '—'}</td>
                          {/* Métriques de la STRATÉGIE (backtest) — vides tant qu'il n'a pas tourné. */}
                          <td className={`border-l border-border pl-2 ${
                            (r.playbook?.expectancy_r ?? 0) > 0 ? 'text-buy' : r.playbook ? 'text-sell' : 'text-muted'}`}>
                            {r.playbook
                              ? `${r.playbook.expectancy_r > 0 ? '+' : ''}${r.playbook.expectancy_r} R`
                              : '—'}
                            {r.playbook && <span className="text-muted"> ({r.playbook.trades})</span>}
                          </td>
                          <td className="text-muted">
                            {r.playbook?.days_between_trades
                              ? `1 / ${r.playbook.days_between_trades} j`
                              : '—'}
                          </td>
                          <td className={(r.playbook?.r_per_month ?? 0) > 0 ? 'text-buy' : 'text-muted'}>
                            {r.playbook?.r_per_month != null ? `${r.playbook.r_per_month} R` : '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          ))}
        </>
      )}

      <p className="text-[11px] text-muted">
        ⚠︎ = données synthétiques (repli) — combo à ignorer. Un 🟢 isolé peut être de la chance : la colonne
        Stabilité compte les sweeps verts consécutifs. Les performances passées ne préjugent pas des futures.
      </p>
    </div>
  );
}

/**
 * Matrice UNITÉ DE TEMPS (lignes) × symbole (colonnes).
 *
 * Le desk n'applique plus qu'UNE stratégie : mettre les stratégies en lignes ne donnerait qu'une
 * seule ligne. La question devenue utile est « sur quelle unité de temps cette méthode a-t-elle un
 * edge, et sur quel symbole ». Une dernière ligne résume le meilleur des unités de temps.
 */
function EdgeHeatmap({ rows }: { rows: EdgeRow[] }) {
  const timeframes = Array.from(new Set(rows.map((r) => r.timeframe))).sort();
  const symbols = Array.from(new Set(rows.map((r) => r.symbol)));
  const byTf = new Map<string, EdgeRow>();
  const best = new Map<string, EdgeRow>();
  for (const r of rows) {
    byTf.set(`${r.timeframe}|${r.symbol}`, r);
    const cur = best.get(r.symbol);
    if (!cur || r.alpha > cur.alpha) best.set(r.symbol, r);
  }

  if (symbols.length === 0) return <p className="text-xs text-muted">Aucune donnée.</p>;

  const lines: [string, string, (sym: string) => EdgeRow | undefined][] = [
    ...timeframes.map((tf) => [tf, `Unité de temps ${tf}`, (sym: string) => byTf.get(`${tf}|${sym}`)] as
      [string, string, (sym: string) => EdgeRow | undefined]),
    ['__best', 'Meilleure unité de temps', (sym: string) => best.get(sym)],
  ];

  return (
    <div className="overflow-x-auto">
      <div
        className="grid gap-1 text-2xs"
        style={{ gridTemplateColumns: `minmax(9rem,auto) repeat(${symbols.length}, minmax(3.5rem,1fr))` }}
      >
        {/* En-tête colonnes */}
        <div className="sticky left-0 z-10 bg-surface" />
        {symbols.map((s) => (
          <div key={s} className="truncate px-1 py-1 text-center font-mono text-muted" title={s}>
            {s.split('/')[0]}
          </div>
        ))}

        {lines.map(([key, label, pick]) => (
          <div key={key} className="contents">
            <div className={`sticky left-0 z-10 flex items-center truncate bg-surface pr-2 ${
              key === '__best' ? 'font-medium text-white' : 'text-muted'}`} title={label}>
              {label}
            </div>
            {symbols.map((sym) => {
              const r = pick(sym);
              const { className, style } = cellStyle(r);
              const pb = r?.playbook;
              return (
                <div
                  key={sym}
                  style={style}
                  title={r
                    ? `${sym} · ${r.timeframe}\nAlpha ${r.alpha >= 0 ? '+' : ''}${r.alpha}% · PF ${r.pf} · ${r.win}% · ${r.trades} trades`
                      + (pb ? `\nStratégie : ${pb.expectancy_r > 0 ? '+' : ''}${pb.expectancy_r} R sur ${pb.trades} trades`
                              + (pb.days_between_trades ? ` · 1 trade / ${pb.days_between_trades} j` : '')
                            : '\nStratégie : pas encore mesurée par le backtest')
                    : `${sym} — non testé`}
                  className={`flex h-9 items-center justify-center rounded font-mono font-medium ${className}`}
                >
                  {r ? `${r.alpha >= 0 ? '+' : ''}${r.alpha}` : '·'}
                </div>
              );
            })}
          </div>
        ))}
      </div>
      <p className="mt-2 text-2xs text-muted">
        Valeur = alpha % (vs. buy &amp; hold), c’est-à-dire ce que la méthode ajoute par rapport à
        détenir l’actif. Vert = exploitable · orange = à surveiller · rouge = sans edge. Survole une
        cellule pour lire aussi l’espérance et la cadence de la stratégie sur ce symbole.
      </p>
    </div>
  );
}
