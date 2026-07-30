"""Agent Macro (M2) — contexte global & régime de marché.

Détermine un régime (risk-on / risk-off / neutre) à partir d'indicateurs macro (taux, inflation,
indice de volatilité). Déterministe ; en production, grounding Google Search via Gemini 2.5 Pro
pour ancrer l'actualité géopolitique en temps réel.
"""

from __future__ import annotations

from app.agents.base import AgentOutput, apply_playbook


def assess_regime(macro: dict) -> tuple[str, float]:
    """Retourne (régime, biais[-1..1]). Biais > 0 = favorable aux actifs risqués."""
    score = 0.0
    rate_trend = macro.get("rate_trend")  # 'up' | 'down' | 'flat'
    if rate_trend == "down":
        score += 0.4
    elif rate_trend == "up":
        score -= 0.4

    inflation = macro.get("inflation")  # %
    if inflation is not None:
        if inflation > 5:
            score -= 0.3
        elif inflation < 2.5:
            score += 0.2

    vix = macro.get("vix")
    if vix is not None:
        # Régime risk-on/off gradué selon le VIX (volatilité implicite).
        if vix > 30:
            score -= 0.4   # panique -> risk-off fort
        elif vix > 20:
            score -= 0.15  # nervosité -> risk-off léger
        elif vix < 15:
            score += 0.3   # complaisance -> risk-on fort
        elif vix < 18:
            score += 0.1   # calme -> risk-on léger

    score = max(-1.0, min(1.0, score))
    regime = "risk-on" if score > 0.2 else "risk-off" if score < -0.2 else "neutre"
    return regime, score


async def run(macro: dict | None = None, context: dict | None = None) -> AgentOutput:
    name = "macro"
    if not macro:
        macro = {} # Avoid None for dictionary access
        
    regime, score = assess_regime(macro)
    confidence = 0.5
    rationale = f"Contexte macro : régime {regime} (taux={macro.get('rate_trend')}, " \
                f"inflation={macro.get('inflation')}%, VIX={macro.get('vix')})."

    # Enrichissement via Grounding LLM
    from app.agents import llm
    if llm.available():
        try:
            prompt = (
                "Fournis un résumé très concis (2 phrases) de la situation macro-économique mondiale actuelle "
                "et de son impact sur les marchés financiers. Précise le régime actuel (Risk-on ou Risk-off)."
            )
            # Utilise le rôle "grounding" qui est par défaut Gemini (qui a accès potentiellement au web via ses outils natifs)
            llm_rationale = await llm.complete(prompt, role="grounding", max_tokens=250)
            
            from app.agents.base import enrich
            # Optionnellement ajuster le score si le LLM est très explicite
            if "risk-on" in llm_rationale.lower() or "bullish" in llm_rationale.lower():
                score = min(1.0, score + 0.2)
            elif "risk-off" in llm_rationale.lower() or "bearish" in llm_rationale.lower():
                score = max(-1.0, score - 0.2)

            rationale = enrich(rationale, llm_rationale.strip())
            confidence = 0.7
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Erreur LLM Macro : %s", e)

    # Cadre de la stratégie, avec la même atténuation douce que le sentiment : la macro ne lit pas
    # le graphique. Son désaccord avec la tendance est une information à conserver, pas un bruit.
    metrics: dict = {"regime": regime}
    notes: list[str] = []
    score, confidence = apply_playbook(score, confidence, notes, metrics, context, soften=0.7)
    if notes:
        rationale += " " + " ; ".join(notes) + "."

    return AgentOutput(
        name=name,
        score=round(score, 3),
        confidence=round(confidence, 3),
        rationale=rationale,
        details=metrics,
    )
