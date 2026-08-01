"""Routes de Backtesting (M6) — authentifié, isolé par tenant, données réelles horodatées."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status

from app.backtest.engine import run_backtest
from app.backtest.schemas import BacktestConfig, BacktestReport
from app.core.config import get_settings
from app.core.deps import current_user, store_dep
from app.core.plans import require_feature
from app.data.ohlcv import get_ohlcv
from app.data.synthetic import generate_candles
from app.domain.indicators import Candle
from app.models.entities import User
from app.repositories.store import AppStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/backtest", tags=["backtest"])

_STEP = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


# ---------------------------------------------------------------------------------------
# Backtest de LA STRATÉGIE DU DESK (playbook) — forex et or, plusieurs paires
# ---------------------------------------------------------------------------------------
@router.get("/playbook")
async def playbook_backtest_report(
    _user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Dernier backtest de la STRATÉGIE du desk : classement des paires + conclusion rédigée.

    Trois passes complémentaires : une passe LONGUE (5 ans, échelle swing — contexte mensuel et
    hebdomadaire, entrée journalière), une passe PORTÉE (le réglage de production, déclencheur
    évalué en 1 h sur ~2 ans) et une passe FIDÉLITÉ (le vrai déclencheur 15 min, sur les ~60 jours
    réellement disponibles). Les limites de données sont exposées telles quelles dans `data_limits`,
    et la FRÉQUENCE d'opportunité par symbole dans `frequency`.
    """
    from app.backtest import playbook_backtest as pbt

    rec = store.records.get(pbt.COLLECTION, pbt.LATEST)
    state = pbt.run_state()
    if not rec:
        return {"available": False, "run_state": state,
                "note": ("Backtest en cours…" if state["running"]
                         else "Aucun backtest de la stratégie n'a encore été exécuté."),
                "universe": pbt.full_universe()}
    return {"available": True, "run_state": state, **rec}


@router.post("/playbook/run")
async def playbook_backtest_run(
    _user: User = Depends(require_feature("backtesting")),
    store: AppStore = Depends(store_dep),
) -> dict:
    """LANCE le backtest complet en arrière-plan et rend la main IMMÉDIATEMENT.

    Un backtest complet dure une dizaine de minutes : le tenir dans le fil d'une requête HTTP
    laisserait le navigateur attendre indéfiniment et bloquerait un worker. On démarre donc une
    tâche de fond et l'interface suit l'avancement via `GET /api/backtest/playbook` (`run_state`).
    """
    import asyncio

    from app.backtest import playbook_backtest as pbt

    state = pbt.run_state()
    if state["running"]:
        return {"started": False, "run_state": state,
                "note": "Un backtest est déjà en cours — inutile d'en lancer un second."}

    async def _job() -> None:
        try:
            await pbt.run_backtest(store)
        except Exception as exc:  # noqa: BLE001 — une tâche de fond ne doit jamais faire tomber l'API
            logger.exception("Backtest de la stratégie échoué (%s)", exc)

    asyncio.create_task(_job())
    return {"started": True, "run_state": pbt.run_state(),
            "note": ("Backtest lancé en arrière-plan (une dizaine de minutes). "
                     "Cette page se met à jour toute seule.")}


@router.get("/playbook/markets")
async def playbook_markets(
    _user: User = Depends(current_user),
) -> dict:
    """Les marchés backtestables et leurs instruments — ce que l'interface propose dans ses menus.

    C'est la MÊME liste que celle du backtest complet : on ne veut pas pouvoir lancer depuis la page
    un backtest sur un symbole que le passage hebdomadaire ne couvre pas, les deux chiffres
    deviendraient incomparables.
    """
    from app.backtest import playbook_backtest as pbt

    return {
        "markets": [
            {"market": cls, "label": pbt.MARKET_LABELS.get(cls, cls),
             "symbols": syms, "count": len(syms)}
            for cls, syms in pbt.MARKET_UNIVERSES.items()
        ],
        "entry_timeframes": [
            {"tf": "1h", "label": "1 h — profondeur maximale (~2 ans)",
             "note": "la passe qui donne le recul statistique"},
            {"tf": "15m", "label": "15 min — le vrai déclencheur (~60 jours)",
             "note": "l'unité d'entrée réelle, mais sur un historique court"},
        ],
    }


@router.get("/playbook/symbol")
async def playbook_symbol_report(
    symbol: str,
    entry_tf: str = "1h",
    _user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Dernier backtest de la stratégie du desk sur UN symbole (avec son état d'exécution)."""
    from app.backtest import playbook_backtest as pbt

    state = pbt.symbol_run_state(symbol, entry_tf)
    rec = pbt.symbol_report(store, symbol, entry_tf)
    if not rec:
        return {"available": False, "symbol": symbol, "entry_timeframe": entry_tf,
                "run_state": state,
                "note": ("Backtest en cours…" if state["running"]
                         else f"{symbol} n'a pas encore été backtesté en {entry_tf}.")}
    return {"available": True, "run_state": state, **rec}


@router.post("/playbook/symbol/run")
async def playbook_symbol_run(
    symbol: str,
    entry_tf: str = "1h",
    step: int = 4,
    _user: User = Depends(require_feature("backtesting")),
    store: AppStore = Depends(store_dep),
) -> dict:
    """LANCE le backtest de la stratégie sur UN symbole, en arrière-plan.

    Même code, mêmes réglages et même walk-forward strict que le backtest complet — restreints à un
    seul instrument. Le résultat se récupère via `GET /api/backtest/playbook/symbol`.

    `entry_tf` : « 1h » (profondeur maximale, ~2 ans) ou « 15m » (le vrai déclencheur, ~60 jours).
    `step` : pas d'évaluation en bougies. Plus il est petit, plus la mesure est fine et lente.
    """
    import asyncio

    from app.backtest import playbook_backtest as pbt
    from app.data import symbols as symbols_catalog

    symbol = symbols_catalog.normalize(symbol)
    if entry_tf not in ("1h", "15m", "4h", "1d"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"unité d'entrée « {entry_tf} » non supportée (1h, 15m, 4h ou 1d)")
    state = pbt.symbol_run_state(symbol, entry_tf)
    if state["running"]:
        return {"started": False, "symbol": symbol, "run_state": state,
                "note": f"Un backtest de {symbol} en {entry_tf} est déjà en cours."}

    async def _job() -> None:
        try:
            await pbt.run_symbol_job(store, symbol, entry_tf=entry_tf, step=max(1, step))
        except Exception as exc:  # noqa: BLE001 — une tâche de fond ne fait jamais tomber l'API
            logger.exception("Backtest %s (%s) échoué (%s)", symbol, entry_tf, exc)

    asyncio.create_task(_job())
    return {"started": True, "symbol": symbol, "entry_timeframe": entry_tf,
            "run_state": pbt.symbol_run_state(symbol, entry_tf),
            "note": ("Backtest lancé en arrière-plan. Le résultat arrive dans "
                     "GET /api/backtest/playbook/symbol — la page se met à jour toute seule.")}


@router.get("/playbook/verdicts")
async def playbook_pair_verdicts(
    _user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """VERDICT PAR PAIRE (🟢/🟡/🔴) issu du backtest hebdomadaire + matrice des déclencheurs.

    🟢 = espérance ≥ +0,4 R sur n ≥ 20 trades, confirmée sur deux passages consécutifs — seules
    ces paires sont auto-tradées. 🟡 = analysée mais non auto-tradée. 🔴 = exclue (la stratégie
    perd ici). Les critères exacts sont dans `criteria` ; les refus récents dans `refusals`.
    """
    from app.services import verdict_service

    out = verdict_service.report(store)
    out["refusals"] = verdict_service.recent_refusals(store, limit=30)
    return out


@router.get("/playbook/volatility-ab")
async def playbook_volatility_ab_report(
    _user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Résultat du dernier A/B volatilité (adapt / refuse / stop k×ATR 4 h)."""
    from app.backtest import playbook_backtest as pbt

    rec = store.records.get(pbt.AB_COLLECTION, pbt.LATEST)
    if not rec:
        return {"available": False, "run_state": pbt.run_state(),
                "variants": list(pbt.VOLATILITY_VARIANTS),
                "note": "A/B volatilité jamais exécuté — lance-le via POST /playbook/volatility-ab/run."}
    return {"available": True, "run_state": pbt.run_state(), **rec}


@router.post("/playbook/volatility-ab/run")
async def playbook_volatility_ab_run(
    _user: User = Depends(require_feature("backtesting")),
    store: AppStore = Depends(store_dep),
) -> dict:
    """LANCE l'A/B volatilité en arrière-plan (3 passes complètes ≈ 30 min)."""
    import asyncio

    from app.backtest import playbook_backtest as pbt

    state = pbt.run_state()
    if state["running"]:
        return {"started": False, "run_state": state,
                "note": "Un backtest est déjà en cours — attendre qu'il se termine."}

    async def _job() -> None:
        try:
            await pbt.run_volatility_ab(store)
        except Exception as exc:  # noqa: BLE001 — une tâche de fond ne doit jamais faire tomber l'API
            logger.exception("A/B volatilité échoué (%s)", exc)

    asyncio.create_task(_job())
    return {"started": True, "run_state": pbt.run_state(),
            "note": "A/B volatilité lancé (3 passes ≈ 30 min). Résultat via GET /playbook/volatility-ab."}


@router.get("/playbook/volume-ab")
async def playbook_volume_ab_report(
    _user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """A/B des LEVIERS DE VOLUME : ce que chaque assouplissement rapporte en trades et coûte en R.

    Répond à « comment avoir plus de trades » par la mesure. Chaque variante desserre UN verrou
    (seuil de confluence, déclencheur divergence, taille d'objectif, bande de R/R, netteté de
    tendance) et une dernière les desserre tous, pour donner la borne haute et son prix. Le
    classement se lit sur le GAIN TOTAL en R : plus de trades ne vaut que si le total monte.
    """
    from app.backtest import playbook_backtest as pbt

    rec = store.records.get(pbt.VOLUME_AB_COLLECTION, pbt.LATEST)
    if not rec:
        return {"available": False, "run_state": pbt.run_state(),
                "variants": {k: v for k, v in pbt.VOLUME_VARIANTS.items()},
                "note": ("A/B volume jamais exécuté — lance-le via POST /playbook/volume-ab/run. "
                         "Le levier qui ne coûte rien (élargir l'univers balayé) est chiffré, lui, "
                         "dans la section `frequency` du backtest.")}
    return {"available": True, "run_state": pbt.run_state(), **rec}


@router.post("/playbook/volume-ab/run")
async def playbook_volume_ab_run(
    _user: User = Depends(require_feature("backtesting")),
    store: AppStore = Depends(store_dep),
    universe: str | None = None,
    step: int = 4,
) -> dict:
    """LANCE l'A/B des leviers de volume en arrière-plan (8 passes — compter plusieurs heures).

    `universe` : liste de symboles séparés par des virgules pour restreindre la mesure (recommandé,
    sinon huit passes sur tout le catalogue). `step` : pas d'évaluation en bougies 1 h.
    """
    import asyncio

    from app.backtest import playbook_backtest as pbt

    state = pbt.run_state()
    if state["running"]:
        return {"started": False, "run_state": state,
                "note": "Un backtest est déjà en cours — attendre qu'il se termine."}
    syms = [s.strip().upper() for s in universe.split(",") if s.strip()] if universe else None

    async def _job() -> None:
        try:
            await pbt.run_volume_ab(store, universe=syms, step=max(1, step))
        except Exception as exc:  # noqa: BLE001 — une tâche de fond ne doit jamais faire tomber l'API
            logger.exception("A/B volume échoué (%s)", exc)

    asyncio.create_task(_job())
    return {"started": True, "run_state": pbt.run_state(),
            "variants": list(pbt.VOLUME_VARIANTS),
            "note": "A/B volume lancé. Résultat via GET /api/backtest/playbook/volume-ab."}


async def _load_history(symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
    """Charge l'historique OHLCV horodaté — RÉEL, ou rien.

    Le repli synthétique qui vivait ici a été retiré : quand le fournisseur ne répondait pas, cette
    fonction fabriquait `limit` bougies déterministes et le backtest rendait ensuite un rapport
    complet (taux de réussite, profit factor, courbe d'équité) calculé sur des prix que le marché
    n'a jamais cotés — sans que rien, dans la réponse, ne le signale. C'est exactement ce que le
    projet interdit partout ailleurs (`data_allow_synthetic`, `false` par défaut) : une mesure
    inventée est pire qu'une absence de mesure, parce qu'elle a l'apparence d'une mesure.

    Une série vide remonte donc telle quelle, et l'appelant refuse le backtest (HTTP 503) au lieu
    d'en inventer un. `data_allow_synthetic=true` reste honoré pour les environnements de
    démonstration hors-ligne, et le rapport porte alors sa source.
    """
    try:
        rows = await get_ohlcv(symbol, interval=timeframe, limit=limit)
        if len(rows) >= 100:
            return [
                Candle(
                    open=r["open"], high=r["high"], low=r["low"], close=r["close"],
                    volume=r.get("volume", 0.0),
                    timestamp=datetime.fromtimestamp(r["time"], UTC),
                )
                for r in rows
            ]
        logger.warning("Historique %s (%s) trop court : %d bougies", symbol, timeframe, len(rows))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Historique %s indisponible (%s)", symbol, exc)

    if not get_settings().data_allow_synthetic:
        return []

    step = _STEP.get(timeframe, 3600)
    base = datetime.now(UTC) - timedelta(seconds=step * limit)
    return [
        Candle(c.open, c.high, c.low, c.close, c.volume, timestamp=base + timedelta(seconds=step * i))
        for i, c in enumerate(generate_candles(n=limit, seed=abs(hash(symbol)) % 10_000))
    ]


@router.post("/run", response_model=BacktestReport)
async def run(
    config: BacktestConfig,
    user: User = Depends(require_feature("backtesting")),
    store: AppStore = Depends(store_dep),
) -> BacktestReport:
    candles = await _load_history(config.symbol, config.timeframe)
    if not candles:
        # Donnée ABSENTE, pas donnée nulle : 503 « réessayer plus tard » et non 400 « ta requête est
        # mauvaise ». La distinction compte pour l'interface, qui doit afficher « indisponible » et
        # non « 0 % de réussite ».
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Données de marché indisponibles pour {config.symbol} en {config.timeframe} — "
            "backtest impossible. Aucun résultat n'est inventé : réessaie quand la source répond.",
        )
    if len(candles) < 100:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Historique insuffisant pour le backtest")
    try:
        report = await run_backtest(config, candles, tenant_id=user.tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur backtest")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    store.backtests.save_report(report)
    return report


@router.get("/reports", response_model=list[BacktestReport])
async def reports(
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> list[BacktestReport]:
    return store.backtests.list_for_tenant(user.tenant_id, limit=20)


_EXPERT_SYMBOLS = {
    "crypto": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    "forex": ["EUR/USD", "GBP/USD", "USD/JPY"],
    "stock": ["AAPL", "MSFT", "NVDA"],
    "commodity": ["XAU/USD", "XAG/USD"],
}


# Les 3 configurations de sortie de l'A/B test (cf. PLAN_FINALISATION §A.1).
_EXIT_CONFIGS = {
    "tp_only": {"trailing": False, "breakeven_r": 0.0, "staged_tp": False},   # (a) TP 2,5×ATR seul
    "be_1_5r": {"trailing": True, "trailing_mult": 3.0, "breakeven_r": 1.5, "staged_tp": False},  # (b)
    "staged": {"trailing": True, "trailing_mult": 3.0, "breakeven_r": 1.5, "staged_tp": True},    # (c)
    "current": {},  # réglages globaux actuels (référence)
}


@router.post("/exit-ab")
async def exit_ab_test(
    strategy: str = "mtf_ema",
    timeframe: str = "1h",
    _user: User = Depends(require_feature("backtesting")),
) -> dict:
    """A/B test des sorties : walk-forward des 4 configs (TP seul, BE 1,5R, TP étagé, actuelle)
    sur plusieurs symboles. Classement par expectancy/alpha -> la meilleure config gagne."""
    from app.backtest.walkforward import walk_forward as run_wf

    symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    results = []
    for name, ec in _EXIT_CONFIGS.items():
        agg = {"config": name, "symbols": []}
        for sym in symbols:
            try:
                r = await run_wf(sym, timeframe, folds=4, strategy_id=strategy, exit_config=ec or None)
                agg["symbols"].append({"symbol": sym, "win": r["avg_win_rate"], "pf": r["avg_profit_factor"],
                                       "alpha": r["avg_alpha_pct"], "trades": r["total_trades"]})
            except Exception:  # noqa: BLE001
                continue
        n = len(agg["symbols"]) or 1
        agg["avg_pf"] = round(sum(x["pf"] for x in agg["symbols"]) / n, 2)
        agg["avg_alpha"] = round(sum(x["alpha"] for x in agg["symbols"]) / n, 2)
        agg["avg_win"] = round(sum(x["win"] for x in agg["symbols"]) / n, 1)
        results.append(agg)
    results.sort(key=lambda a: (a["avg_alpha"], a["avg_pf"]), reverse=True)
    best = results[0] if results else None
    return {"strategy": strategy, "timeframe": timeframe, "results": results, "best": best,
            "note": f"Meilleure config sorties : {best['config']} (alpha {best['avg_alpha']}%, PF {best['avg_pf']})" if best else ""}


@router.get("/edge-map")
async def edge_map(
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """La CARTE DE L'EDGE : où (stratégie × symbole × TF) y a-t-il un edge prouvé out-of-sample ?

    🟢 alpha>0 + PF≥1,2 (exploitable) · 🟡 alpha>0 · 🔴 pas d'edge. Mise à jour par sweep nocturne."""
    from app.services import edge_map_service

    latest = edge_map_service.get_edge_map(store)
    if latest is None:
        return {"rows": [], "greens": 0, "yellows": 0, "reds": 0,
                "note": "Premier sweep pas encore exécuté (auto ~10 min après le démarrage, ou lance-le manuellement)."}
    return latest


@router.post("/edge-map/run")
async def edge_map_run(
    timeframe: str | None = None,
    market: str | None = None,
    _user: User = Depends(require_feature("backtesting")),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Lance le sweep manuellement (peut prendre 1-3 min). Filtres optionnels : timeframe, market."""
    from app.services import edge_map_service

    return await edge_map_service.run_edge_sweep(
        store,
        timeframes=[timeframe] if timeframe else None,
        markets=[market] if market else None,
    )


@router.post("/expert-validation")
async def expert_validation(
    market: str = "crypto",
    timeframe: str = "1h",
    _user: User = Depends(require_feature("backtesting")),
) -> dict:
    """Mesure l'impact des agents experts par marché (walk-forward multi-agents multi-symboles)."""
    from app.backtest.walkforward import validate_expert_agent

    symbols = _EXPERT_SYMBOLS.get(market, _EXPERT_SYMBOLS["crypto"])
    return await validate_expert_agent(market, symbols, timeframe=timeframe)


@router.post("/walk-forward")
async def walk_forward(
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    folds: int = 4,
    _user: User = Depends(require_feature("backtesting")),
) -> dict:
    """Validation out-of-sample : backteste `symbol` sur plusieurs périodes successives + verdict."""
    from app.backtest.walkforward import walk_forward as run_wf

    return await run_wf(symbol, timeframe, folds=max(2, min(folds, 6)))


# Univers évalué pour le track record (cryptos majeures, données réelles via Binance).
_TRACK_SYMBOLS = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT")


@router.get("/track-record")
async def track_record(
    refresh: bool = False,
    user: User = Depends(require_feature("backtesting")),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Track record HONNÊTE : validation walk-forward (out-of-sample) + perf réellement observée.

    - `validation` : robustesse de la stratégie sur l'historique réel, par symbole (mis en cache).
    - `observed` : performance des signaux réellement enregistrés dans le Journal (forward, limitée).
    Toujours accompagné de l'avertissement : les performances passées ne préjugent pas du futur.
    """
    from app.backtest.walkforward import walk_forward as run_wf
    from app.services import journal_service

    today = datetime.now(UTC).date().isoformat()
    cached = store.records.get("track_record", today)
    if cached and not refresh:
        validation = cached["validation"]
    else:
        validation = [await run_wf(sym, "1h", folds=4) for sym in _TRACK_SYMBOLS]
        store.records.put("track_record", today, {"date": today, "validation": validation})

    robust = sum(1 for v in validation if v["verdict"] == "robuste")
    observed = journal_service.stats(journal_service.recent_entries(store, user.tenant_id))
    return {
        "date": today,
        "validation": validation,
        "summary": {"symbols": len(validation), "robust": robust},
        "observed": observed,
        "disclaimer": (
            "Validation sur données historiques réelles. Les performances passées ne préjugent PAS "
            "des résultats futurs. Aide à la décision, pas un conseil en investissement."
        ),
    }
