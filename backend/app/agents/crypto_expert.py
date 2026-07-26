"""Agent expert CRYPTO — applique les règles métier crypto par-dessus l'analyse technique.

Réutilise `domain.ta.analyze` (indicateurs partagés) puis ajuste le score selon les spécificités
crypto (marché 24/7) :
- RSI conditionnel : en tendance forte (ADX>25), on suit la tendance et on neutralise le biais
  mean-reversion du RSI (anti-whipsaw).
- Funding rate contrarien : un funding très positif = marché long surchargé (biais baissier).
- BTC lead : un altcoin ne suit pas un signal opposé à BTC quand BTC bouge fort (confiance réduite).

Sortie : AgentOutput(name="technical", ...) — même nom que l'agent transversal pour ne pas casser
le Master ni le Journal d'apprentissage.
"""

from __future__ import annotations

import logging

from app.agents.base import AgentOutput, apply_playbook
from app.data import cross_asset
from app.domain import ta
from app.domain.indicators import Candle

logger = logging.getLogger(__name__)


async def run(candles: list[Candle], symbol: str = "BTC/USDT", context: dict | None = None) -> AgentOutput:
    name = "technical"
    if len(candles) < 35:
        return AgentOutput(name, 0.0, 0.1, "Données insuffisantes pour l'analyse technique.")

    a = ta.analyze(candles)
    score, conf, metrics = a["score"], a["confidence"], a["metrics"]
    notes = list(a["notes"])

    adx = metrics.get("adx", 0) or 0
    trend = metrics.get("trend", "")
    trend_sign = 1 if "hauss" in trend else -1 if "baiss" in trend else 0

    # 1) RSI conditionnel : en tendance forte, accentuer la tendance (le RSI mean-reversion ment).
    if adx > 25 and trend_sign != 0:
        score = 0.7 * score + 0.3 * trend_sign * min(1.0, adx / 40)
        notes.append("Crypto : tendance forte (ADX>25) → priorité tendance, RSI mean-reversion atténué")

    # 2) Funding rate contrarien (repli gracieux).
    funding = await cross_asset.get_funding_rates(symbol)
    if funding is not None:
        if funding > 0.001:        # > +0.1%
            score -= 0.15
            notes.append(f"Funding {funding * 100:.3f}% élevé → contrarien baissier")
        elif funding < -0.0005:    # < -0.05%
            score += 0.10
            notes.append(f"Funding {funding * 100:.3f}% négatif → contrarien haussier")
        metrics["funding_rate"] = funding

    # 3) BTC lead pour les altcoins.
    base = symbol.split("/")[0].split("-")[0].upper()
    if base != "BTC":
        btc = await cross_asset.get_btc_lead()
        if btc is not None and abs(btc) > 0.3 and score != 0 and (score > 0) != (btc > 0):
            conf *= 0.7
            notes.append(f"BTC lead opposé ({btc:+.2f}) → confiance réduite")
            metrics["btc_lead"] = round(btc, 3)

    # Cadre commun : la stratégie du desk (tendance de fond, niveaux majeurs, fenêtre de session).
    score, conf = apply_playbook(score, conf, notes, metrics, context)

    score = max(-1.0, min(1.0, score))
    metrics["expert"] = True
    rationale = "Analyse technique (expert crypto) : " + " ; ".join(notes) + "."
    return AgentOutput(name=name, score=round(score, 3), confidence=round(conf, 3), rationale=rationale, details=metrics)
