"""Agent expert FOREX — règles métier forex par-dessus l'analyse technique.

- Marché sans volume centralisé (l'agent volume est déjà neutre).
- Forex range naturellement -> on n'amplifie pas excessivement la tendance.
- Filtre DXY : pour les paires USD, aligner le biais avec le Dollar Index (DXY haussier ->
  baissier pour EUR/USD, GBP/USD… ; haussier pour USD/JPY, USD/CAD…).
Sortie : AgentOutput(name="technical", details={"expert": True, ...}).
"""

from __future__ import annotations

import logging

from app.agents.base import AgentOutput, apply_playbook
from app.data import cross_asset
from app.domain import ta
from app.domain.indicators import Candle

logger = logging.getLogger(__name__)

# Paires où le DOLLAR est la devise de COTATION (USD en 2e) : DXY↑ => paire↓.
_USD_QUOTE = {"EUR", "GBP", "AUD", "NZD"}
# Paires où le dollar est la devise de BASE (USD en 1er) : DXY↑ => paire↑.


async def run(candles: list[Candle], symbol: str = "EUR/USD", context: dict | None = None) -> AgentOutput:
    name = "technical"
    if len(candles) < 35:
        return AgentOutput(name, 0.0, 0.1, "Données insuffisantes pour l'analyse technique.")

    a = ta.analyze(candles)
    score, conf, metrics = a["score"], a["confidence"], a["metrics"]
    notes = list(a["notes"])

    # Filtre DXY pour les paires impliquant l'USD.
    parts = symbol.upper().replace("-", "/").split("/")
    if len(parts) == 2 and "USD" in parts:
        dxy = await cross_asset.get_dxy_signal()
        if dxy is not None and abs(dxy) > 0.1:
            base, quote = parts
            # Sens de l'impact du DXY sur la paire.
            usd_effect = -dxy if quote == "USD" else (dxy if base == "USD" else 0.0)
            if usd_effect != 0.0:
                score = 0.8 * score + 0.2 * max(-1.0, min(1.0, usd_effect))
                notes.append(f"Filtre DXY ({dxy:+.2f}) appliqué à la paire USD")
                metrics["dxy"] = round(dxy, 3)

    # Cadre commun : la stratégie du desk (tendance de fond, niveaux majeurs, fenêtre de session).
    score, conf = apply_playbook(score, conf, notes, metrics, context)

    score = max(-1.0, min(1.0, score))
    metrics["expert"] = True
    rationale = "Analyse technique (expert forex) : " + " ; ".join(notes) + "."
    return AgentOutput(name=name, score=round(score, 3), confidence=round(conf, 3), rationale=rationale, details=metrics)
