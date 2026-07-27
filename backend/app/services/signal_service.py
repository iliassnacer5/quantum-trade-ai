"""Service de génération de signaux — la boucle de valeur du MVP.

Données (Binance ou synthétique) -> Agents (Technique + Sentiment) -> Master -> Signal Engine
-> persistance -> diffusion WebSocket -> alerte.
"""

from __future__ import annotations

import logging

from app.data import macro as macro_data_mod, markets, news as news_mod
from app.domain.indicators import Candle
from app.domain.risk import RiskParams
from app.models.entities import User
from app.models.signal import Timeframe
from app.models.signal import SignalCard
from app.realtime import bus
from app.repositories.store import AppStore
from app.services import journal_service, risk_service
from app.signal_engine.engine import generate_signal
from app.alerts.notifier import notify_signal

logger = logging.getLogger(__name__)

# Profil de risque -> % du capital risqué par trade
_RISK_PCT = {"conservative": 0.5, "moderate": 1.0, "aggressive": 2.0}

# Unité de temps demandée -> intervalle du connecteur de données.
_TF_INTERVAL = {
    Timeframe.M15: "15m",
    Timeframe.H1: "1h",
    Timeframe.H4: "4h",
    Timeframe.D1: "1d",
    Timeframe.W1: "1w",
    Timeframe.MN: "1M",
}


async def _load_candles(symbol: str, timeframe: Timeframe) -> list[Candle]:
    """Charge les bougies selon la classe d'actif (crypto/actions/forex), repli synthétique inclus."""
    return await markets.load_candles(symbol, interval=_TF_INTERVAL.get(timeframe, "1h"), limit=200)


_TF_TO_INTERVAL = {"15min": "15m", "1h": "1h", "4h": "4h", "1d": "1d", "1week": "1w", "1month": "1M"}


async def backtest_metrics(symbol: str, interval: str, limit: int = 500) -> dict | None:
    """Backtest déterministe de la paire et renvoie les KPI (ou None si indisponible)."""
    from datetime import UTC, datetime

    from app.backtest.engine import run_backtest
    from app.backtest.schemas import BacktestConfig
    from app.data.ohlcv import get_ohlcv
    from app.domain.indicators import Candle

    try:
        rows = await get_ohlcv(symbol, interval, limit=limit)
        candles = [
            Candle(r["open"], r["high"], r["low"], r["close"], r.get("volume", 0.0),
                   timestamp=datetime.fromtimestamp(r["time"], UTC))
            for r in rows
        ]
        if len(candles) < 100:
            return None
        cfg = BacktestConfig(symbol=symbol, timeframe=interval,
                             start_time=candles[0].timestamp, end_time=candles[-1].timestamp, initial_capital=10000)
        m = (await run_backtest(cfg, candles, tenant_id="bt")).metrics
        return {
            "trades": m.total_trades, "win_rate": round(m.win_rate * 100, 1),
            "profit_factor": m.profit_factor, "total_pnl_pct": m.total_pnl_pct,
            "max_drawdown_pct": m.max_drawdown_pct, "sharpe": m.sharpe_ratio,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Backtest %s échoué (%s)", symbol, exc)
        return None


def learning_multipliers(store: AppStore | None, tenant_id: str | None, market: str | None) -> dict[str, float]:
    """Poids d'apprentissage des agents = expérience VÉCUE × expertise ENTRAÎNÉE.

    Deux sources, multiplicatives et complémentaires :
    - le **Journal** : ce qui a réellement gagné ou perdu sur le compte de cet utilisateur ;
    - l'**entraînement quotidien** : la justesse mesurée de chaque agent quand la stratégie du desk
      est rejouée sur l'historique (cf. `training_service`). C'est ce qui fait des agents des
      experts DE CETTE stratégie plutôt que de bons généralistes.
    """
    from app.services import training_service

    base = dict(journal_service.compute_multipliers(store, tenant_id, market=market) or {}) \
        if (store is not None and tenant_id) else {}
    for agent, mult in training_service.agent_multipliers().items():
        base[agent] = round(base.get(agent, 1.0) * mult, 3)
    return base


async def daily_top_trades(count: int | None = None, *, now=None) -> dict:
    """LES trades du jour, produits par la STRATÉGIE du desk (playbook).

    Cascade appliquée à chaque symbole : Mensuel + Journalier (tendance de fond + niveaux majeurs)
    → Journalier détaillé (RSI14, MA20/MA50, volume, VWAP, divergences RSI/MACD, Fibonacci si
    correction) → 4 h (mêmes facteurs) → entrée 15 min UNIQUEMENT, avec R/R ≥ 1:2 et objectif
    ≥ 100 pips. L'univers scanné privilégie le chevauchement Londres/New York.

    Retourne au plus `count` setups, chacun étiqueté `ready` (exécutable) ou `armed` (contexte
    validé, en attente du déclencheur 15 min). Aucun remplissage artificiel : s'il n'y a pas
    5 setups conformes, on le dit.
    """
    from app.core.config import get_settings
    from app.services import playbook_service

    n = count or get_settings().daily_top_trades_count
    return await playbook_service.top_trades(n, now=now)


async def daily_picks(
    per_market: int = 3,
    classes: tuple[str, ...] = ("crypto", "forex", "stock", "commodity"),
    timeframe: str = "1h",
) -> list[dict]:
    """Sélection quotidienne GRADUÉE par marché — toujours utile sans jamais mentir.

    Deux niveaux de fiabilité :
    - ``confirmed`` : ★ haute-conviction (ADX>25) ET backtest fiable (réussite>55 %, PF>1,3, ≥5 trades).
    - ``watch``     : meilleurs setups directionnels du moment, à surveiller mais NON confirmés.

    On privilégie les ``confirmed`` ; s'il n'y en a pas assez, on complète avec des ``watch`` clairement
    étiquetés. Mieux vaut proposer les meilleures opportunités honnêtement qualifiées qu'une page vide.
    """
    out: list[dict] = []
    for cls in classes:
        scanned = await scan_market(asset_class=cls, timeframe=timeframe, limit=12)
        directional = [c for c in scanned if c["direction"] != "HOLD"]
        confirmed: list[dict] = []
        watch: list[dict] = []
        for cand in directional[:8]:  # borne les backtests (coût)
            bt = await backtest_metrics(cand["symbol"], timeframe)
            reliable = bool(bt and bt["trades"] >= 5 and bt["win_rate"] > 55 and bt["profit_factor"] > 1.3)
            item = {
                "symbol": cand["symbol"], "asset_class": cls, "direction": cand["direction"],
                "price": cand["price"], "adx": cand["adx"], "rsi": cand["rsi"],
                "trend": cand["trend"], "conviction": cand["conviction"], "backtest": bt,
                "high_conviction": cand["high_conviction"], "reliable": reliable,
                "tier": "confirmed" if (cand["high_conviction"] and reliable) else "watch",
                "timeframe": timeframe,
            }
            (confirmed if item["tier"] == "confirmed" else watch).append(item)
        picks = confirmed[:per_market]
        if len(picks) < per_market:
            picks += watch[: per_market - len(picks)]
        out.extend(picks)
    return out


async def verify_signal(
    symbol: str, timeframe: str, *,
    confidence: int = 0, consensus_pct: int = 0, risk_reward: float = 0.0,
    mtf_aligned: int = 0, mtf_total: int = 0, adx: float | None = None, direction: str = "HOLD",
) -> dict:
    """Vérifie la fiabilité d'un signal : backtest de la paire + checklist du trader.

    Combine une validation quantitative (backtest historique) et les critères de qualité du signal
    (confiance, consensus, multi-timeframe, R/R, force de tendance) en un verdict ✅/⚠️/🔴.
    """
    interval = _TF_TO_INTERVAL.get(timeframe, timeframe if "m" in timeframe or "h" in timeframe or "d" in timeframe else "1h")
    bt = await backtest_metrics(symbol, interval)

    checks: list[dict] = []
    if bt and bt["trades"] >= 5:
        checks.append({"label": "Backtest : taux de réussite > 55 %", "pass": bt["win_rate"] > 55, "value": f"{bt['win_rate']}%"})
        checks.append({"label": "Backtest : profit factor > 1,3", "pass": bt["profit_factor"] > 1.3, "value": bt["profit_factor"]})
    checks.append({"label": "Confiance ≥ 70 %", "pass": confidence >= 70, "value": f"{confidence}%"})
    checks.append({"label": "Consensus ≥ 70 %", "pass": consensus_pct >= 70, "value": f"{consensus_pct}%"})
    if mtf_total:
        checks.append({"label": "Multi-timeframe ≥ 2/3 alignés", "pass": mtf_aligned >= 2, "value": f"{mtf_aligned}/{mtf_total}"})
    # Bande de la stratégie : R/R entre 1,2 et 1,3 pour la prise de décision.
    checks.append({"label": "R/R ≥ 1:1,2", "pass": risk_reward >= 1.2, "value": f"1 : {risk_reward}"})
    checks.append({"label": "Tendance forte (ADX > 25)", "pass": (adx or 0) > 25, "value": round(adx, 1) if adx else "—"})

    passed = sum(1 for c in checks if c["pass"])
    total = len(checks)
    if direction == "HOLD":
        verdict = "skip"
    elif passed >= total - 1:
        verdict = "strong"
    elif passed >= total / 2:
        verdict = "moderate"
    else:
        verdict = "weak"

    # Interprétation HONNÊTE et non alarmante (les chiffres bruts de backtest font peur sans contexte).
    interpretation = {
        "strong": "✅ Setup solide : critères réunis et backtest favorable. Reste prudent, rien n'est garanti.",
        "moderate": "⚠️ Setup moyen : une partie des critères seulement. À ne prendre qu'avec une gestion stricte du risque.",
        "weak": ("ℹ️ Edge non prouvé sur l'historique — c'est NORMAL : presque aucune stratégie ne franchit "
                 "cette barre (55 % de réussite, PF>1,3) APRÈS frais. Ce n'est pas un bug, c'est l'honnêteté. "
                 "Ne traite pas ce signal à l'aveugle ; entraîne-toi en paper et juge sur la durée."),
        "skip": "⏸️ Pas de trade : signal neutre ou filtré. S'abstenir est une décision valable.",
    }.get(verdict, "")
    return {"verdict": verdict, "passed": passed, "total": total, "checks": checks,
            "backtest": bt, "interpretation": interpretation}


def finalize_decision(card, mtf_res: dict, blackout: tuple[bool, str] | None = None,
                      mode: str = "strict") -> "SignalCard":
    """Applique la décision FINALE — identique partout (analyse détaillée ET scanner).

    0) Gate ÉVÉNEMENTIEL : blackout news/earnings/FOMC -> HOLD (avant tout, pas de trade).
    1) Gate multi-timeframe : BUY/SELL seulement si ≥2/3 unités alignées, sinon HOLD.
    2) Filtre de qualité : confiance + ADX + R/R (cf. quality.is_tradeable), sinon HOLD.
    3) Flag ★ haute-conviction.
    Source unique de vérité -> le scanner et l'analyse ne peuvent plus diverger."""
    from app.core.config import get_settings
    from app.models.signal import Direction
    from app.signal_engine import quality

    card.mtf = mtf_res

    def _to_hold(reason: str) -> None:
        # Mémorise le trade BLOQUÉ (direction + niveaux d'origine) -> permet de mesurer après coup
        # ce que les filtres t'ont évité (rejeu du prix : le trade bloqué aurait-il perdu ?).
        if card.direction != Direction.HOLD:
            card.metrics["blocked_direction"] = card.direction.value
            card.metrics["blocked_entry"] = card.entry
            card.metrics["blocked_sl"] = card.stop_loss
            card.metrics["blocked_tp"] = card.take_profit_1
        card.direction = Direction.HOLD
        card.stop_loss = card.take_profit_1 = card.entry
        card.take_profit_2 = card.take_profit_3 = None
        card.risk_reward = 0.0
        card.position_size = card.position_value = card.risk_amount = None
        card.confidence = min(card.confidence, 45)
        card.rationale = reason + "\n" + card.rationale

    # 0) Gate événementiel (en tête) : on ne trade pas dans une fenêtre à fort impact.
    if blackout and blackout[0]:
        _to_hold(f"⏸️ Blackout événementiel : {blackout[1]} — pas de trade. [EVENT_LOCK]")
        card.high_conviction = False
        return card

    th = quality.thresholds(mode)
    # 1) Gate multi-timeframe (seuil selon le mode : strict/équilibré = 2/3, agressif = 1/3)
    if card.direction != Direction.HOLD and mtf_res.get("total", 0) >= 2 and mtf_res.get("aligned", 0) < th["mtf_min"]:
        _to_hold("⏸️ Non confirmé par le multi-timeframe (unités de temps divergentes) — pas de trade.")
    # 2) Filtre de qualité (confiance + ADX + R/R, seuils du mode)
    if get_settings().entry_quality_gate and card.direction != Direction.HOLD and not quality.is_tradeable(card, mode=mode):
        _to_hold(f"⏸️ Setup filtré (qualité insuffisante : {quality.rejection_reason(card, mode=mode)}) — pas de trade.")
    card.metrics["signal_mode"] = mode

    adx = card.metrics.get("adx")
    pb = card.metrics.get("playbook") or {}
    session = card.metrics.get("session") or {}
    # ★ Haute conviction : soit la voie classique (ADX + consensus + multi-TF), soit — et c'est la
    # voie privilégiée — un setup de PLAYBOOK complet (4 étapes validées, déclencheur 15 min actif)
    # pris dans une fenêtre à forte valeur (ouverture Londres/NY ou chevauchement).
    card.high_conviction = bool(
        card.direction.value != "HOLD"
        and (
            (adx and adx > 25 and card.consensus_pct >= 70 and mtf_res.get("aligned", 0) >= 2)
            or (pb.get("ready") and pb.get("direction") == card.direction.value and session.get("prime"))
        )
    )
    # Scores explicatifs (contexte marché + timing) — informatifs, non bloquants.
    card.metrics["context_score"] = quality.context_score(card)
    card.metrics["timing_score"] = quality.timing_score(card)
    return card


async def scan_market(
    asset_class: str | None = None,
    timeframe: str = "1h",
    limit: int = 20,
    high_conviction_only: bool = False,
    symbols: list[dict] | None = None,
    confirm_mtf: bool = False,
    user: User | None = None,
    store: AppStore | None = None,
) -> list[dict]:
    """Scanne un marché et classe les symboles par conviction (rapide : analyse technique).

    Retourne TOUS les symboles analysés, triés par score de conviction, avec un flag
    `high_conviction`. Si `confirm_mtf` est vrai, le flag ★ n'est conservé que si le
    multi-timeframe CONFIRME la direction (≥2/3 unités alignées) — comme l'analyse détaillée,
    pour que le scanner ne contredise pas la décision finale.

    `symbols` : univers explicite (ex. paires d'une session mondiale) ; sinon le catalogue.
    """
    from app.data import symbols as symbols_catalog
    from app.domain import ta
    from app.models.signal import Direction

    universe = symbols[:limit] if symbols else symbols_catalog.search(asset_class=asset_class, limit=limit)

    # 1re passe (rapide) : analyse technique seule pour classer tout l'univers.
    results: list[dict] = []
    candle_cache: dict[str, list] = {}
    for item in universe:
        sym = item["symbol"]
        try:
            candles = await markets.load_candles(sym, interval=timeframe, limit=200)
            if len(candles) < 60:
                continue
            candle_cache[sym] = candles
            a = ta.analyze(candles)
            m = a["metrics"]
            adx = m.get("adx", 0) or 0
            direction = Direction.BUY if a["score"] > 0.12 else Direction.SELL if a["score"] < -0.12 else Direction.HOLD
            high_conv = direction != Direction.HOLD and ta.is_high_conviction(a["score"], adx, item["asset_class"])
            results.append({
                "symbol": sym, "asset_class": item["asset_class"], "direction": direction.value,
                "score": a["score"], "adx": round(adx, 1), "adx_state": m.get("adx_state"),
                "trend": m.get("trend"), "rsi": m.get("rsi"), "price": m.get("price"),
                "conviction": round(abs(a["score"]) * (1 + adx / 50), 3),
                "high_conviction": high_conv, "mtf_aligned": None, "mtf_total": None,
                "consolidated": False,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("Scan %s échoué (%s)", sym, exc)

    # 2e passe (cohérence) : pour les meilleurs candidats ★, on exécute EXACTEMENT la même décision
    # que l'analyse détaillée (mêmes agents, news, macro, LLM, gates) via finalize_decision, pour un
    # utilisateur neutre. Borné à _CONSOLIDATE_TOP pour le coût. -> scanner == analyse (compte neutre).
    if confirm_mtf:
        raw_hc = [r for r in results if r["high_conviction"]]
        raw_hc.sort(key=lambda r: r["conviction"], reverse=True)
        macro_ctx = await macro_data_mod.fetch_macro_data()
        # Contexte de l'utilisateur (exposition réelle + apprentissage) -> scanner == SON analyse.
        if user is not None and store is not None:
            ctx = {"exposure_pct": risk_service.real_exposure_pct(user, store), "drawdown_pct": 0.0}
            tenant = user.tenant_id
            capital = user.capital
            rpt = _RISK_PCT.get(user.risk_profile, 1.0)
            scan_mode = (store.records.get("signal_mode", tenant) or {}).get("mode", "strict")
        else:
            ctx, tenant, capital, rpt = {"exposure_pct": 0.0, "drawdown_pct": 0.0}, None, 10000.0, 1.0
            scan_mode = "strict"
        consolidated_syms = {r["symbol"] for r in raw_hc[:_CONSOLIDATE_TOP]}
        for row in results:
            if row["symbol"] in consolidated_syms:
                # Apprentissage PAR MARCHÉ (comme l'analyse) -> cohérence préservée.
                jmult = learning_multipliers(store, tenant, row["asset_class"])
                await _consolidate_row(row, candle_cache[row["symbol"]], macro_ctx, ctx, jmult, capital, rpt, scan_mode)
            else:
                # Non consolidé : lead technique seul -> on n'affirme NI ★ NI verdict (à analyser).
                row["high_conviction"] = False
                row["consolidated"] = False

    # 3e passe : LA STRATÉGIE DU DESK a le dernier mot, pour TOUTES les lignes.
    # Le scanner ne peut pas afficher « BUY » sur une paire que le playbook refuse : c'est la même
    # méthode qui doit gouverner le scanner, les trades du jour et l'analyse détaillée.
    _apply_playbook_verdicts(results)

    if high_conviction_only:
        results = [r for r in results if r["high_conviction"]]
    results.sort(key=lambda r: (r["high_conviction"], r.get("playbook_edge", 0.0), r["conviction"]),
                 reverse=True)
    return results


def _apply_playbook_verdicts(rows: list[dict]) -> None:
    """Superpose à chaque ligne le verdict de la stratégie, calculé par la boucle de fond.

    Aucun calcul supplémentaire : on lit l'instantané que la boucle produit déjà pour les trades du
    jour. Conséquence directe : un symbole ne peut plus être noté différemment selon la page.
    """
    from app.services import live_snapshot

    verdicts = live_snapshot.verdicts()
    if not verdicts:
        return
    for row in rows:
        v = verdicts.get(row["symbol"])
        if not v:
            row["playbook_tier"] = "non balayé"
            continue
        row["playbook_tier"] = v["tier"]
        row["playbook_direction"] = v["direction"]
        row["playbook_reason"] = v["reason"]
        row["playbook_edge"] = v.get("edge_score", 0.0)
        row["reliability_score"] = v.get("reliability_score") or v.get("context_reliability") or 0
        # La stratégie refuse le trade, ou le veut dans l'autre sens -> le scanner n'affirme rien.
        if v["tier"] not in ("ready", "armed") or (
            v["direction"] in ("BUY", "SELL") and row["direction"] != v["direction"]
        ):
            row["direction"] = "HOLD"
            row["high_conviction"] = False


_CONSOLIDATE_TOP = 8  # nb de meilleurs candidats évalués À L'IDENTIQUE de l'analyse (LLM inclus)


async def _consolidate_row(
    row: dict, candles: list, macro_ctx: dict,
    risk_context: dict, journal_mult: dict | None, capital: float, risk_pct: float,
    mode: str = "strict",
) -> None:
    """Recalcule la décision FINALE d'un candidat EXACTEMENT comme l'analyse (mêmes agents + LLM + gates
    + MÊME contexte utilisateur : exposition + apprentissage). -> identique à « Analyser ce symbole »."""
    from app.data import fundamentals
    from app.domain.risk import RiskParams
    from app.models.signal import Timeframe
    from app.signal_engine import mtf
    from app.signal_engine.engine import generate_signal

    sym = row["symbol"]
    news = await news_mod.fetch_news(sym)
    ratios = await fundamentals.fetch_ratios(sym) if row["asset_class"] == "stock" else None
    card = await generate_signal(
        asset=sym, candles=candles, news=news,
        risk=RiskParams(capital=capital, risk_per_trade_pct=risk_pct),
        timeframe=Timeframe.H1, ratios=ratios, macro_data=macro_ctx,
        risk_context=risk_context, journal_multipliers=journal_mult,
    )
    from app.data import economic_calendar
    mtf_res = await mtf.confirm(sym, card.direction)
    blackout = await economic_calendar.is_news_blackout(sym, row["asset_class"])
    finalize_decision(card, mtf_res, blackout, mode=mode)
    row["direction"] = card.direction.value
    row["consensus"] = card.consensus_pct
    row["mtf_aligned"] = mtf_res["aligned"]
    row["mtf_total"] = mtf_res["total"]
    row["high_conviction"] = card.high_conviction
    row["consolidated"] = True


async def generate_for_user(
    user: User,
    store: AppStore,
    *,
    asset: str,
    timeframe: Timeframe = Timeframe.H1,
    notify: bool = True,
) -> SignalCard:
    """Génère, persiste, diffuse et notifie un signal pour un utilisateur."""
    candles = await _load_candles(asset, timeframe)
    news_items = await news_mod.fetch_news(asset)
    macro_ctx = await macro_data_mod.fetch_macro_data()
    # Ratios fondamentaux pour les ACTIONS (Finnhub) -> alimente l'agent fondamental.
    ratios = None
    if markets.asset_class(asset) == "stock":
        from app.data import fundamentals
        ratios = await fundamentals.fetch_ratios(asset)

    risk = RiskParams(
        capital=user.capital,
        risk_per_trade_pct=_RISK_PCT.get(user.risk_profile, 1.0),
    )

    # Contexte de risque courant (exposition) + apprentissage Journal (poids dynamiques).
    # L'agent risque se base sur l'exposition RÉELLE (ordres exécutés), pas sur l'accumulation des
    # analyses générées — sinon générer des signaux pénaliserait injustement la confiance.
    risk_context = {"exposure_pct": risk_service.real_exposure_pct(user, store), "drawdown_pct": 0.0}
    journal_mult = learning_multipliers(store, user.tenant_id, markets.asset_class(asset))

    card = await generate_signal(
        asset=asset,
        candles=candles,
        news=news_items,
        risk=risk,
        timeframe=timeframe,
        ratios=ratios,
        macro_data=macro_ctx,
        risk_context=risk_context,
        journal_multipliers=journal_mult,
    )

    # Avertissement de risque non bloquant (exposition simulée) sur la carte.
    warn = risk_service.generation_warning(user, store)
    if warn:
        card.risk_warning = warn if not card.risk_warning else f"{card.risk_warning} {warn}"

    # Confirmation multi-timeframe (1h/4h/1j) puis décision finale (blackout + gate MTF + qualité + ★).
    # Logique PARTAGÉE avec le scanner (finalize_decision) -> aucune divergence possible.
    from app.data import economic_calendar
    from app.signal_engine import mtf
    mtf_res = await mtf.confirm(asset, card.direction)
    blackout = await economic_calendar.is_news_blackout(asset, markets.asset_class(asset))
    mode = (store.records.get("signal_mode", user.tenant_id) or {}).get("mode", "strict")
    finalize_decision(card, mtf_res, blackout, mode=mode)

    # News utilisées : consultables sur la page de la prédiction (transparence totale).
    card.news = [
        {"headline": n.headline, "sentiment": n.sentiment} for n in (news_items or [])[:6]
    ]

    # Persistance (isolée par tenant)
    payload = card.model_dump(mode="json")
    stored = store.signals.add(user.tenant_id, payload)
    payload["id"] = stored.id
    card.id = stored.id  # la réponse API porte l'id -> lien direct vers la page de la prédiction

    # Observabilité (Phase 5)
    from app.core import metrics
    metrics.inc("signals_generated_total", direction=card.direction.value if hasattr(card.direction, "value") else str(card.direction))

    # Boucle d'apprentissage : enregistre l'issue (open) + scores d'agents pour le Journal.
    journal_service.record_signal(store, user.tenant_id, card, stored.id)

    # Copy-trading (Phase 4) : si l'utilisateur est un trader public, réplique vers ses suiveurs.
    try:
        from app.services import copytrading_service
        await copytrading_service.on_leader_signal(store, user.tenant_id, card)
    except Exception as exc:  # noqa: BLE001 — la copie ne doit jamais casser la génération
        logger.warning("Copy-trading fan-out échoué (%s)", exc)

    # Diffusion temps réel (Redis pub/sub si actif, sinon hub mémoire) + alerte
    await bus.publish(user.tenant_id, {"type": "signal", "data": payload})
    if notify:
        await notify_signal(
            card,
            email=user.email if user.alert_email else None,
            telegram_chat_id=user.telegram_chat_id if user.alert_telegram else None,
            webhook_url=user.webhook_url if user.alert_webhook else None,
            sms_to=user.phone if user.alert_sms else None,
            push_token=user.push_token,
        )

    return card
