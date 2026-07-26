"""Routes des signaux : génération à la demande + historique (isolé par tenant)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from pydantic import BaseModel

from app.core.deps import current_user, store_dep
from app.core.plans import require_feature
from app.models.entities import User
from app.models.schemas import GenerateSignalRequest
from app.models.signal import SignalCard
from app.repositories.store import AppStore
from app.services import risk_service, signal_service

router = APIRouter(prefix="/api/signals", tags=["signals"])


class VerifyRequest(BaseModel):
    symbol: str
    timeframe: str = "swing"
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


_DAILY_TF = {"scalp": "5m", "intraday": "15m", "swing": "1h", "position": "4h", "daily": "1d"}


@router.get("/daily-picks")
async def daily_picks(
    refresh: bool = False,
    timeframe: str = "1h",
    user: User = Depends(require_feature("backtesting")),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Sélection du jour par marché (graduée, mise en cache par timeframe).

    `timeframe` : 5m | 15m | 1h | 4h | 1d (ou alias scalp/intraday/swing/position/daily).
    Les unités plus longues (4h, 1d) filtrent le bruit -> signaux généralement plus fiables.
    """
    from datetime import UTC, datetime

    tf = _DAILY_TF.get(timeframe, timeframe)
    today = datetime.now(UTC).date().isoformat()
    # Le 1h garde la clé historique (compat digest) ; les autres TF ont leur propre cache.
    key = today if tf == "1h" else f"{today}:{tf}"
    cached = store.records.get("daily_picks", key)
    if cached and not refresh:
        return cached
    picks = await signal_service.daily_picks(timeframe=tf)
    return store.records.put(
        "daily_picks", key,
        {"date": today, "timeframe": tf, "picks": picks, "generated_at": datetime.now(UTC).isoformat()},
    )


@router.get("/top-trades")
async def top_trades(
    refresh: bool = False,
    count: int = 5,
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """LES trades du jour, produits par la STRATÉGIE du desk (playbook multi-unités de temps).

    Cascade : Mensuel + Journalier (tendance de fond + supports/résistances majeurs) → Journalier
    détaillé (RSI14, MA20/MA50, volume, tendance VWAP, divergences RSI/MACD, Fibonacci en cas de
    correction) → 4 h (mêmes facteurs) → **entrée en 15 min uniquement**, avec R/R ≥ 1:2 et
    objectif ≥ 100 pips. L'univers privilégie le chevauchement Londres / New York.

    Chaque setup est étiqueté `ready` (déclencheur 15 min actif, exécutable) ou `armed` (contexte
    validé, en attente du déclencheur). Recalculé automatiquement à chaque ouverture de session.
    """
    from datetime import UTC, datetime

    from app.data import sessions as sessions_mod

    today = datetime.now(UTC).date().isoformat()
    cached = store.records.get("top_trades", today)
    # Le cache n'est valable que TANT QU'ON EST DANS LA MÊME FENÊTRE de session : à l'ouverture de
    # Londres ou de New York, le marché change de visage -> on recalcule.
    if cached and not refresh:
        now_zones = sessions_mod.session_context()["kill_zones"]
        if ((cached.get("session") or {}).get("kill_zones") or []) == now_zones:
            return cached
    payload = await signal_service.daily_top_trades(max(1, min(count, 10)))
    payload["date"] = today
    return store.records.put("top_trades", today, payload)


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
    """Scanner de marché : classe les symboles par conviction (flag haute-conviction ADX>25).

    `session` (asian|london|newyork) restreint l'univers aux paires liquides de cette session.
    Le scanner utilise TON contexte (exposition + apprentissage) -> ★ identique à ton analyse.
    """
    universe = None
    if session:
        from app.data import sessions as sessions_mod
        universe = sessions_mod.session_universe(session)
        if asset_class:
            universe = [u for u in universe if u["asset_class"] == asset_class]
    results = await signal_service.scan_market(
        asset_class=asset_class, timeframe=timeframe, limit=min(limit, 40),
        high_conviction_only=high_conviction_only, symbols=universe,
        confirm_mtf=True,  # consolidation = même décision (et même contexte) que ton analyse
        user=user, store=store,
    )
    return {
        "count": len(results),
        "high_conviction": sum(1 for r in results if r["high_conviction"]),
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
            outcome, _, _ = await replay.replay_outcome(
                p.get("asset", ""), m["blocked_direction"], m.get("blocked_entry"),
                m.get("blocked_sl"), m.get("blocked_tp"), p.get("created_at"),
            )
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
