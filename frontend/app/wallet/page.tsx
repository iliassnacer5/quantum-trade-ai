'use client';

import { useEffect, useState } from 'react';
import { Area, AreaChart, ResponsiveContainer, Tooltip, YAxis } from 'recharts';
import { api, Wallet } from '@/lib/api';
import { PageHeader, Card, Stat as AnimatedStat } from '@/components/ui';

const usd = (n: number) => `${n.toLocaleString('fr-FR', { maximumFractionDigits: 0 })} $`;

export default function WalletPage() {
  const [w, setW] = useState<Wallet | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [startBal, setStartBal] = useState('10000');

  async function load() {
    setLoading(true);
    try {
      setW(await api.wallet());
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    load();
    const id = setInterval(load, 10000); // suit les clôtures auto et les entrées automatiques
    return () => clearInterval(id);
  }, []);

  async function reset() {
    if (!confirm('Réinitialiser le portefeuille et effacer l’historique des trades papier ?')) return;
    setW(await api.resetWallet(parseFloat(startBal) || 10000, true));
  }

  if (loading && !w) return <div className="p-8 text-white">Chargement…</div>;
  if (error) return <div className="p-8 text-sell">{error}</div>;
  if (!w) return null;

  const st = w.stats;
  const pnlColor = (v: number) => (v >= 0 ? 'text-buy' : 'text-sell');
  const up = w.return_pct >= 0;
  const chartColor = up ? '#1D9E75' : '#E24B4A';
  // Données de la courbe d'équité (aire dégradée).
  const chartData = [{ equity: w.starting_balance, label: 'départ' }, ...w.equity_curve.map((p, i) => ({ equity: p.equity, label: p.symbol ?? `#${i + 1}` }))];

  return (
    <div className="p-8 space-y-6">
      <PageHeader
        title="Portefeuille virtuel"
        subtitle="Compte simulé : ton solde évolue exactement comme un vrai compte, au fil de tes trades papier."
        actions={
          <a href="/execution" className="rounded-lg border border-border px-3 py-1.5 text-sm text-muted hover:bg-surface hover:text-white">
            Paper Trading →
          </a>
        }
      />

      {/* Solde & équité */}
      <section className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Card variant="stat"><AnimatedStat label="Solde" value={w.balance} format={usd} hint={`départ ${usd(w.starting_balance)}`} /></Card>
        <Card variant="stat"><AnimatedStat label="Équité (positions incluses)" value={w.equity} format={usd} /></Card>
        <Card variant="stat"><AnimatedStat label="P&L réalisé" value={w.realized_pnl} format={(n) => `${n >= 0 ? '+' : ''}${usd(n)}`} tone={w.realized_pnl >= 0 ? 'buy' : 'sell'} /></Card>
        <Card variant="stat"><AnimatedStat label="Performance" value={w.return_pct} format={(n) => `${n >= 0 ? '+' : ''}${n.toFixed(1)}%`} tone={up ? 'buy' : 'sell'} hint={`latent ${w.unrealized_pnl >= 0 ? '+' : ''}${w.unrealized_pnl} $`} /></Card>
      </section>

      {/* Courbe d'équité */}
      <Card>
        <h2 className="mb-2 text-sm font-semibold text-white">Courbe d’équité ({st.trades} trade(s) clôturé(s))</h2>
        {w.equity_curve.length > 0 ? (
          <div className="h-56 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={chartData} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                <defs>
                  <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={chartColor} stopOpacity={0.35} />
                    <stop offset="100%" stopColor={chartColor} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <YAxis domain={['dataMin', 'dataMax']} hide />
                <Tooltip
                  contentStyle={{ background: '#1B222B', border: '1px solid #232A33', borderRadius: 8, fontSize: 12 }}
                  labelStyle={{ color: '#8A94A6' }}
                  formatter={(v) => [usd(Number(v)), 'Équité']}
                />
                <Area type="monotone" dataKey="equity" stroke={chartColor} strokeWidth={2} fill="url(#equityFill)" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="text-sm text-muted">Aucun trade clôturé. Passe des ordres en <a href="/execution" className="text-accent underline">Paper Trading</a> et laisse-les se résoudre.</p>
        )}
      </Card>

      {/* Statistiques de fiabilité */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Taux de réussite" value={`${st.win_rate}%`} color={st.win_rate >= 50 ? 'text-buy' : 'text-sell'} />
        <Stat label="Profit factor" value={st.profit_factor} color={st.profit_factor >= 1 ? 'text-buy' : 'text-sell'} />
        <Stat label="Gagnants / Perdants" value={`${st.wins} / ${st.losses}`} />
        <Stat label="Positions ouvertes" value={st.open_positions} />
        <Stat label="Meilleur trade" value={`${st.best_trade >= 0 ? '+' : ''}${st.best_trade} $`} color="text-buy" />
        <Stat label="Pire trade" value={`${st.worst_trade} $`} color="text-sell" />
      </section>

      {/* Positions ouvertes */}
      {w.positions.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-lg font-semibold text-white">Positions ouvertes ({w.positions.length})</h2>
          {w.positions.map((p) => (
            <div key={p.id} className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-surface p-3 text-sm">
              <span className="font-mono text-white">{p.symbol}</span>
              <span className={p.side === 'buy' ? 'text-buy' : 'text-sell'}>{p.side}</span>
              <span className="text-muted">{p.qty} @ {p.entry}</span>
              <span className="text-muted">prix {p.current_price ?? '—'}</span>
              <span className={`ml-auto ${pnlColor(p.unrealized_pnl)}`}>P&L latent {p.unrealized_pnl >= 0 ? '+' : ''}{p.unrealized_pnl} $</span>
            </div>
          ))}
        </section>
      )}

      {/* Réinitialisation */}
      <section className="rounded-xl border border-border bg-surface p-4">
        <h2 className="mb-2 text-sm font-semibold text-white">Recommencer à zéro</h2>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-muted">Solde de départ :</span>
          <input value={startBal} onChange={(e) => setStartBal(e.target.value)} inputMode="decimal"
            className="w-28 rounded border border-border bg-background px-2 py-1 font-mono text-white" />
          <span className="text-muted">$</span>
          <button onClick={reset} className="rounded-lg border border-sell/50 px-3 py-1 text-sm text-sell hover:bg-sell/10">
            Réinitialiser (efface l’historique)
          </button>
        </div>
      </section>

      <p className="text-xs text-muted">
        C’est le juge de paix : après plusieurs dizaines de trades, ce solde te dit objectivement si ta façon de trader est fiable.
        Argent fictif — aide à la décision, pas un conseil en investissement.
      </p>
    </div>
  );
}

function Stat({ label, value, color = 'text-white' }: { label: string; value: string | number; color?: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface p-3">
      <p className="text-xs text-muted">{label}</p>
      <p className={`mt-0.5 text-lg ${color}`}>{value}</p>
    </div>
  );
}
