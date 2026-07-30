"""M3 — Signal Engine.

Orchestre TOUS les agents (Technique, Sentiment, Pattern, Fondamental, Macro, Risque), applique
l'arbitrage du Master Agent (pondération dynamique + apprentissage Journal), calcule les niveaux de
risque (déterministe) et produit une `SignalCard` consolidée, explicable et avec le détail des agents.
"""

from __future__ import annotations

from app.agents import fundamental, macro as macro_agent, pattern, risk_agent, sentiment, technical, volume
from app.agents import master
from app.agents.base import AgentOutput
from app.agents.sentiment import NewsItem
from app.domain import indicators as ind
from app.domain.indicators import Candle
from app.domain.playbook import PlaybookSetup
from app.domain.risk import RiskParams, compute_levels, compute_levels_from_prices
from app.models.signal import Direction, SignalCard, Timeframe


def _breakdown(outputs: list[AgentOutput]) -> list[dict]:
    """Détail COMPLET par agent : score, confiance, justification ET données structurées.

    Les `details` (figures détectées, Fear & Greed, funding, pénalité risque…) sont conservés pour
    que la prédiction soit consultable « en détail du détail ». Le technique est allégé (ses
    métriques complètes sont déjà dans card.metrics, inutile de les dupliquer)."""
    out = []
    for o in outputs:
        d = dict(getattr(o, "details", None) or {})
        if o.name == "technical":
            d = {k: d[k] for k in ("funding_rate", "btc_lead", "spx_regime", "gap_pct", "dxy", "expert") if k in d}
        elif o.name == "playbook":
            # La version complète (couches + checklist) est déjà dans card.metrics["playbook"].
            d = {k: d[k] for k in ("direction", "ready", "veto", "context_ok", "risk_reward",
                                   "reward_pips", "risk_pips", "pips_label", "trigger", "reasons",
                                   "insufficient", "expert") if k in d}
        out.append({"name": o.name, "score": o.score, "confidence": o.confidence,
                    "rationale": o.rationale, "details": d})
    return out


async def generate_signal(
    *,
    asset: str,
    candles: list[Candle],
    news: list[NewsItem] | None = None,
    fear_greed: int | None = None,
    risk: RiskParams,
    timeframe: Timeframe = Timeframe.H1,
    weights: dict[str, float] | None = None,
    ratios: dict | None = None,
    macro_data: dict | None = None,
    risk_context: dict | None = None,
    journal_multipliers: dict[str, float] | None = None,
    playbook_setup: PlaybookSetup | None = None,
    include_playbook: bool = True,
) -> SignalCard:
    """Produit une Signal Card à partir des données de marché, sentiment, fondamentaux et macro.

    `playbook_setup` : résultat de la stratégie du desk (mensuel+journalier -> 4h -> entrée 15 min).
    S'il est fourni, il pilote la décision (droit de veto) et fournit les niveaux d'entrée/SL/TP.
    Sinon il est calculé automatiquement à partir du symbole quand la stratégie est activée.

    `include_playbook=False` sort la stratégie du desk de l'analyse : l'agent playbook n'est ni
    exécuté ni compté dans le vote, et son droit de veto ne s'applique pas. C'est ce que demande
    l'ANALYSE QUOTIDIENNE (`services.market_opinion_service`) : un avis des agents qui ne soit pas
    la stratégie répétée autrement. Un second regard n'a d'intérêt que s'il peut dire autre chose —
    laisser le playbook opposer son veto ramènerait mécaniquement l'avis à celui de la stratégie.
    """
    news = news or []
    # Sans Fear & Greed externe, on dérive un indice de marché (momentum/volatilité) pour que
    # l'agent sentiment contribue au lieu de rester muet ("pas de news").
    if fear_greed is None:
        from app.domain import ta as _ta
        fear_greed = _ta.fear_greed_proxy(candles)

    # 0. PLAYBOOK — la stratégie du desk s'exécute EN PREMIER : son verdict (et ses niveaux)
    # cadrent tout le reste. Les autres agents reçoivent son contexte pour analyser dans le
    # même cadre professionnel (biais de fond, niveaux majeurs, fenêtre de session).
    from app.core.config import get_settings

    settings = get_settings()
    pb_output: AgentOutput | None = None
    if settings.playbook_enabled and include_playbook:
        try:
            if playbook_setup is None:
                from app.services import playbook_service

                playbook_setup = await playbook_service.build_setup(asset)
            from app.agents import playbook as playbook_agent

            pb_output = await playbook_agent.run(playbook_setup)
        except Exception as exc:  # noqa: BLE001 — jamais bloquant : on retombe sur les autres agents
            import logging

            logging.getLogger(__name__).warning("Playbook indisponible pour %s (%s)", asset, exc)
            playbook_setup = None
    elif not include_playbook:
        # Sortie COMPLÈTE de la stratégie : même un setup fourni par l'appelant est écarté, sinon
        # il ré-entrerait par le contexte des agents (`_ctx["playbook"]`) et par les niveaux
        # d'entrée — l'avis ne serait plus indépendant, il serait la stratégie déguisée.
        playbook_setup = None

    # 1. Agents — l'agent technique est routé vers l'expert du marché (contexte = classe d'actif).
    from app.data.markets import asset_class as _asset_class
    _ctx = {"market_type": _asset_class(asset), "symbol": asset}
    if playbook_setup is not None:
        _ctx["playbook"] = playbook_setup.as_dict()
    outputs: list[AgentOutput] = []
    if pb_output is not None:
        outputs.append(pb_output)
    # TOUS les agents reçoivent le contexte de la stratégie : la tendance validée, les zones et les
    # niveaux d'exécution sont la même lecture du marché pour tout le monde. Chacun décide ensuite
    # du poids qu'il accorde à un désaccord (cf. `apply_playbook(soften=...)`).
    outputs += [
        await technical.run(candles, symbol=asset, context=_ctx),
        await volume.run(candles, _ctx),
        await sentiment.run(news, fear_greed, context=_ctx),
        await pattern.run(candles, symbol=asset, context=_ctx),
    ]
    # L'agent fondamental n'a de sens que pour les ACTIONS (ou si des ratios sont fournis).
    # (Avant : condition inversée qui l'activait sur la crypto et l'omettait sur les actions.)
    from app.data.markets import asset_class as _asset_class
    if ratios is not None or _asset_class(asset) == "stock":
        outputs.append(await fundamental.run(asset, ratios, context=_ctx))
    if macro_data is not None:
        outputs.append(await macro_agent.run(macro_data, context=_ctx))

    risk_out = None
    if risk_context is not None:
        risk_out = risk_agent.run_sync(
            exposure_pct=risk_context.get("exposure_pct", 0.0),
            drawdown_pct=risk_context.get("drawdown_pct", 0.0),
            correlation=risk_context.get("correlation", 0.0),
            returns=risk_context.get("returns"),
        )
        outputs.append(risk_out)

    # 2. Arbitrage Master (pondération dynamique + apprentissage + autorité du playbook)
    decision = master.decide(
        outputs, weights=weights, journal_multipliers=journal_multipliers, risk_output=risk_out,
        playbook_gate=settings.playbook_veto and include_playbook,
    )

    entry = candles[-1].close
    atr_val = ind.atr(candles, 14) or (entry * 0.01)
    breakdown = _breakdown(outputs)
    # Tableau de bord des indicateurs : exposé depuis l'agent technique (détails = métriques ta).
    metrics = next((o.details for o in outputs if o.name == "technical"), {}) or {}
    # La PESÉE du Master (transparence totale) : poids effectifs par agent, score combiné, seuils.
    metrics["master_decision"] = {
        "score": decision.score,           # score combiné pondéré [-1..+1]
        "threshold": 0.12,                 # BUY si > +0.12 ; SELL si < -0.12 ; sinon HOLD
        "consensus": decision.consensus,
        "conflict": decision.conflict,
        "weights_used": decision.weights_used,  # poids effectif de chaque agent dans la décision
        "playbook_veto": decision.playbook_veto,
    }
    # La stratégie complète (4 étapes + checklist + niveaux majeurs + session) est exposée telle
    # quelle : le trader peut relire chaque étape de la décision.
    if playbook_setup is not None:
        metrics["playbook"] = playbook_setup.as_dict()
        metrics["session"] = playbook_setup.session

    if decision.direction == Direction.HOLD:
        return SignalCard(
            asset=asset,
            direction=Direction.HOLD,
            entry=round(entry, 8),
            stop_loss=round(entry, 8),
            take_profit_1=round(entry, 8),
            risk_reward=0.0,
            confidence=decision.confidence,
            timeframe=timeframe,
            rationale=decision.rationale,
            agents=breakdown,
            metrics=metrics,
            consensus_pct=decision.consensus,
        )

    # Niveaux : ceux du PLAYBOOK en priorité (stop derrière la structure 15 min, objectif
    # max(2×risque, 100 pips)) — ils ont un sens de marché, contrairement à un multiple d'ATR seul.
    use_pb = (
        playbook_setup is not None
        and playbook_setup.ready
        and playbook_setup.direction == decision.direction.value
        and playbook_setup.entry is not None
    )
    if use_pb:
        entry = playbook_setup.entry
        levels = compute_levels_from_prices(
            decision.direction, entry, playbook_setup.stop_loss,
            (playbook_setup.take_profit_1, playbook_setup.take_profit_2, playbook_setup.take_profit_3),
            risk,
        )
        metrics["levels_source"] = "playbook"
        metrics["target_pips"] = round(playbook_setup.reward_pips, 1)
        metrics["stop_pips"] = round(playbook_setup.risk_pips, 1)
        metrics["pips_label"] = playbook_setup.pips_label
    else:
        levels = compute_levels(decision.direction, entry, atr_val, risk)
        metrics["levels_source"] = "atr"

    return SignalCard(
        asset=asset,
        direction=decision.direction,
        entry=round(entry, 8),
        stop_loss=levels.stop_loss,
        take_profit_1=levels.take_profit_1,
        take_profit_2=levels.take_profit_2,
        take_profit_3=levels.take_profit_3,
        risk_reward=levels.risk_reward,
        confidence=decision.confidence,
        timeframe=timeframe,
        rationale=decision.rationale,
        position_size=levels.position_size,
        position_value=levels.position_value,
        risk_amount=levels.risk_amount,
        agents=breakdown,
        metrics=metrics,
        consensus_pct=decision.consensus,
    )
