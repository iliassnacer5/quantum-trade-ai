"""Agent expert ACTIONS — règles métier actions par-dessus l'analyse technique.

- Filtre macro SPX : en régime risk-off (S&P 500 sous sa MM20), on tempère/annule les biais LONG.
- Gap : un gap d'ouverture marqué tend à se combler (~70%) -> biais contrarien vers le comblement.
(Les fondamentaux sont gérés par l'agent `fundamental` séparé, déjà actif sur les actions.)
Sortie : AgentOutput(name="technical", details={"expert": True, ...}).
"""

from __future__ import annotations

import logging

from app.agents.base import AgentOutput, apply_playbook
from app.data import cross_asset
from app.domain import ta
from app.domain.indicators import Candle

logger = logging.getLogger(__name__)


async def run(candles: list[Candle], symbol: str = "AAPL", context: dict | None = None) -> AgentOutput:
    name = "technical"
    if len(candles) < 35:
        return AgentOutput(name, 0.0, 0.1, "Données insuffisantes pour l'analyse technique.")

    a = ta.analyze(candles)
    score, conf, metrics = a["score"], a["confidence"], a["metrics"]
    notes = list(a["notes"])

    # Filtre macro SPX : risk-off -> on annule le biais haussier (corrélation actions/indice).
    regime = await cross_asset.get_spx_regime()
    if regime == "risk_off" and score > 0:
        score *= 0.3
        notes.append("SPX en risk-off → biais haussier fortement tempéré")
    elif regime == "risk_on" and score > 0:
        score = min(1.0, score * 1.1)
        notes.append("SPX en risk-on → contexte favorable aux longs")
    metrics["spx_regime"] = regime

    # Gap de la dernière bougie : tend à se combler (contrarien).
    prev_close = candles[-2].close
    cur_open = candles[-1].open
    if prev_close > 0:
        gap = (cur_open - prev_close) / prev_close
        if abs(gap) > 0.003:  # > 0.3%
            score += -0.15 if gap > 0 else 0.15  # gap haussier -> biais vers comblement (baissier)
            notes.append(f"Gap {gap * 100:+.1f}% → biais contrarien vers comblement")
            metrics["gap_pct"] = round(gap * 100, 2)

    # Cadre commun : la stratégie du desk (tendance de fond, niveaux majeurs, fenêtre de session).
    score, conf = apply_playbook(score, conf, notes, metrics, context)

    score = max(-1.0, min(1.0, score))
    metrics["expert"] = True
    rationale = "Analyse technique (expert actions) : " + " ; ".join(notes) + "."
    return AgentOutput(name=name, score=round(score, 3), confidence=round(conf, 3), rationale=rationale, details=metrics)
