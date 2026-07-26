"""Agent Technique (M2).

Calcule une analyse technique experte complète (RSI, MACD, ADX, Stochastique, EMA, Bollinger, ATR,
OBV, VWAP, supports/résistances) via `domain.ta`, en dérive un score directionnel décisif, et expose
toutes les métriques. Le LLM ne sert qu'à AJOUTER un commentaire (jamais à remplacer l'analyse).
"""

from __future__ import annotations

import logging

from app.agents.base import AgentOutput, apply_playbook, enrich
from app.domain import ta
from app.domain.indicators import Candle

logger = logging.getLogger(__name__)


async def run(candles: list[Candle], symbol: str | None = None, context: dict | None = None) -> AgentOutput:
    """Routeur : aiguille vers l'expert du marché (crypto pour l'instant), sinon analyse générique.

    Rétrocompatible : appelé sans `symbol`/`context` -> comportement générique inchangé (tests OK).
    """
    from app.core.config import get_settings

    if get_settings().expert_agents_enabled and (symbol or context):
        from app.data import markets

        market = (context or {}).get("market_type") or markets.asset_class(symbol or "")
        if market == "crypto":
            from app.agents import crypto_expert
            return await crypto_expert.run(candles, symbol=symbol or "BTC/USDT", context=context)
        if market == "forex":
            from app.agents import forex_expert
            return await forex_expert.run(candles, symbol=symbol or "EUR/USD", context=context)
        if market == "stock":
            from app.agents import stocks_expert
            return await stocks_expert.run(candles, symbol=symbol or "AAPL", context=context)
        if market == "commodity":
            from app.agents import gold_expert
            return await gold_expert.run(candles, symbol=symbol or "XAU/USD", context=context)
    return await _generic(candles, context)


async def _generic(candles: list[Candle], context: dict | None = None) -> AgentOutput:
    name = "technical"
    if len(candles) < 35:
        return AgentOutput(name, 0.0, 0.1, "Données insuffisantes pour l'analyse technique.")

    a = ta.analyze(candles)
    notes = list(a["notes"])
    metrics = a["metrics"]
    # Cadre commun : la stratégie du desk (tendance de fond, niveaux majeurs, fenêtre de session).
    score, conf = apply_playbook(a["score"], a["confidence"], notes, metrics, context)
    a = {**a, "score": round(max(-1.0, min(1.0, score)), 3), "confidence": round(conf, 3),
         "notes": notes, "metrics": metrics}
    rationale = "Analyse technique : " + " ; ".join(a["notes"]) + "."

    from app.agents import llm
    if llm.available():
        try:
            prompt = (
                "Tu es analyste technique. À partir de ces signaux, rédige UNE phrase de synthèse "
                "claire et complète (français), sans conseil d'investissement :\n"
                + " ; ".join(a["notes"])
            )
            rationale = enrich(rationale, await llm.complete(prompt, role="fast", max_tokens=220))
        except Exception as e:  # noqa: BLE001
            logger.warning("Erreur LLM technical : %s", e)

    return AgentOutput(
        name=name,
        score=a["score"],
        confidence=a["confidence"],
        rationale=rationale,
        details=a["metrics"],
    )
