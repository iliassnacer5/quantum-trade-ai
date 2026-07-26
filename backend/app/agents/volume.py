"""Agent Volume (M2).

Analyse la pression d'achat/vente en utilisant les indicateurs de volume (OBV, VWAP).
Déterministe par défaut.
"""

from __future__ import annotations

from app.agents.base import AgentOutput, apply_playbook, enrich
from app.domain import indicators as ind
from app.domain.indicators import Candle


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


async def run(candles: list[Candle], context: dict | None = None) -> AgentOutput:
    name = "volume"
    if len(candles) < 20:
        return AgentOutput(name, 0.0, 0.1, "Données insuffisantes pour l'analyse de volume.")

    # Sans volume réel (ex. forex spot : volume=0), OBV/VWAP/divergence sont du bruit -> rester neutre.
    if sum(c.volume for c in candles) <= 0:
        return AgentOutput(name, 0.0, 0.15, "Pas de données de volume fiables (marché sans volume, ex. forex) — agent neutre.")

    price = candles[-1].close
    obv_line = ind.obv(candles)
    vwap_val = ind.vwap(candles)
    
    signals: list[float] = []
    notes: list[str] = []

    # Analyse OBV (tendance)
    if len(obv_line) >= 10:
        obv_current = obv_line[-1]
        obv_past = obv_line[-10]
        if obv_current > obv_past:
            signals.append(0.3)
            notes.append("OBV en hausse (pression acheteuse)")
        elif obv_current < obv_past:
            signals.append(-0.3)
            notes.append("OBV en baisse (pression vendeuse)")
            
    # Analyse VWAP : position ET TENDANCE (la stratégie demande explicitement la tendance VWAP).
    if vwap_val is not None:
        if price > vwap_val:
            signals.append(0.2)
            notes.append(f"Prix au-dessus du VWAP ({vwap_val:.2f})")
        else:
            signals.append(-0.2)
            notes.append(f"Prix en-dessous du VWAP ({vwap_val:.2f})")
    vwap_slope = ind.slope_pct(ind.rolling_vwap(candles, 20), 5)
    if vwap_slope is not None:
        if vwap_slope > 0.01:
            signals.append(0.25)
            notes.append(f"Tendance VWAP haussière ({vwap_slope:+.2f}%)")
        elif vwap_slope < -0.01:
            signals.append(-0.25)
            notes.append(f"Tendance VWAP baissière ({vwap_slope:+.2f}%)")
        else:
            notes.append("Tendance VWAP plate (pas de direction du flux)")

    # Divergence prix / OBV (simplifiée)
    if len(obv_line) >= 5:
        price_trend = price > candles[-5].close
        obv_trend = obv_line[-1] > obv_line[-5]
        if price_trend and not obv_trend:
            signals.append(-0.4)
            notes.append("Divergence baissière Prix/OBV")
        elif not price_trend and obv_trend:
            signals.append(0.4)
            notes.append("Divergence haussière Prix/OBV")

    # Pic de volume (climax) : volume > 2× la moyenne 20 = mouvement CONFIRMÉ par la participation.
    if len(candles) >= 21:
        avg_vol = sum(c.volume for c in candles[-21:-1]) / 20
        last = candles[-1]
        if avg_vol > 0 and last.volume > 2 * avg_vol:
            move_up = last.close > last.open
            signals.append(0.35 if move_up else -0.35)
            notes.append(
                f"Pic de volume ({last.volume / avg_vol:.1f}× la moyenne) confirmant le mouvement "
                f"{'haussier' if move_up else 'baissier'}"
            )

    # Ligne Accumulation/Distribution : les pros y lisent la pression réelle (achats sur les creux ?).
    ad_line = ind.acc_dist(candles)
    if len(ad_line) >= 10:
        ad_up = ad_line[-1] > ad_line[-10]
        signals.append(0.2 if ad_up else -0.2)
        notes.append(f"Ligne A/D en {'hausse (accumulation institutionnelle)' if ad_up else 'baisse (distribution)'}")

    score = _clamp(sum(signals) / max(len(signals), 1))
    confidence = min(1.0, 0.4 + 0.15 * len(signals))
    details = {"obv_current": obv_line[-1], "vwap": vwap_val, "vwap_slope_pct": vwap_slope,
               "relative_volume": ind.relative_volume(candles, 20)}
    # Cadre commun : la stratégie du desk (tendance de fond, niveaux majeurs, fenêtre de session).
    score, confidence = apply_playbook(score, confidence, notes, details, context)
    score = _clamp(score)
    rationale = "Analyse de volume : " + " ; ".join(notes) + "."

    # Enrichissement LLM optionnel (AJOUTÉ, jamais substitué)
    from app.agents import llm
    if llm.available() and notes:
        try:
            prompt = (
                f"Analyste volumes. Rédige UNE phrase complète de synthèse (français) sur la dynamique "
                f"des volumes, sans conseil : {', '.join(notes)}."
            )
            rationale = enrich(rationale, await llm.complete(prompt, role="fast", max_tokens=200))
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Erreur LLM volume : %s", e)

    return AgentOutput(
        name=name,
        score=round(score, 3),
        confidence=round(confidence, 3),
        rationale=rationale,
        details=details,
    )
