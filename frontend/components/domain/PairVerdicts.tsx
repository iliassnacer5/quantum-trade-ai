'use client';

/**
 * 🎯 VERDICTS PAR PAIRE — la note 🟢/🟡/🔴 que le backtest hebdomadaire donne à chaque paire
 * de la stratégie du desk, avec les chiffres qui la motivent (plan, tâches 2.1 / 2.7).
 *
 * 🟢 = espérance ≥ +0,4 R sur n ≥ 20 trades, confirmée sur DEUX passages consécutifs — seules ces
 * paires sont auto-tradées. 🟡 = analysée mais non auto-tradée. 🔴 = exclue (la stratégie perd ici).
 */
import { useEffect, useState } from 'react';
import { api, PairVerdicts as PairVerdictsData, PairVerdict } from '@/lib/api';
import { Card, Skeleton } from '@/components/ui';

const ORDER: Record<string, number> = { green: 0, yellow: 1, red: 2 };

function sorted(pairs: Record<string, PairVerdict>): PairVerdict[] {
  return Object.values(pairs).sort(
    (a, b) => (ORDER[a.status] ?? 3) - (ORDER[b.status] ?? 3)
      || (b.expectancy_r ?? -9) - (a.expectancy_r ?? -9),
  );
}

export function PairVerdictsPanel({ compact = false }: { compact?: boolean }) {
  const [data, setData] = useState<PairVerdictsData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.pairVerdicts().then(setData).catch((e) => setError(e.message));
  }, []);

  if (error) return null; // section optionnelle : une erreur ne casse pas la page
  if (!data) return <Skeleton lines={2} />;
  if (!data.available) {
    return (
      <Card>
        <h2 className="mb-1 text-sm font-semibold text-white">🎯 Verdicts par paire (stratégie du desk)</h2>
        <p className="text-sm text-muted">{data.note}</p>
      </Card>
    );
  }

  const rows = sorted(data.pairs);
  const greens = rows.filter((r) => r.status === 'green').length;
  const reds = rows.filter((r) => r.status === 'red').length;

  if (compact) {
    return (
      <Card>
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white">🎯 Paires de la stratégie — verdict hebdo</h2>
          <a href="/edge" className="text-xs text-accent hover:underline">détail →</a>
        </div>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {rows.map((r) => (
            <span
              key={r.symbol}
              title={`${r.reason}${r.expectancy_r != null ? `\nEspérance ${r.expectancy_r > 0 ? '+' : ''}${r.expectancy_r} R · ${r.trades} trades · ${r.win_rate}% de réussite` : ''}`}
              className={`rounded-md border px-2 py-0.5 font-mono text-2xs ${
                r.status === 'green' ? 'border-buy/40 bg-buy/10 text-buy'
                : r.status === 'red' ? 'border-sell/40 bg-sell/10 text-sell'
                : 'border-warn/30 bg-warn/5 text-warn'}`}
            >
              {r.emoji} {r.symbol}
            </span>
          ))}
        </div>
        <p className="mt-2 text-2xs text-muted">
          {greens > 0
            ? `${greens} paire(s) 🟢 auto-tradée(s) · le reste est analysé mais non auto-tradé.`
            : 'Aucune paire 🟢 pour l’instant : le vert exige deux backtests hebdomadaires consécutifs au-dessus du seuil — l’auto-entrée attend la confirmation.'}
          {reds > 0 && ` ${reds} paire(s) 🔴 exclue(s).`}
        </p>
      </Card>
    );
  }

  return (
    <section className="rounded-xl border border-border bg-surface p-4">
      <div className="mb-1 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-white">🎯 Verdicts par paire — stratégie du desk (backtest hebdo)</h2>
        {data.date && <span className="text-xs text-muted">passage du {data.date}</span>}
      </div>
      <p className="mb-3 text-xs text-muted">
        🟢 = espérance ≥ +0,4 R sur ≥ 20 trades, confirmée sur 2 passages consécutifs (seules paires
        auto-tradées) · 🟡 = analysée, non auto-tradée · 🔴 = exclue. Mesuré, pas supposé.
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-muted">
            <tr>
              <th className="py-1">Verdict</th><th>Paire</th><th>Espérance</th><th>Trades</th>
              <th>Réussite</th><th>PF</th><th>Série 🟢</th><th>Pourquoi</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.symbol} className={`border-t border-border/40 ${r.status === 'green' ? 'bg-buy/5' : ''}`}>
                <td className="py-1.5">{r.emoji}</td>
                <td className="font-mono text-white">{r.symbol}</td>
                <td className={r.expectancy_r != null && r.expectancy_r > 0 ? 'text-buy' : 'text-sell'}>
                  {r.expectancy_r != null ? `${r.expectancy_r > 0 ? '+' : ''}${r.expectancy_r} R` : '—'}
                </td>
                <td className="text-white">{r.trades ?? '—'}</td>
                <td className="text-white">{r.win_rate != null ? `${r.win_rate}%` : '—'}</td>
                <td className="text-muted">{r.profit_factor ?? '∞'}</td>
                <td className="text-muted">{r.green_streak > 0 ? `×${r.green_streak}` : '—'}</td>
                <td className="max-w-[26rem] text-muted">{r.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(data.refusals?.length ?? 0) > 0 && (
        <div className="mt-3 border-t border-border/50 pt-2">
          <p className="mb-1 text-xs font-semibold text-white">🛡️ Derniers trades refusés par les gates</p>
          {data.refusals!.slice(0, 5).map((r, i) => (
            <p key={i} className="text-2xs text-muted">
              <span className="font-mono text-white">{r.symbol}</span> {r.direction} — {r.reason}
            </p>
          ))}
        </div>
      )}
    </section>
  );
}
