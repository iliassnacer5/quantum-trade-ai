'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, BrokerConn, Order, PaperPosition, PlanInfo, PositionsSnapshot } from '@/lib/api';
import { MarketSelector, OutcomeBanner, DataSourceBadge } from '@/components/domain';
import { PageHeader } from '@/components/ui';
import { refreshLabel, useAutoRefresh } from '@/lib/useAutoRefresh';

type Ticket = { connId: string; side: 'buy' | 'sell' };

/** Le P&L latent est recalculé côté serveur — aucun clic nécessaire. C'est aussi ce qui fait
 *  apparaître les positions ouvertes AUTOMATIQUEMENT par le robot.
 *
 *  15 s et non 10 s (30/07/2026) : le prix de référence côté serveur est lui-même mémorisé ~20 s
 *  (`market_cache_ttl`), donc interroger plus souvent que ça ne rapportait aucune fraîcheur
 *  supplémentaire — seulement des requêtes. Le hook suspend déjà la boucle quand l'onglet est
 *  masqué (cf. `useAutoRefresh`). */
const REFRESH_MS = 15_000;

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
  // Confirmation de clôture EN PAGE (pas de window.confirm) : la boîte de dialogue native du
  // navigateur peut être bloquée ou auto-rejetée silencieusement dans certains contextes embarqués
  // (webview, aperçu intégré…) — le clic ne produisait alors AUCUNE requête, sans aucun message
  // d'erreur, ce qui ressemblait à un bouton cassé. Un état de confirmation en JSX fonctionne
  // partout où React fonctionne.
  const [confirmCloseId, setConfirmCloseId] = useState<string | null>(null);
  // Échec de clôture, rattaché à SA position. L'erreur partait dans le bandeau global, en haut de
  // page : l'utilisateur qui clique « Clôturer » est scrollé sur sa position et ne la voyait jamais
  // — le bouton semblait donc ne rien faire.
  const [closeError, setCloseError] = useState<{ id: string; message: string } | null>(null);
  // Clôture groupée : confirmation, état d'envoi, et compte rendu.
  const [confirmCloseAll, setConfirmCloseAll] = useState(false);
  const [closingAll, setClosingAll] = useState(false);
  const [closeAllMsg, setCloseAllMsg] = useState<string | null>(null);
  const [dataSrc, setDataSrc] = useState<{ source: string; real: boolean; label: string } | null>(null);

  // Modification manuelle du stop / de l'objectif d'une position ouverte : une seule ligne
  // éditable à la fois, avec son propre message d'erreur (le prix a pu franchir le niveau demandé
  // entre la saisie et l'envoi, par ex.).
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editSL, setEditSL] = useState('');
  const [editTP, setEditTP] = useState('');
  const [editSaving, setEditSaving] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);

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
    setConfirmCloseId(null);
    setChecking(id);
    setError(null);
    setCloseError(null);
    try {
      await api.closeOrder(id);
      await refresh();
    } catch (e: any) {
      // Rattachée à la position : c'est là que l'utilisateur regarde quand il clique.
      setCloseError({ id, message: e?.message ?? 'Clôture impossible.' });
    } finally {
      setChecking(null);
    }
  }

  async function closeAll() {
    setConfirmCloseAll(false);
    setClosingAll(true);
    setCloseAllMsg(null);
    try {
      const res = await api.closeAllOrders();
      // On rapporte AUSSI ce qui n'a pas pu être fermé : une position laissée ouverte faute de
      // prix réel doit être visible, sinon « tout clôturer » aurait menti sur son résultat.
      setCloseAllMsg(res.note);
      await refresh();
    } catch (e: any) {
      setCloseAllMsg(e?.message ?? 'Clôture groupée impossible.');
    } finally {
      setClosingAll(false);
    }
  }

  function startEdit(o: PaperPosition) {
    setEditingId(o.id);
    setEditSL(o.stop_loss != null ? String(o.stop_loss) : '');
    setEditTP(o.take_profit != null ? String(o.take_profit) : '');
    setEditError(null);
  }

  function cancelEdit() {
    setEditingId(null);
    setEditError(null);
  }

  async function saveEdit(id: string) {
    const sl = editSL.trim() ? parseFloat(editSL) : undefined;
    const tp = editTP.trim() ? parseFloat(editTP) : undefined;
    if (sl === undefined && tp === undefined) {
      setEditError('Indique un stop ou un objectif.');
      return;
    }
    if ((editSL.trim() && Number.isNaN(sl)) || (editTP.trim() && Number.isNaN(tp))) {
      setEditError('Valeur numérique invalide.');
      return;
    }
    setEditSaving(true);
    setEditError(null);
    try {
      await api.updateOrderLevels(id, { stop_loss: sl, take_profit: tp });
      setEditingId(null);
      await refresh();
    } catch (e: any) {
      setEditError(e.message);
    } finally {
      setEditSaving(false);
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

  // Une seule position, rendue identiquement qu'elle soit ouverte ou clôturée — extrait pour
  // éviter de dupliquer ce bloc entre les deux groupes (en cours / clôturés) affichés plus bas.
  function positionCard(o: PaperPosition) {
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
          {/* Pips gagnés ou perdus : la lecture du trade indépendante de la taille de position. */}
          {o.pips != null && (
            <span className={`rounded px-2 py-0.5 font-mono text-xs ${o.pips >= 0 ? 'bg-buy/15 text-buy' : 'bg-sell/15 text-sell'}`}
              title={`${o.pips >= 0 ? 'Gagné' : 'Perdu'} ${Math.abs(o.pips)} ${o.pips_label ?? 'pips'} ${closed ? '(réalisé)' : '(latent)'}`}>
              {o.pips >= 0 ? '+' : ''}{o.pips} pips
            </span>
          )}
          {!closed && editingId !== o.id && (
            <span className="ml-auto flex items-center gap-2">
              {confirmCloseId === o.id ? (
                <>
                  <span className="text-xs text-muted">Clôturer au prix du marché ?</span>
                  <button onClick={() => manualClose(o.id)} disabled={checking === o.id}
                    className="rounded border border-sell bg-sell/10 px-2 py-0.5 text-xs font-medium text-sell hover:bg-sell/20 disabled:opacity-50">
                    {checking === o.id ? '…' : 'Oui, clôturer'}
                  </button>
                  <button onClick={() => setConfirmCloseId(null)} disabled={checking === o.id}
                    className="rounded border border-border px-2 py-0.5 text-xs text-muted hover:bg-[#1A1A1A]">
                    Annuler
                  </button>
                </>
              ) : (
                <>
                  <button onClick={() => startEdit(o)}
                    className="rounded border border-border px-2 py-0.5 text-xs text-muted hover:bg-[#1A1A1A]">
                    Modifier SL/TP
                  </button>
                  <button onClick={() => setConfirmCloseId(o.id)}
                    className="rounded border border-sell/50 px-2 py-0.5 text-xs text-sell hover:bg-sell/10">
                    Clôturer maintenant
                  </button>
                </>
              )}
            </span>
          )}
        </div>

        {/* Pourquoi la clôture a échoué, À CÔTÉ de la position concernée. */}
        {closeError?.id === o.id && (
          <p className="mt-2 rounded border border-sell/40 bg-sell/10 px-3 py-2 text-xs text-sell">
            {closeError.message}
          </p>
        )}

        {/* Ce qui a été choisi au lancement du trade — ou le formulaire de modification. */}
        {editingId === o.id ? (
          <div className="mt-2 space-y-2 rounded border border-border bg-background/60 p-2">
            <div className="flex flex-wrap items-end gap-3">
              <label className="block">
                <span className="mb-1 block text-[11px] text-muted">Nouveau stop</span>
                <input value={editSL} onChange={(e) => setEditSL(e.target.value)} inputMode="decimal"
                  className="w-32 rounded border border-border bg-surface px-2 py-1 text-sm text-white" />
              </label>
              <label className="block">
                <span className="mb-1 block text-[11px] text-muted">Nouvel objectif</span>
                <input value={editTP} onChange={(e) => setEditTP(e.target.value)} inputMode="decimal"
                  className="w-32 rounded border border-border bg-surface px-2 py-1 text-sm text-white" />
              </label>
              <button onClick={() => saveEdit(o.id)} disabled={editSaving}
                className="rounded bg-accent px-3 py-1 text-xs font-medium text-background hover:brightness-110 disabled:opacity-50">
                {editSaving ? 'Enregistrement…' : 'Enregistrer'}
              </button>
              <button onClick={cancelEdit} disabled={editSaving}
                className="rounded border border-border px-3 py-1 text-xs text-muted hover:bg-[#1A1A1A]">
                Annuler
              </button>
            </div>
            {editError && <p className="text-xs text-sell">{editError}</p>}
            <p className="text-[11px] text-muted">
              Le risque d&apos;origine (ce que vaut 1R pour la sécurisation automatique à +2R)
              ne change pas — seuls les niveaux affichés et le R/R informatif sont mis à jour.
            </p>
          </div>
        ) : (
        <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-muted">
          {/* Heure d'ENTRÉE : sans elle, impossible de rattacher le trade à une séance. */}
          {o.opened_at && (
            <span>Entrée le <span className="text-white">{new Date(o.opened_at).toLocaleString('fr-FR')}</span>
              {o.held_seconds != null && <span className="text-muted"> · tenue {formatDuration(o.held_seconds)}</span>}
            </span>
          )}
          {o.stop_loss != null && (
            <span>Stop : <span className="text-sell">{o.stop_loss}</span>
              {o.stop_pips != null && <span className={o.stop_pips >= 0 ? 'text-buy' : 'text-sell'}> ({o.stop_pips >= 0 ? '+' : ''}{o.stop_pips} pips)</span>}
            </span>
          )}
          {o.take_profit != null && (
            <span>Objectif : <span className="text-buy">{o.take_profit}</span>
              {o.target_pips != null && <span className={o.target_pips >= 0 ? 'text-buy' : 'text-sell'}> ({o.target_pips >= 0 ? '+' : ''}{o.target_pips} pips)</span>}
            </span>
          )}
          {o.risk_reward != null && <span>R/R : <span className="text-white">1 : {o.risk_reward}</span></span>}
          {o.risk_amount != null && <span>Risqué : <span className="text-sell">{o.risk_amount}</span></span>}
          {o.potential_profit != null && <span>Gain visé : <span className="text-buy">{o.potential_profit}</span></span>}
          {!closed && o.current_price != null && <span>Prix actuel : <span className="text-white">{o.current_price}</span></span>}
          {!closed && o.r_multiple != null && <span>En multiples de risque : <span className="text-white">{o.r_multiple} R</span></span>}
        </div>
        )}

        {/*
          POURQUOI CE TRADE A ÉTÉ PRIS. Ces informations sont enregistrées à l'ouverture et ne sont
          jamais recalculées : c'est le raisonnement du moment, pas une reconstruction après coup.
          Sans elles, une position ouverte automatiquement n'a aucune justification consultable —
          on voit le résultat, jamais la décision qui l'a produit.
        */}
        {o.trigger && (
          <div className="mt-2 rounded border border-border/60 bg-background/40 px-3 py-2">
            <p className="text-[11px] font-medium text-white/85">Pourquoi ce trade a été ouvert</p>
            <p className="mt-0.5 text-[11px] leading-relaxed text-muted">
              Déclencheur 15 min — <span className="text-white/80">{o.trigger}</span>
            </p>
            <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-[10.5px] text-muted">
              {o.session_window && (
                <span>Fenêtre à l&apos;entrée : <span className="text-white/75">{o.session_window}</span></span>
              )}
              {o.atr_pct != null && (
                <span>Volatilité journalière : <span className="text-white/75">{o.atr_pct} %</span></span>
              )}
              {o.risk_pct != null && (
                <span>Capital risqué : <span className="text-white/75">{o.risk_pct} %</span></span>
              )}
              {o.pair_verdict && (
                <span>Verdict de la paire : <span className="text-white/75">{o.pair_verdict}</span>
                  {o.conviction_mult != null && o.conviction_mult !== 1 && (
                    <span className="text-muted"> (taille ×{o.conviction_mult})</span>
                  )}
                </span>
              )}
            </div>
          </div>
        )}

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
            {o.pips != null && (
              <> · <span className={o.pips >= 0 ? 'text-buy' : 'text-sell'}>
                {o.pips >= 0 ? '+' : ''}{o.pips} {o.pips_label ?? 'pips'}
              </span></>
            )}
            {/* Pourquoi la position s'est fermée : « stop touché » sur un trade GAGNANT
                n'explique rien, le motif dit désormais que le stop avait été remonté. */}
            {o.close_reason && <> · {o.close_reason}</>}
          </p>
        )}
      </div>
    );
  }

  const openPositions = snap?.positions.filter((o) => !o.closed) ?? [];
  const closedPositions = snap?.positions.filter((o) => o.closed) ?? [];

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
          <span className="flex items-center gap-3 text-[11px] text-muted">
            {/*
              « Tout clôturer » : le geste de fin de séance, ou avant une annonce. Le faire position
              par position prend du temps — et ce temps compte précisément dans ces moments-là.
              Confirmation obligatoire : l'action est irréversible et touche toutes les positions.
            */}
            {(snap?.open_count ?? 0) > 0 && (
              confirmCloseAll ? (
                <span className="flex items-center gap-2">
                  <span className="text-white/80">Clôturer les {snap?.open_count} positions ?</span>
                  <button onClick={closeAll} disabled={closingAll}
                    className="rounded border border-sell bg-sell/10 px-2 py-0.5 font-medium text-sell hover:bg-sell/20 disabled:opacity-50">
                    {closingAll ? '…' : 'Oui, tout clôturer'}
                  </button>
                  <button onClick={() => setConfirmCloseAll(false)} disabled={closingAll}
                    className="rounded border border-border px-2 py-0.5 text-muted hover:bg-[#1A1A1A]">
                    Annuler
                  </button>
                </span>
              ) : (
                <button onClick={() => setConfirmCloseAll(true)}
                  className="rounded border border-sell/50 px-2 py-0.5 text-sell hover:bg-sell/10">
                  Tout clôturer
                </button>
              )
            )}
            <span>
              <span className={auto.refreshing ? 'text-accent' : ''}>🔄 Suivi automatique</span> · {refreshLabel(auto)}
            </span>
          </span>
        </div>

        {/* Résultat de la clôture groupée : ce qui a été fermé, et ce qui est resté ouvert. */}
        {closeAllMsg && (
          <p className="rounded border border-border bg-surface px-3 py-2 text-xs text-white/80">
            {closeAllMsg}
          </p>
        )}

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

        {/* EN COURS et CLÔTURÉS, séparés : deux listes chronologiques mélangées obligeaient à
            chercher les positions encore actives au milieu de l'historique. */}
        {openPositions.length > 0 && (
          <div className="space-y-2">
            <h3 className="text-sm font-semibold text-accent">🟢 En cours ({openPositions.length})</h3>
            {openPositions.map(positionCard)}
          </div>
        )}

        {closedPositions.length > 0 && (
          <div className="space-y-2">
            <h3 className="mt-2 text-sm font-semibold text-muted">📋 Clôturés ({closedPositions.length})</h3>
            {closedPositions.map(positionCard)}
          </div>
        )}
      </section>
    </div>
  );
}

/** Durée de détention, lisible : « 3 j 4 h », « 2 h 15 min », « 40 min ». */
function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d} j ${h % 24} h` : `${d} j`;
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
