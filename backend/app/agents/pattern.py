"""Agent Pattern (M2) — reconnaissance de figures chartistes.

Détection déterministe des figures chandeliers classiques (avalée, marteau, étoile filante, doji)
+ structure (plus hauts/bas). En production, l'option vision (Gemini 2.5 Pro) analyse une image du
graphe ; ici le cœur est déterministe et testable, l'appel vision est un enrichissement optionnel.

La détection elle-même vit dans `domain.price_action` : la stratégie playbook en a besoin pour sa
confluence d'entrée, et le domaine ne peut pas dépendre des agents. `detect_patterns` reste exposée
ici pour ne rien casser chez les appelants existants.
"""

from __future__ import annotations

from app.agents.base import AgentOutput, apply_playbook
from app.domain.indicators import Candle
from app.domain.price_action import detect_patterns

__all__ = ["detect_patterns", "run"]


async def run(candles: list[Candle], symbol: str = "Symbol", timeframe: str = "TF",
              context: dict | None = None) -> AgentOutput:
    name = "pattern"
    if len(candles) < 5:
        return AgentOutput(name, 0.0, 0.1, "Données insuffisantes pour l'analyse chartiste.")

    patterns = detect_patterns(candles)
    base_score = max(-1.0, min(1.0, sum(b for _, b in patterns) / len(patterns))) if patterns else 0.0
    base_confidence = min(1.0, 0.4 + 0.15 * len(patterns)) if patterns else 0.3
    labels = ", ".join(p for p, _ in patterns) if patterns else "Aucune figure"
    # Cadre de la stratégie : cet agent lit le PRIX, comme le playbook. Une figure qui contredit la
    # tendance validée sur quatre unités de temps est presque toujours un piège de correction, d'où
    # l'atténuation pleine (contrairement aux agents orthogonaux au prix).
    notes: list[str] = []
    metrics: dict = {"patterns": [p for p, _ in patterns]}
    base_score, base_confidence = apply_playbook(
        base_score, base_confidence, notes, metrics, context)
    rationale = f"Analyse chartiste (déterministe) : {labels}."
    if notes:
        rationale += " " + " ; ".join(notes) + "."

    # Enrichissement Vision LLM (Gemini)
    from app.agents import llm
    if llm.available() and len(candles) >= 10:
        try:
            from app.agents.chart_renderer import render_chart_base64
            img_b64 = render_chart_base64(candles, symbol=symbol, timeframe=timeframe)
            
            prompt = (
                "Analyse ce graphique en chandeliers japonais. "
                "1) Détecte les figures chartistes majeures (supports, résistances, triangles, têtes et épaules, etc.). "
                "2) Donne un biais de marché de -1.0 (très baissier) à 1.0 (très haussier) au format '[Biais: X.X]'. "
                "3) Donne une explication concise. Ne donne pas de conseil financier."
            )
            
            resp = await llm.complete_vision(prompt, img_b64, role="vision", max_tokens=300)
            
            # Extraction du biais si présent
            import re
            from app.agents.base import enrich
            m = re.search(r"\[Biais:\s*(-?\d+\.\d+)\]", resp)
            if m:
                llm_score = float(m.group(1))
                llm_score = max(-1.0, min(1.0, llm_score))
                # Pondération 50/50 entre déterministe et LLM si des figures déterministes existent
                score = (base_score + llm_score) / 2 if patterns else llm_score
                confidence = min(1.0, base_confidence + 0.2)
                # Le commentaire vision s'AJOUTE (sans la balise [Biais]), jamais ne remplace.
                rationale = enrich(rationale, re.sub(r"\[Biais:[^\]]*\]", "", resp).strip())
            else:
                score = base_score
                confidence = base_confidence
                rationale = enrich(rationale, resp.strip())

            return AgentOutput(
                name=name,
                score=round(score, 3),
                confidence=round(confidence, 3),
                rationale=rationale,
                details={**metrics, "vision_used": True},
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Erreur LLM Vision pattern : %s", e)

    return AgentOutput(
        name=name,
        score=round(base_score, 3),
        confidence=round(base_confidence, 3),
        rationale=rationale,
        details={**metrics, "vision_used": False},
    )

