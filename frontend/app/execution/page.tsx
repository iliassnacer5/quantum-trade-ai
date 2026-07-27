'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, BrokerConn, Order, PlanInfo, PositionsSnapshot } from '@/lib/api';
import { MarketSelector, OutcomeBanner, DataSourceBadge } from '@/components/domain';
import { PageHeader } from '@/components/ui';
import { refreshLabel, useAutoRefresh } from '@/lib/useAutoRefresh';

type Ticket = { connId: string; side: 'buy' | 'sell' };

/** Le P&L latent est recalculé côté serveur toutes les 10 s — aucun clic nécessaire.
 *  C'est aussi ce qui fait apparaître les positions ouvertes AUTOMATIQUEMENT par le robot. */
const REFRESH_MS = 10_000;

export default function ExecutionPage() {
  const [plan, setPlan] = useState<PlanInfo | null>(null);
  const [kyc, setKyc] = useState<string>('none');
  const [conns, setConns] = useState<BrokerConn[]>([]);
  const [snap, setSnap] = useState<PositionsSnapshot | null>(null);
  // Récapitulatif de CE QUE J'AI CHOISI au lancement du dernier trade.
  const [lastOrder, setLastOrder] = useState<Order | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Ticket d'ordre (formulaire complet : entrée, SL, TP, taille).
  const [ticket, setTicket] = useState<Ticket | null>(null);
  const [symbol, setSymbol] = useState('BTC/USDT');
  const [cls, setCls] = useState('crypto');
  const [session, setSession] = useState('');
  const [symbols, setSymbols] = useState<{ symbol: string; asset_class: string }[]>([]);
  const [sessions, setSessions] = useState<{ id: string; label: string; window_utc: string; open: boolean }[]>([]);
  const [utcTime, setUtcTime] = useState('');
  const [qty, setQty] = useState('0.01');
  const [stopLoss, setStopLoss] = useState('');
  const [takeProfit, setTakeProfit] = useState('');
  const [price, setPrice] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [checking, setChecking] = useState<string | null>(null);
  const [dataSrc, setDataSrc] = useState<{ source: string; real: boolean; label: string } | null>(null);

  // Un seul appel : positions + P&L latent déjà calculé côté serveur.
  const refresh = useCallback(async () => {
    setSnap(await api.positions());
  }, []);

  const auto = useAutoRefresh(refresh, REFRESH_MS);

  const load = useCallback(async () => {
    try {
      const [k, c] = await Promise.all([api.kycStatus(), api.brokers()]);
      setKyc(k.status);
      setConns(c);
      await refresh();
    } catch (e: any) {
      setError(e.message);
    }
  }, [refresh]);

  useEffect(() => {
    api.myPlan().then(setPlan).catch(() => {});
    api.sessions().then((d) => { setSessions(d.sessions); setUtcTime(d.utc_time); }).catch(() => {});
    void load();
  }, [load]);

  // Charge les symboles selon le marché et la session sélectionnés.
  useEffect(() => {
    api.symbols(undefined, cls || undefined, session || undefined)
      .then((d) => {
        setSymbols(d.results);
        if (d.results.length && !d.results.some((r) => r.symbol === symbol)) setSymbol(d.results[0].symbol);
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cls, session]);

  // Récupère le prix courant (≈ prix d'entrée) + la qualité des données quand on ouvre un ticket.
  useEffect(() => {
    if (!ticket) return;
    setPrice(null);
    setDataSrc(null);
    // Prix de référence : uniquement s'il vient de données RÉELLES. Sans source réelle, on laisse
    // le champ vide plutôt que de proposer un prix d'entrée qui n'existe pas.
    api.ohlcv(symbol, '1h')
      .then((res) =>
        setPrice(res.real && res.candles.length ? res.candles[res.candles.length - 1].close : null),
      )
      .catch(() => setPrice(null));
    api.dataSource(symbol).then(setDataSrc).catch(() => setDataSrc(null));
  }, [ticket, symbol]);

  const liveAllowed = !!plan?.features.auto_execution;

  // Aperçu live du trade (R/R, risque, gain potentiel) à partir du prix courant.
  const preview = useMemo(() => {
    const e = price;
    const sl = parseFloat(stopLoss);
    const tp = parseFloat(takeProfit);
    const q = parseFloat(qty);
    if (!e || !ticket) return null;
    const side = ticket.side;
    const slOk = !stopLoss || (side === 'buy' ? sl < e : sl > e);
    const tpOk = !takeProfit || (side === 'buy' ? tp > e : tp < e);
    const riskUnit = stopLoss ? Math.abs(e - sl) : null;
    const rewardUnit = takeProfit ? Math.abs(tp - e) : null;
    return {
      entry: e,
      slOk, tpOk,
      risk: riskUnit && q ? riskUnit * q : null,
      reward: rewardUnit && q ? rewardUnit * q : null,
      rr: riskUnit && rewardUnit ? rewardUnit / riskUnit : null,
    };
  }, [price, stopLoss, takeProfit, qty, ticket]);

  async function submitKyc() {
    const name = prompt('Nom légal complet ?');
    if (!name) return;
    const country = prompt('Pays (ex: FR) ?') ?? '';
    const doc = prompt('N° pièce d’identité ?') ?? '';
    const r = await api.kycSubmit(name, country, doc);
    setKyc(r.status);
  }
  async function connect(mode: string) {
    try {
      if (mode === 'live') {
        const key = prompt('Clé API broker (Alpaca) ?') ?? '';
        const secret = prompt('Secret API ?') ?? '';
        await api.connectBroker('alpaca', 'live', key, secret);
      } else {
        await api.connectBroker('paper', 'paper');
      }
      load();
    } catch (e: any) {
      setError(e.message);
    }
  }

  function openTicket(conn: BrokerConn, side: 'buy' | 'sell') {
    setError(null);
    setTicket({ connId: conn.id, side });
    setStopLoss('');
    setTakeProfit('');
  }

  async function manualClose(id: string) {
    if (!confirm('Clôturer cette position au prix du marché actuel ?')) return;
    setChecking(id);
    setError(null);
    try {
      await api.closeOrder(id);
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setChecking(null);
    }
  }

  async function confirmOrder() {
    if (!ticket) return;
    const q = parseFloat(qty);
    if (!q || q <= 0) { setError('Quantité invalide.'); return; }
    if (preview && ((stopLoss && !preview.slOk) || (takeProfit && !preview.tpOk))) {
      setError(ticket.side === 'buy'
        ? 'Achat : le stop loss doit être sous l’entrée et le take profit au-dessus.'
        : 'Vente : le stop loss doit être au-dessus de l’entrée et le take profit en dessous.');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const placed = await api.placeOrder(
        ticket.connId, symbol, ticket.side, q,
        stopLoss ? parseFloat(stopLoss) : null,
        takeProfit ? parseFloat(takeProfit) : null,
      );
      setLastOrder(placed);   // récapitulatif de ce qui a été choisi
      setTicket(null);
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="p-8 space-y-6">
      <PageHeader
        title="Paper Trading"
        subtitle={
          <>
            Trading simulé <b className="text-buy">gratuit</b> pour s’entraîner sans risque. Exécution réelle = Elite + KYC.
          </>
        }
      />

      {error && <p className="text-sell">{error}</p>}

      <section className="rounded-xl border border-border bg-surface p-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-white">Statut KYC / AML</h2>
            <p className="text-sm text-muted">Requis pour l&apos;exécution réelle uniquement (le papier est libre).</p>
          </div>
          <div className="flex items-center gap-3">
            <span className={`rounded px-2 py-1 text-xs ${kyc === 'verified' ? 'bg-buy/20 text-buy' : 'bg-muted/20 text-muted'}`}>{kyc}</span>
            {kyc !== 'verified' && (
              <button onClick={submitKyc} className="rounded-lg bg-accent px-3 py-1 text-sm text-white">Soumettre KYC</button>
            )}
          </div>
        </div>
      </section>

      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-white">Connexions broker</h2>
          <div className="flex gap-2">
            <button onClick={() => connect('paper')} className="rounded-lg border border-buy/40 bg-buy/10 px-3 py-1 text-sm text-buy hover:bg-buy/20">+ Papier (gratuit)</button>
            <button onClick={() => connect('live')} disabled={!liveAllowed || kyc !== 'verified'}
              title={!liveAllowed ? 'Réservé au plan Elite' : kyc !== 'verified' ? 'KYC requis' : ''}
              className="rounded-lg border border-border px-3 py-1 text-sm hover:bg-surface disabled:opacity-40">+ Réel (Alpaca)</button>
          </div>
        </div>
        {conns.length === 0 && <p className="text-muted">Aucune connexion. Ajoute un broker papier pour commencer sans risque.</p>}
        {conns.map((c) => (
          <div key={c.id} className="rounded-xl border border-border bg-surface p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-3">
                <span className="font-medium text-white capitalize">{c.broker}</span>
                <span className={`rounded px-2 py-0.5 text-xs ${c.mode === 'live' ? 'bg-sell/20 text-sell' : 'bg-buy/20 text-buy'}`}>{c.mode}</span>
                {c.key_hint && <span className="font-mono text-xs text-muted">{c.key_hint}</span>}
              </div>
              <div className="flex gap-2">
                <button onClick={() => openTicket(c, 'buy')} className="rounded border border-buy/40 px-3 py-1 text-xs text-buy hover:bg-buy/10">Acheter</button>
                <button onClick={() => openTicket(c, 'sell')} className="rounded border border-sell/40 px-3 py-1 text-xs text-sell hover:bg-sell/10">Vendre</button>
                <button onClick={() => api.revokeBroker(c.id).then(load)} className="rounded border border-border px-3 py-1 text-xs text-muted hover:bg-[#1A1A1A]">Révoquer</button>
              </div>
            </div>

            {/* Ticket d'ordre complet pour cette connexion */}
            {ticket?.connId === c.id && (
              <>
                <div className="fixed inset-0 z-40 bg-black/60 backdrop-blur-sm" onClick={() => setTicket(null)} />
                <aside className="fixed right-0 top-0 z-50 flex h-full w-full max-w-md flex-col overflow-y-auto border-l border-border bg-elevated p-5 shadow-elevated">
                <div className="mb-3 flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-white">
                    Nouvel ordre — <span className={ticket.side === 'buy' ? 'text-buy' : 'text-sell'}>{ticket.side === 'buy' ? 'ACHAT' : 'VENTE'}</span>
                  </h3>
                  <button onClick={() => setTicket(null)} className="text-xs text-muted hover:text-white">✕ Fermer</button>
                </div>

                {/* Sélecteur complet : marché · session · paire/symbole */}
                <div className="mb-3 space-y-2">
                  <MarketSelector value={cls} onChange={setCls} />
                  {sessions.length > 0 && (
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-xs text-muted">Session <span className="text-[10px]">({utcTime})</span></span>
                      <button onClick={() => setSession('')}
                        className={`rounded border px-2 py-0.5 text-xs ${session === '' ? 'border-accent bg-accent/10 text-white' : 'border-border text-muted hover:bg-surface'}`}>
                        Toutes
                      </button>
                      {sessions.map((ss) => (
                        <button key={ss.id} onClick={() => setSession(ss.id)}
                          className={`rounded border px-2 py-0.5 text-xs ${session === ss.id ? 'border-accent bg-accent/10 text-white' : 'border-border text-muted hover:bg-surface'}`}>
                          <span className={`mr-1 inline-block h-1.5 w-1.5 rounded-full ${ss.open ? 'bg-buy' : 'bg-muted/40'}`} />
                          {ss.label}
                        </button>
                      ))}
                    </div>
                  )}
                </div>

                <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                  <label className="block">
                    <span className="mb-1 block text-xs text-muted">Paire / Symbole</span>
                    <select value={symbol} onChange={(e) => setSymbol(e.target.value)}
                      className="w-full rounded border border-border bg-surface px-2 py-1.5 font-mono text-sm text-white outline-none focus:border-accent">
                      {symbols.map((s) => <option key={s.symbol} value={s.symbol}>{s.symbol}</option>)}
                    </select>
                  </label>
                  <label className="block">
                    <span className="mb-1 block text-xs text-muted">Quantité</span>
                    <input value={qty} onChange={(e) => setQty(e.target.value)} inputMode="decimal"
                      className="w-full rounded border border-border bg-surface px-2 py-1.5 text-sm text-white" />
                  </label>
                  <label className="block">
                    <span className="mb-1 block text-xs text-muted">Stop loss</span>
                    <input value={stopLoss} onChange={(e) => setStopLoss(e.target.value)} inputMode="decimal" placeholder="optionnel"
                      className={`w-full rounded border bg-surface px-2 py-1.5 text-sm text-white ${stopLoss && preview && !preview.slOk ? 'border-sell' : 'border-border'}`} />
                  </label>
                  <label className="block">
                    <span className="mb-1 block text-xs text-muted">Take profit</span>
                    <input value={takeProfit} onChange={(e) => setTakeProfit(e.target.value)} inputMode="decimal" placeholder="optionnel"
                      className={`w-full rounded border bg-surface px-2 py-1.5 text-sm text-white ${takeProfit && preview && !preview.tpOk ? 'border-sell' : 'border-border'}`} />
                  </label>
                </div>

                {/* Badge qualité des données */}
                {dataSrc && (
                  <div className="mt-3">
                    <DataSourceBadge real={dataSrc.real} label={dataSrc.real ? `${dataSrc.label} — données réelles` : `${dataSrc.label} — pas de source réelle, trade bloqué`} />
                  </div>
                )}

                {/* Aperçu du trade */}
                <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs md:grid-cols-4">
                  <span className="text-muted">Prix d&apos;entrée ≈ <span className="text-white">{price ?? '…'}</span></span>
                  <span className="text-muted">R/R : <span className="text-white">{preview?.rr ? `1 : ${preview.rr.toFixed(2)}` : '—'}</span></span>
                  <span className="text-muted">Risque : <span className="text-sell">{preview?.risk != null ? preview.risk.toFixed(2) : '—'}</span></span>
                  <span className="text-muted">Gain potentiel : <span className="text-buy">{preview?.reward != null ? preview.reward.toFixed(2) : '—'}</span></span>
                </div>

                <div className="mt-3 flex items-center gap-2">
                  <button onClick={confirmOrder} disabled={submitting || dataSrc?.real === false}
                    title={dataSrc?.real === false ? 'Données synthétiques : trade désactivé' : ''}
                    className={`rounded-lg px-4 py-1.5 text-sm font-medium text-white disabled:opacity-50 ${ticket.side === 'buy' ? 'bg-buy' : 'bg-sell'}`}>
                    {submitting ? '…' : ticket.side === 'buy' ? 'Confirmer l’achat' : 'Confirmer la vente'}
                  </button>
                  <span className="text-[11px] text-muted">Ordre simulé — rempli au prix marché. Le stop/TP sont enregistrés avec le trade.</span>
                </div>
                </aside>
              </>
            )}
          </div>
        ))}
      </section>

      {/* Récapitulatif du trade que je viens de lancer : tout ce que j'ai choisi. */}
      {lastOrder && (
        <section className="rounded-xl border border-buy/40 bg-buy/5 p-4">
          <div className="flex items-start justify-between">
            <h2 className="text-sm font-semibold text-white">
              ✅ Trade lancé — voici exactement ce qui a été enregistré
            </h2>
            <button onClick={() => setLastOrder(null)} className="text-xs text-muted hover:text-white">✕</button>
          </div>
          <div className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-3 lg:grid-cols-4">
            <Recap label="Symbole" value={lastOrder.symbol} mono />
            <Recap label="Sens" value={lastOrder.side === 'buy' ? 'ACHAT' : 'VENTE'}
              tone={lastOrder.side === 'buy' ? 'buy' : 'sell'} />
            <Recap label="Quantité" value={lastOrder.qty} mono />
            <Recap label="Prix d'entrée" value={lastOrder.entry ?? lastOrder.filled_price ?? '—'} mono />
            <Recap label="Stop loss" value={lastOrder.stop_loss ?? 'aucun'} tone="sell" mono />
            <Recap label="Take profit" value={lastOrder.take_profit ?? 'aucun'} tone="buy" mono />
            <Recap label="Risque / rendement" value={lastOrder.risk_reward != null ? `1 : ${lastOrder.risk_reward}` : '—'} />
            <Recap label="Montant risqué" value={lastOrder.risk_amount ?? '—'} tone="sell" />
            <Recap label="Gain visé" value={lastOrder.potential_profit ?? '—'} tone="buy" />
            <Recap label="Mode" value={lastOrder.mode} />
            <Recap label="Statut" value={lastOrder.status} />
          </div>
          <p className="mt-2 text-[11px] text-muted">
            Le stop et l&apos;objectif sont enregistrés avec la position : elle sera clôturée
            automatiquement dès que l&apos;un des deux est touché. Aucune action de ta part.
          </p>
        </section>
      )}

      <section className="space-y-2">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-lg font-semibold text-white">
            Positions ({snap?.open_count ?? 0} ouverte(s) · {snap?.closed_count ?? 0} clôturée(s))
          </h2>
          <span className="text-[11px] text-muted">
            <span className={auto.refreshing ? 'text-accent' : ''}>🔄 Suivi automatique</span> · {refreshLabel(auto)}
          </span>
        </div>

        {/* Bandeau P&L global, mis à jour tout seul. */}
        {snap && (snap.open_count > 0 || snap.closed_count > 0) && (
          <div className="flex flex-wrap gap-x-6 gap-y-1 rounded-lg border border-border bg-surface px-4 py-2 text-xs">
            <span className="text-muted">
              P&L latent (positions ouvertes) :{' '}
              <span className={snap.unrealized_pnl >= 0 ? 'text-buy' : 'text-sell'}>
                {snap.unrealized_pnl >= 0 ? '+' : ''}{snap.unrealized_pnl}
              </span>
            </span>
            <span className="text-muted">
              P&L réalisé :{' '}
              <span className={snap.realized_pnl >= 0 ? 'text-buy' : 'text-sell'}>
                {snap.realized_pnl >= 0 ? '+' : ''}{snap.realized_pnl}
              </span>
            </span>
            <span className="text-muted">Gagnants : <span className="text-buy">{snap.wins}</span></span>
            <span className="text-muted">Perdants : <span className="text-sell">{snap.losses}</span></span>
            {snap.win_rate != null && (
              <span className="text-muted">Taux de réussite : <span className="text-white">{snap.win_rate}%</span></span>
            )}
          </div>
        )}

        {snap?.positions.length === 0 && (
          <p className="text-muted">Aucune position. Lance un trade ci-dessus ou depuis les trades du jour.</p>
        )}

        {snap?.positions.map((o) => {
          const closed = o.closed;
          const pnl = closed ? o.realized_pnl : o.unrealized_pnl;
          const progress = Math.max(0, Math.min(100, o.progress_pct ?? 0));
          return (
            <div key={o.id} className="rounded-lg border border-border bg-surface p-3 text-sm">
              <div className="flex flex-wrap items-center gap-3">
                <span className="font-mono text-white">{o.symbol}</span>
                <span className={o.side === 'buy' ? 'text-buy' : 'text-sell'}>
                  {o.side === 'buy' ? 'ACHAT' : 'VENTE'}
                </span>
                <span className="text-muted">{o.qty} @ {o.entry ?? o.filled_price ?? '—'}</span>
                <span className="rounded bg-muted/20 px-2 py-0.5 text-xs text-muted">{o.mode}</span>
                {closed ? <OutcomeBanner outcome={o.outcome!} className="px-2 py-0.5" />
                  : <span className="rounded bg-accent/15 px-2 py-0.5 text-xs text-accent">en cours</span>}
                {o.copied_from && <span className="rounded bg-accent/20 px-2 py-0.5 text-xs text-accent">copié</span>}
                {pnl != null && (
                  <span className={`font-mono font-semibold ${pnl >= 0 ? 'text-buy' : 'text-sell'}`}>
                    {pnl >= 0 ? '+' : ''}{pnl}
                    {o.pnl_pct != null && !closed && <span className="ml-1 text-[11px]">({o.pnl_pct > 0 ? '+' : ''}{o.pnl_pct}%)</span>}
                  </span>
                )}
                {!closed && (
                  <button onClick={() => manualClose(o.id)} disabled={checking === o.id}
                    className="ml-auto rounded border border-sell/50 px-2 py-0.5 text-xs text-sell hover:bg-sell/10 disabled:opacity-50">
                    {checking === o.id ? '…' : 'Clôturer maintenant'}
                  </button>
                )}
              </div>

              {/* Ce qui a été choisi au lancement du trade. */}
              <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-muted">
                {o.stop_loss != null && <span>Stop : <span className="text-sell">{o.stop_loss}</span></span>}
                {o.take_profit != null && <span>Objectif : <span className="text-buy">{o.take_profit}</span></span>}
                {o.risk_reward != null && <span>R/R : <span className="text-white">1 : {o.risk_reward}</span></span>}
                {o.risk_amount != null && <span>Risqué : <span className="text-sell">{o.risk_amount}</span></span>}
                {o.potential_profit != null && <span>Gain visé : <span className="text-buy">{o.potential_profit}</span></span>}
                {!closed && o.current_price != null && <span>Prix actuel : <span className="text-white">{o.current_price}</span></span>}
                {!closed && o.r_multiple != null && <span>En multiples de risque : <span className="text-white">{o.r_multiple} R</span></span>}
              </div>

              {/* Progression vers l'objectif, sans aucun clic. */}
              {!closed && o.progress_pct != null && (
                <div className="mt-2">
                  <div className="mb-0.5 flex justify-between text-[10px] text-muted">
                    <span>Entrée</span>
                    <span>{o.progress_pct}% du chemin vers l&apos;objectif</span>
                    <span>Objectif</span>
                  </div>
                  <div className="h-1.5 w-full rounded bg-border">
                    <div className={`h-1.5 rounded ${(o.progress_pct ?? 0) >= 0 ? 'bg-buy' : 'bg-sell'}`}
                      style={{ width: `${progress}%` }} />
                  </div>
                </div>
              )}

              {closed && o.exit_price != null && (
                <p className="mt-1.5 text-xs text-muted">
                  Sortie @ <span className="text-white">{o.exit_price}</span>
                  {o.closed_at && <> · le {new Date(o.closed_at).toLocaleString('fr-FR')}</>}
                </p>
              )}
            </div>
          );
        })}
      </section>
    </div>
  );
}

function Recap({ label, value, tone = '', mono = false }:
  { label: string; value: string | number; tone?: string; mono?: boolean }) {
  const color = tone === 'buy' ? 'text-buy' : tone === 'sell' ? 'text-sell' : 'text-white';
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
      <div className={`${mono ? 'font-mono' : ''} ${color}`}>{value}</div>
    </div>
  );
}
