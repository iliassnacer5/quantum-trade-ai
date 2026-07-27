'use client';

/**
 * « Ma journée » — le briefing du matin en 30 secondes.
 * Régime du jour · ce qui s'est passé sur mes positions · les meilleures opportunités ·
 * mon track record réel · ce que les filtres m'ont évité. Le rituel quotidien du trader.
 */
import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { api, MarketRegime, SignalsTrackRecord, Wallet } from '@/lib/api';
import { PageHeader, Card, Stat, Skeleton } from '@/components/ui';
import { DirectionBadge, UpgradeGate, PairVerdictsPanel } from '@/components/domain';
import { staggerContainer, staggerItem } from '@/lib/motion';

const usd = (n: number) => `${n.toLocaleString('fr-FR', { maximumFractionDigits: 0 })} $`;

export default function TodayPage() {
  const [regime, setRegime] = useState<MarketRegime | null>(null);
  const [wallet, setWallet] = useState<Wallet | null>(null);
  const [track, setTrack] = useState<SignalsTrackRecord | null>(null);
  const [picks, setPicks] = useState<any[] | null>(null);
  const [picksLocked, setPicksLocked] = useState(false);

  useEffect(() => {
    api.marketRegime().then(setRegime).catch(() => {});
    api.wallet().then(setWallet).catch(() => {});
    api.signalsTrackRecord().then(setTrack).catch(() => {});
    api.dailyPicks(false, '4h')
      .then((d) => setPicks(d.picks.slice(0, 3)))
      .catch((e) => { if (String(e.message).includes('402')) setPicksLocked(true); });
  }, []);

  const today = new Date().toLocaleDateString('fr-FR', { weekday: 'long', day: 'numeric', month: 'long' });
  const regimeTone = regime?.regime === 'on' ? 'border-buy/40' : regime?.regime === 'off' ? 'border-sell/40' : '';
  const regimeDot = regime?.regime === 'on' ? 'bg-buy' : regime?.regime === 'off' ? 'bg-sell' : 'bg-muted';
  const closedRecently = (wallet?.equity_curve ?? []).slice(-3).reverse();
  const obs = track?.observed;
  const av = track?.avoided;

  return (
    <div className="mx-auto max-w-4xl space-y-5 p-6">
      <PageHeader title="☀️ Ma journée" subtitle={<span className="capitalize">{today} · ton briefing en 30 secondes</span>} />

      {/* 1. Le régime du jour */}
      <Card variant="glass" className={`overflow-hidden ${regimeTone}`}>
        <div className="mb-1 flex items-center gap-2">
          <span className={`h-2 w-2 animate-pulse-dot rounded-full ${regimeDot}`} />
          <h2 className="text-sm font-semibold text-white">🌍 Le marché aujourd&apos;hui</h2>
        </div>
        {regime ? (
          <>
            <p className="text-sm text-gray-200">{regime.regime_label}</p>
            <div className="mt-2 flex flex-wrap gap-2 text-xs text-muted">
              <span>Sessions ouvertes : <b className="text-white">{regime.open_sessions.join(', ') || 'aucune'}</b></span>
              {regime.inflation != null && <span>· Inflation {regime.inflation}%</span>}
              {regime.rate_trend && <span>· Taux : {regime.rate_trend}</span>}
              <span className="text-2xs">({regime.utc_time})</span>
            </div>
          </>
        ) : <Skeleton lines={2} />}
      </Card>

      {/* 2. Mes positions / ce qui s'est passé */}
      <Card>
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white">💼 Mon portefeuille</h2>
          <a href="/wallet" className="text-xs text-accent hover:underline">détail →</a>
        </div>
        {wallet ? (
          <>
            <div className="mt-3 grid grid-cols-2 gap-4 md:grid-cols-4">
              <Stat label="Équité" value={wallet.equity} format={usd} />
              <Stat label="Performance" value={wallet.return_pct} format={(n) => `${n >= 0 ? '+' : ''}${n.toFixed(1)}%`} tone={wallet.return_pct >= 0 ? 'buy' : 'sell'} />
              <Stat label="Positions ouvertes" value={wallet.stats.open_positions} />
              <Stat label="Réussite" value={wallet.stats.win_rate} format={(n) => `${n.toFixed(0)}%`} tone={wallet.stats.win_rate >= 50 ? 'buy' : 'default'} />
            </div>
            {closedRecently.length > 0 && (
              <div className="mt-4 space-y-1 border-t border-border/50 pt-3">
                <p className="text-xs text-muted">Dernières clôtures :</p>
                {closedRecently.map((c, i) => (
                  <p key={i} className="text-xs">
                    <span className={c.outcome === 'won' ? 'text-buy' : 'text-sell'}>{c.outcome === 'won' ? '✅' : '🔴'}</span>{' '}
                    <span className="font-mono text-white">{c.symbol}</span>{' '}
                    <span className={c.pnl! >= 0 ? 'text-buy' : 'text-sell'}>{c.pnl! >= 0 ? '+' : ''}{c.pnl} $</span>
                  </p>
                ))}
              </div>
            )}
          </>
        ) : <Skeleton lines={2} className="mt-3" />}
      </Card>

      {/* 3. Les opportunités du jour (4h = timeframe validé) */}
      <Card>
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white">🎯 Les opportunités du jour (4h)</h2>
          <a href="/daily" className="text-xs text-accent hover:underline">tout voir →</a>
        </div>
        {picksLocked && <UpgradeGate feature="Opportunités du jour" plan="Pro+" className="mt-3" />}
        {picks && picks.length === 0 && <p className="mt-2 text-sm text-muted">Rien de convaincant aujourd&apos;hui — s&apos;abstenir est une décision valable.</p>}
        {picks && picks.length > 0 && (
          <motion.div variants={staggerContainer} initial="initial" animate="animate" className="mt-3 grid gap-2 md:grid-cols-3">
            {picks.map((p) => (
              <motion.div key={p.symbol} variants={staggerItem}>
                <Card
                  padded={false}
                  hover
                  className={`p-3 ${p.tier === 'confirmed' ? 'border-buy/40' : 'border-warn/30'}`}
                >
                  <div className="flex items-center justify-between text-sm">
                    <span className="font-mono font-semibold text-white">{p.symbol}</span>
                    <DirectionBadge direction={p.direction} size="sm" />
                  </div>
                  <p className="mt-1 text-2xs text-muted">{p.tier === 'confirmed' ? '★ confirmé par backtest' : '👀 à surveiller'}</p>
                </Card>
              </motion.div>
            ))}
          </motion.div>
        )}
      </Card>

      {/* 3 bis. Verdicts par paire de la stratégie (🟢 auto-tradées / 🟡 analysées / 🔴 exclues) */}
      <PairVerdictsPanel compact />

      {/* 4. Track record réel + ce que les filtres t'ont évité */}
      <section className="grid gap-4 md:grid-cols-2">
        <Card>
          <h2 className="mb-2 text-sm font-semibold text-white">📈 Mes prédictions — issues réelles</h2>
          {obs && obs.closed > 0 ? (
            <>
              <p className="font-mono text-2xl font-bold text-white">{obs.win_rate}% <span className="text-sm font-normal text-muted">de réussite</span></p>
              <p className="mt-1 text-xs text-muted">{obs.wins} gagnées · {obs.losses} perdues · {obs.open} en cours — issues vérifiées sur le prix réel, y compris les perdantes.</p>
            </>
          ) : (
            <p className="text-sm text-muted">Pas encore de trade résolu. Génère des signaux : leurs issues s&apos;afficheront ici, gagnées comme perdues.</p>
          )}
        </Card>
        <Card variant="glass" className="border-accent/30">
          <h2 className="mb-2 text-sm font-semibold text-white">🛡️ Ce que les filtres t&apos;ont évité</h2>
          {av && av.blocked > 0 ? (
            <>
              <p className="font-mono text-2xl font-bold text-white">{av.would_have_lost} <span className="text-sm font-normal text-muted">trade(s) perdant(s) évité(s)</span></p>
              <p className="mt-1 text-xs text-muted">
                Sur {av.blocked} signaux bloqués (multi-timeframe, qualité, news) : {av.would_have_lost} auraient perdu,{' '}
                {av.would_have_won} auraient gagné{av.undecided ? `, ${av.undecided} indécis` : ''} — on te montre les deux, honnêtement.
              </p>
            </>
          ) : (
            <p className="text-sm text-muted">Aucun signal bloqué récemment. Quand un gate refusera un trade, tu verras ici s&apos;il t&apos;a protégé.</p>
          )}
        </Card>
      </section>

      <p className="text-2xs text-muted">
        Aide à la décision, pas un conseil en investissement. Les performances passées ne préjugent pas des résultats futurs.
      </p>
    </div>
  );
}
