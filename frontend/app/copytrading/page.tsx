'use client';

import { useEffect, useState } from 'react';
import { api, CopyFollow, PlanInfo, Trader } from '@/lib/api';
import { PageHeader, Button } from '@/components/ui';
import { UpgradeGate } from '@/components/domain';

export default function CopyTradingPage() {
  const [plan, setPlan] = useState<PlanInfo | null>(null);
  const [board, setBoard] = useState<Trader[]>([]);
  const [follows, setFollows] = useState<CopyFollow[]>([]);
  const [commission, setCommission] = useState<{ total: number; count: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const [b, f, c] = await Promise.all([api.leaderboard(), api.following(), api.commissions()]);
      setBoard(b);
      setFollows(f);
      setCommission({ total: c.total, count: c.count });
    } catch (e: any) {
      setError(e.message);
    }
  }
  useEffect(() => {
    api.myPlan().then(setPlan).catch(() => {});
    load();
  }, []);

  const locked = plan && !plan.features.copy_trading;

  async function publish() {
    const name = prompt('Nom public de trader ?', 'Pro Trader');
    if (!name) return;
    await api.publishProfile(name);
    load();
  }
  async function follow(t: Trader) {
    const alloc = parseFloat(prompt('Allocation % du capital par copie ?', '5') ?? '5');
    const maxPer = parseFloat(prompt('Plafond par trade ?', '1000') ?? '1000');
    const minConf = parseInt(prompt('Confiance minimale (%) ?', '60') ?? '60');
    try {
      await api.follow(t.tenant_id, alloc, maxPer, minConf);
      load();
    } catch (e: any) {
      setError(e.message);
    }
  }

  // Barrière de plan : on utilise le composant PARTAGÉ plutôt qu'un encadré maison, sinon chaque
  // page finit par avoir sa propre façon d'annoncer la même chose.
  if (locked)
    return (
      <div className="space-y-6 p-8">
        <PageHeader
          title="Copy-trading"
          subtitle="Suivre automatiquement les trades d'un trader public."
        />
        <UpgradeGate
          feature="Le copy-trading"
          plan="Elite"
          description="Répliquer automatiquement les positions d'un trader public, avec ton propre dimensionnement et tes propres garde-fous de risque."
        />
      </div>
    );

  return (
    <div className="p-8 space-y-6">
      <PageHeader
        title="Copy-trading"
        subtitle="Suis les meilleurs traders (copie en papier, garde-fous de risque)."
        actions={
          <Button variant="secondary" size="sm" onClick={publish}>Publier mon profil</Button>
        }
      />

      {error && <p className="text-sell">{error}</p>}

      {commission && (
        <div className="rounded-xl border border-border bg-surface p-4">
          <p className="text-xs text-muted">Commissions perçues (partage de revenus)</p>
          <p className="text-xl text-white">{commission.total} <span className="text-sm text-muted">sur {commission.count} copies</span></p>
        </div>
      )}

      <section className="space-y-3">
        <h2 className="text-lg font-semibold text-white">Classement des traders</h2>
        {board.length === 0 && <p className="text-muted">Aucun trader public pour le moment.</p>}
        {board.map((t, i) => {
          const followed = follows.find((f) => f.leader_tenant === t.tenant_id);
          return (
            <div key={t.tenant_id} className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border bg-surface p-4">
              <div className="flex items-center gap-3">
                <span className="text-muted">#{i + 1}</span>
                <span className="font-medium text-white">{t.display_name}</span>
                <span className="text-sm text-buy">{t.win_rate}% WR</span>
                <span className="text-sm text-gray-300">P&L {t.total_pnl}</span>
                <span className="text-xs text-muted">{t.closed_trades} trades</span>
              </div>
              {followed ? (
                <button onClick={() => api.unfollow(followed.id).then(load)} className="rounded border border-border px-3 py-1 text-xs text-muted hover:bg-[#1A1A1A]">Ne plus suivre</button>
              ) : (
                <button onClick={() => follow(t)} className="rounded bg-accent px-3 py-1 text-xs text-white">Copier</button>
              )}
            </div>
          );
        })}
      </section>

      {follows.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-lg font-semibold text-white">Je copie ({follows.length})</h2>
          {follows.map((f) => (
            <div key={f.id} className="rounded-lg border border-border bg-surface p-3 text-sm text-gray-300">
              {f.leader_tenant.slice(0, 8)}… · alloc {f.allocation_pct}% · max {f.max_per_trade} · conf≥{f.min_confidence}%
            </div>
          ))}
        </section>
      )}
    </div>
  );
}
