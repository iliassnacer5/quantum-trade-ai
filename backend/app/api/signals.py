"""Routes des signaux : génération à la demande + historique (isolé par tenant)."""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, status

from pydantic import BaseModel

from app.core.deps import current_user, store_dep
from app.core.plans import require_feature
from app.models.entities import User
from app.models.schemas import GenerateSignalRequest
from app.models.signal import SignalCard
from app.repositories.store import AppStore
from app.services import live_snapshot, risk_service, signal_service

router = APIRouter(prefix="/api/signals", tags=["signals"])


class VerifyRequest(BaseModel):
    symbol: str
    timeframe: str = "1h"
    direction: str = "HOLD"
    confidence: int = 0
    consensus_pct: int = 0
    risk_reward: float = 0.0
    mtf_aligned: int = 0
    mtf_total: int = 0
    adx: float | None = None

# Gating par plan (cf. grille tarifaire) : Free = 1 marché.
_PLAN_MARKETS = {"free": 1, "starter": 3, "pro": 999, "elite": 999, "enterprise": 999}


@router.post("/generate", response_model=SignalCard)
async def generate(
    body: GenerateSignalRequest,
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> SignalCard:
    tenant = store.tenants.get(user.tenant_id)
    plan = tenant.plan if tenant else "free"
    allowed = _PLAN_MARKETS.get(plan, 1)
    base = body.asset.split("/")[0]
    distinct_markets = {s.payload.get("asset", "").split("/")[0] for s in store.signals.list_for_tenant(user.tenant_id, 1000)}
    if base not in distinct_markets and len(distinct_markets) >= allowed:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            f"Plan '{plan}' limité à {allowed} marché(s). Passez à un plan supérieur.",
        )

    # Garde-fous de risque (exposition / signaux quotidiens) — protection du capital.
    ok, reason = risk_service.check_can_generate(user, store)
    if not ok:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, reason)

    return await signal_service.generate_for_user(
        user, store, asset=body.asset, timeframe=body.timeframe, notify=body.notify
    )


_DAILY_TF = {"15min": "15m", "1h": "1h", "4h": "4h", "1d": "1d", "1week": "1w", "1month": "1M"}


@router.get("/daily-picks")
async def daily_picks(
    refresh: bool = False,
    timeframe: str = "1h",
    user: User = Depends(require_feature("backtesting")),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Sélection du jour par marché (graduée, servie depuis un instantané précalculé).

    `timeframe` : 15m | 1h | 4h | 1d | 1w | 1M (ou les libellés 15min/1h/4h/1d/1week/1month).
    Les unités plus longues (4h, 1d) filtrent le bruit -> signaux généralement plus fiables.

    **Réponse immédiate** : cette route ne calcule plus rien en temps normal. Une boucle de fond
    (`scheduler.daily_picks_loop`) précalcule les six unités de temps, exactement comme
    `live_snapshot` le fait pour les trades du jour. Avant ce changement, chaque première visite
    d'une unité de temps lançait jusqu'à 32 backtests complets DANS la requête. Chaque réponse porte
    son âge (`age_seconds`, `stale`) ; `refresh=true` force un recalcul synchrone.
    """
    from app.services import daily_picks_cache

    tf = _DAILY_TF.get(timeframe, timeframe)
    return await daily_picks_cache.get(store, tf, force=refresh)


@router.get("/top-trades")
async def top_trades(
    refresh: bool = False,
    count: int = 0,
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """LES trades du jour, produits par la STRATÉGIE du desk (playbook multi-unités de temps).

    Trois étapes, appliquées à tous les marchés : la tendance est mesurée par six indicateurs sur
    D1/4h/1h/15min puis FIGÉE ; l'entrée en 15 min n'est autorisée que si au moins trois
    confirmations pondérées se rejoignent ; le stop est posé sur le niveau qui invaliderait le
    scénario et les objectifs devant le premier obstacle réel, avec un R/R entre 1:2 et 1:3.

    **Réponse immédiate** : cette route ne calcule rien, elle sert l'instantané produit en continu
    par la boucle de fond (cf. `services.live_snapshot`). C'est ce qui permet à la page de se
    rafraîchir toutes les 10 secondes au lieu d'attendre un recalcul complet. Chaque réponse porte
    son âge (`age_seconds`, `stale`). `refresh=true` force un recalcul synchrone.

    Chaque setup est étiqueté `ready` (déclencheur 15 min actif, exécutable) ou `armed` (contexte
    validé) — les setups armés sont ouverts AUTOMATIQUEMENT en compte démo dès que le déclencheur
    15 min se forme, sans aucune action de l'utilisateur.

    `count=0` (défaut) rend TOUS les setups conformes, sans plafond : la stratégie ne dit pas « les
    cinq meilleurs », elle dit « ceux qui remplissent les trois étapes ». `count>0` n'extrait que
    les N premiers du classement.
    """
    return await live_snapshot.get(store, count=max(0, count), force=refresh)


@router.get("/auto-entry")
async def auto_entry_status(
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """État de l'AUTO-ENTRÉE : ce que le robot surveille, et ce qu'il a ouvert tout seul.

    L'auto-entrée n'engage QUE le compte démo (papier) : aucune position réelle ne peut être
    ouverte sans action humaine, quelle que soit la configuration.
    """
    from app.core.config import get_settings
    from app.services import auto_entry_service

    s = get_settings()
    snap = live_snapshot.current() or {}
    watching = [
        {"symbol": p["symbol"], "direction": p.get("direction"), "tier": p.get("tier"),
         "reason": (p.get("reasons") or [""])[0],
         # État du marché de CE symbole : « sous surveillance » ne veut pas dire la même chose
         # selon que la place est ouverte (le robot ouvrira) ou fermée (il analyse seulement).
         # Sans cette distinction, la liste laisse croire qu'une position va partir alors que le
         # garde-fou d'heures de marché l'en empêchera.
         "tradable_now": p.get("tradable_now", True),
         "market_status": p.get("market_status")}
        for p in (snap.get("picks") or []) if p.get("tier") in ("ready", "armed")
    ]
    return {
        "enabled": auto_entry_service.enabled(),
        "mode": "paper",
        "interval_seconds": s.playbook_auto_entry_interval,
        "cooldown_min": s.playbook_auto_entry_cooldown_min,
        # Ce qui pourrait encore refuser une entrée dont le déclencheur s'est formé. Affiché parce
        # qu'un refus silencieux est exactement ce qui a fait croire à une panne.
        "pair_gating": s.playbook_pair_gating,
        "watching": watching,
        "ready_now": [w["symbol"] for w in watching if w["tier"] == "ready"],
        "recent": auto_entry_service.recent_events(store, user.tenant_id),
        # Setups marqués EXÉCUTABLES à l'écran mais refusés au dernier passage de veille pour CE
        # compte (garde-fou de portefeuille, corrélation, anti-doublon…) — sans ce champ, un refus
        # légitime est indiscernable d'une panne du robot.
        "blocked": auto_entry_service.blocked_for(store, user.tenant_id),
        # QUAND ce passage a eu lieu. Un refus sans date est invérifiable : c'est ce qui a permis à
        # un « déjà 2 positions ouvertes » de rester affiché après la remise à zéro du portefeuille,
        # en contradiction avec Paper Trading qui montrait zéro position.
        "last_run_at": auto_entry_service.last_run_at(store),
        # Setups écartés parce que LEUR MARCHÉ EST FERMÉ (week-end, hors Londres/New York). Champ
        # distinct de `blocked` : ce refus ne dépend pas du compte mais de l'état du marché.
        "market_closed": auto_entry_service.market_closed_now(store),
        "note": (
            "Les setups armés sont ouverts automatiquement en COMPTE DÉMO dès que le déclencheur "
            "15 min se forme. Aucun clic, aucun argent réel."
            + ("" if s.playbook_pair_gating else
               f" Aucun filtre de verdict ne s'y oppose : tout déclencheur formé part, sauf "
               f"doublon dans les {s.playbook_auto_entry_cooldown_min} dernières minutes.")
            if auto_entry_service.enabled()
            else "Auto-entrée désactivée : les setups armés attendent une ouverture manuelle."
        ),
    }


@router.post("/auto-entry/run")
async def auto_entry_run(
    _user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Force un passage de veille immédiat (utile pour vérifier le câblage sans attendre la boucle)."""
    from app.services import auto_entry_service

    return await auto_entry_service.run_auto_entry(store)


@router.post("/auto-entry/reset")
async def auto_entry_reset(
    close_positions: bool = True,
    relaunch: bool = True,
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """REMET L'AUTO-ENTRÉE À ZÉRO : positions démo en cours neutralisées + traces effacées.

    N'affecte QUE le compte démo de l'utilisateur : la boucle d'auto-entrée ne connaît pas d'autre
    mode, et le reset ne regarde que les ordres `paper`. Les trades déjà clôturés sont conservés —
    ce sont eux qui portent la mesure, les effacer reviendrait à ne plus rien savoir.

    Les positions rouvertes ne sont pas comptées gagnantes ou perdantes mais `reset` : elles n'ont
    pas été jouées jusqu'au bout, leur donner une issue serait inventer un résultat.

    `relaunch=true` (défaut) enchaîne aussitôt sur un passage de veille, pour que la remise à zéro
    ne laisse pas le desk inactif jusqu'au prochain tour de boucle.
    """
    from app.services import auto_entry_service

    out = auto_entry_service.reset(store, user.tenant_id, close_positions=close_positions)
    if relaunch:
        out["run"] = await auto_entry_service.run_auto_entry(store)
    return out


@router.get("/playbook/{symbol:path}")
async def playbook_for_symbol(
    symbol: str,
    _user: User = Depends(current_user),
) -> dict:
    """Applique la stratégie complète à UN symbole et renvoie le détail des 4 étapes.

    Retourne la checklist (chaque étape réussie/échouée avec sa valeur), les couches d'analyse
    mensuelle/journalière/4 h/15 min, les niveaux majeurs, la fenêtre de session, et — si tout est
    réuni — l'entrée, le stop, les objectifs, le R/R et le nombre de pips visés.
    """
    from app.data import symbols as symbols_catalog
    from app.services import playbook_service

    setup = await playbook_service.build_setup(symbols_catalog.normalize(symbol))
    return {**setup.as_dict(), "summary": setup.summary()}


@router.post("/verify")
async def verify(
    body: VerifyRequest,
    user: User = Depends(require_feature("backtesting")),
) -> dict:
    """Vérifie la fiabilité d'un signal : backtest auto de la paire + verdict checklist."""
    return await signal_service.verify_signal(
        body.symbol, body.timeframe,
        confidence=body.confidence, consensus_pct=body.consensus_pct, risk_reward=body.risk_reward,
        mtf_aligned=body.mtf_aligned, mtf_total=body.mtf_total, adx=body.adx, direction=body.direction,
    )


@router.get("/scan")
async def scan(
    asset_class: str | None = None,
    timeframe: str = "1h",
    limit: int = 20,
    high_conviction_only: bool = False,
    session: str | None = None,
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Scanner de marché — c'est LA STRATÉGIE DU DESK qui a le dernier mot sur chaque ligne.

    Chaque symbole porte le verdict de la stratégie (`ready` / `armed` / refusé), ses métriques
    (score de tendance, confirmations réunies et leur poids, R/R, niveaux) et surtout `why` :
    comment la tendance a été établie, quelles confirmations se sont réunies, et — quand il n'y a
    pas de trade — ce qui a bloqué. Un symbole que la stratégie refuse ne peut pas s'afficher en
    BUY : le scanner, les trades du jour et l'analyse détaillée lisent le même calcul.

    `session` (asian|london|newyork) restreint l'univers aux paires liquides de cette session.
    """
    universe = None
    if session:
        from app.data import sessions as sessions_mod
        universe = sessions_mod.session_universe(session)
        if asset_class:
            universe = [u for u in universe if u["asset_class"] == asset_class]
    # Plafond relevé à 100 : la stratégie balaie désormais tout le catalogue, et un scanner qui n'en
    # montre que 40 afficherait « non balayé » sur des symboles qu'elle a bel et bien analysés.
    results = await signal_service.scan_market(
        asset_class=asset_class, timeframe=timeframe, limit=min(limit, 100),
        high_conviction_only=high_conviction_only, symbols=universe,
        confirm_mtf=True,  # consolidation = même décision (et même contexte) que ton analyse
        user=user, store=store,
    )
    tiers = Counter(r.get("playbook_tier") or "non balayé" for r in results)
    return {
        "count": len(results),
        "high_conviction": sum(1 for r in results if r["high_conviction"]),
        # Ce que LA STRATÉGIE a conclu sur l'univers scanné, en une ligne.
        "ready": tiers.get("ready", 0),
        "armed": tiers.get("armed", 0),
        "refused": sum(v for k, v in tiers.items() if k not in ("ready", "armed", "non balayé")),
        "not_scanned": tiers.get("non balayé", 0),
        "strategy": (
            "Playbook — tendance multi-indicateurs figée (D1/4h/1h/15min) → entrée par confluence "
            "pondérée (≥ 3 confirmations dont une forte) → stop et objectifs posés sur des niveaux"
        ),
        "results": results,
    }


@router.delete("")
async def clear_signals(
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Vide l'historique des signaux du tenant (repartir propre)."""
    deleted = store.signals.clear_for_tenant(user.tenant_id)
    return {"deleted": deleted}


@router.post("/mode")
async def set_signal_mode(
    mode: str = "strict",
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Choisit la sévérité des filtres : strict (fiabilité max) | balanced | aggressive (plus de signaux).

    Curseur fiabilité <-> quantité : moins strict = plus de BUY/SELL mais plus de faux signaux."""
    from app.signal_engine.quality import MODES

    if mode not in MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"mode invalide (choix : {', '.join(MODES)})")
    store.records.put("signal_mode", user.tenant_id, {"mode": mode}, tenant_id=user.tenant_id)
    return {"mode": mode, "thresholds": MODES[mode]}


@router.get("/mode")
async def get_signal_mode(
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    from app.signal_engine.quality import MODES

    mode = (store.records.get("signal_mode", user.tenant_id) or {}).get("mode", "strict")
    return {"mode": mode, "thresholds": MODES.get(mode, MODES["strict"])}


@router.get("/track-record")
async def signals_track_record(
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Track record HONNÊTE des prédictions : issues réelles observées + ce que les filtres t'ont ÉVITÉ.

    - `observed` : trades résolus (gagnés/perdus, via rejeu auto du Journal).
    - `avoided`  : signaux BLOQUÉS par les gates (multi-TF, qualité, blackout) rejoués « et si » —
      combien auraient perdu (capital protégé) et combien auraient gagné (transparence totale)."""
    from app.data import replay
    from app.services import journal_service

    entries = journal_service.recent_entries(store, user.tenant_id, limit=500)
    observed = journal_service.stats(entries)

    would_lost = would_won = still_open = 0
    for s in store.signals.list_for_tenant(user.tenant_id, limit=100):
        p = s.payload
        m = p.get("metrics") or {}
        if p.get("direction") != "HOLD" or not m.get("blocked_direction"):
            continue
        try:
            # Un verdict `undetermined` retombe dans « encore ouvert » : on ne compte jamais un
            # trade évité comme gagnant ou perdant sans données réelles pour le prouver.
            outcome = (await replay.replay_outcome(
                p.get("asset", ""), m["blocked_direction"], m.get("blocked_entry"),
                m.get("blocked_sl"), m.get("blocked_tp"), p.get("created_at"),
            ))["outcome"]
        except Exception:  # noqa: BLE001
            continue
        if outcome == "lost":
            would_lost += 1
        elif outcome == "won":
            would_won += 1
        else:
            still_open += 1

    return {
        "observed": observed,
        "avoided": {
            "blocked": would_lost + would_won + still_open,
            "would_have_lost": would_lost,   # trades perdants évités = capital protégé
            "would_have_won": would_won,     # honnêteté : les filtres ratent aussi des gagnants
            "undecided": still_open,
        },
    }


@router.get("/{signal_id}")
async def get_signal(
    signal_id: str,
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Consulte UNE prédiction en détail : agents, gates, news, métriques — le pourquoi complet."""
    s = store.signals.get(signal_id)
    if s is None or s.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prédiction introuvable")
    payload = dict(s.payload)
    payload["id"] = s.id
    payload.setdefault("created_at", s.created_at.isoformat() if s.created_at else None)

    # Issue RÉELLE de la prédiction (résolue par le Journal) -> « a gagné / a perdu / en cours ».
    from app.services import journal_service
    entry = next(
        (e for e in journal_service.recent_entries(store, user.tenant_id, limit=500)
         if e.get("signal_id") == signal_id),
        None,
    )
    if entry and payload.get("direction") != "HOLD":
        payload["trade_outcome"] = {"outcome": entry.get("outcome"), "pnl": entry.get("pnl")}
    return payload


@router.get("")
async def list_signals(
    limit: int = 50,
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> list[dict]:
    items = store.signals.list_for_tenant(user.tenant_id, limit)
    out = []
    for s in items:
        payload = dict(s.payload)
        payload["id"] = s.id
        out.append(payload)
    return out
