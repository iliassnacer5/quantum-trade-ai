"""Agent expert OR (XAU) & métaux précieux — les moteurs qu'un trader or professionnel surveille.

L'or n'est ni une action ni une devise : c'est un actif SANS rendement dont le prix est piloté par
quatre forces macro, par-dessus la technique classique :

1. **Le DOLLAR (DXY) — corrélation inverse forte** : l'or est coté en USD ; dollar fort = or cher
   pour le reste du monde = pression baissière. C'est LE driver n°1 au quotidien.
2. **Les TAUX RÉELS (taux - inflation)** : l'or ne verse pas d'intérêts. Taux qui montent =
   coût d'opportunité = baissier. Taux qui baissent / inflation élevée = haussier (couverture).
3. **La PEUR (VIX / risk-off)** : valeur refuge — stress de marché = flux acheteurs vers l'or.
4. **Les NIVEAUX RONDS** ($2 400, $2 500…) : les ordres institutionnels et les options s'y
   agglutinent — supports/résistances psychologiques réels.

Réutilise `ta.analyze` (RSI, MACD, ADX, pivots, Fibonacci…) puis applique ces règles or.
Sortie : AgentOutput(name="technical", details={"expert": True, ...}) — compatible Master/Journal.
"""

from __future__ import annotations

import logging

from app.agents.base import AgentOutput, apply_playbook
from app.data import cross_asset
from app.domain import ta
from app.domain.indicators import Candle

logger = logging.getLogger(__name__)


def _round_level_bias(price: float) -> tuple[float, str | None]:
    """Proximité d'un niveau rond ($50 pour l'or) : zone de réaction institutionnelle."""
    if price <= 0:
        return 0.0, None
    step = 50.0 if price > 500 else 5.0  # or ~ pas de 50$, argent ~ pas de 5$
    nearest = round(price / step) * step
    dist_pct = abs(price - nearest) / price * 100
    if dist_pct < 0.15:
        return 0.0, f"prix collé au niveau rond {nearest:.0f}$ (zone de décision institutionnelle)"
    return 0.0, None


async def run(candles: list[Candle], symbol: str = "XAU/USD", context: dict | None = None) -> AgentOutput:
    name = "technical"
    if len(candles) < 35:
        return AgentOutput(name, 0.0, 0.1, "Données insuffisantes pour l'analyse technique.")

    a = ta.analyze(candles)
    score, conf, metrics = a["score"], a["confidence"], a["metrics"]
    notes = list(a["notes"])
    price = candles[-1].close

    # 1) DOLLAR (DXY) — corrélation inverse forte, poids supérieur au filtre forex.
    dxy = await cross_asset.get_dxy_signal()
    if dxy is not None and abs(dxy) > 0.05:
        score = 0.7 * score - 0.3 * max(-1.0, min(1.0, dxy))
        notes.append(f"Dollar (DXY {dxy:+.2f}) → pression {'baissière' if dxy > 0 else 'haussière'} sur l'or (corrélation inverse)")
        metrics["dxy"] = round(dxy, 3)

    # 2) TAUX RÉELS + INFLATION (le coût d'opportunité de détenir de l'or).
    macro = await cross_asset.get_macro_snapshot()
    rate_trend, inflation, vix = macro.get("rate_trend"), macro.get("inflation"), macro.get("vix")
    if rate_trend == "up":
        score -= 0.15
        notes.append("Taux en hausse → coût d'opportunité défavorable à l'or")
    elif rate_trend == "down":
        score += 0.15
        notes.append("Taux en baisse → favorable à l'or (actif sans rendement)")
    if inflation is not None and inflation > 3.0:
        score += 0.10
        notes.append(f"Inflation élevée ({inflation}%) → demande de couverture pro-or")
    if rate_trend is not None:
        metrics["rate_trend"] = rate_trend
    if inflation is not None:
        metrics["inflation"] = inflation

    # 3) VALEUR REFUGE (VIX) : le stress de marché nourrit l'or.
    if vix is not None:
        metrics["vix"] = vix
        if vix > 25:
            score += 0.15
            notes.append(f"VIX {vix} élevé → flux refuge vers l'or")
        elif vix < 14:
            score -= 0.05
            notes.append(f"VIX {vix} très bas → appétit pour le risque, or moins demandé")

    # 4) NIVEAUX RONDS institutionnels.
    _, round_note = _round_level_bias(price)
    if round_note:
        notes.append(round_note)

    # Cadre commun : la stratégie du desk (tendance de fond, niveaux majeurs, fenêtre de session).
    score, conf = apply_playbook(score, conf, notes, metrics, context)

    score = max(-1.0, min(1.0, score))
    metrics["expert"] = True
    rationale = "Analyse technique (expert OR) : " + " ; ".join(notes) + "."
    return AgentOutput(name=name, score=round(score, 3), confidence=round(conf, 3), rationale=rationale, details=metrics)
