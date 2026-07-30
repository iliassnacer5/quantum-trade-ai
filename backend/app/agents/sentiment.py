"""Agent Sentiment (M2).

Analyse une liste d'items de news (titre + score de sentiment éventuel) et produit un biais
de marché. Si les news n'ont pas de score, un lexique simple fournit un fallback déterministe.
En production : NLP/FinBERT ou LLM via LiteLLM pour le scoring fin.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.base import AgentOutput, apply_playbook

_BULLISH = {
    "surge", "rally", "soar", "gain", "bullish", "beat", "record", "upgrade",
    "adoption", "partnership", "approval", "inflow", "breakout", "all-time high",
}
_BEARISH = {
    "crash", "plunge", "drop", "fall", "bearish", "miss", "downgrade", "hack",
    "ban", "lawsuit", "outflow", "selloff", "fear", "liquidation", "dump",
}


@dataclass
class NewsItem:
    headline: str
    sentiment: float | None = None  # -1..+1 si déjà scoré


def _lexicon_score(headline: str) -> float:
    text = headline.lower()
    pos = sum(1 for w in _BULLISH if w in text)
    neg = sum(1 for w in _BEARISH if w in text)
    if pos == neg:
        return 0.0
    return (pos - neg) / (pos + neg)


async def run(news: list[NewsItem], fear_greed: int | None = None,
              context: dict | None = None) -> AgentOutput:
    name = "sentiment"
    if not news and fear_greed is None:
        return AgentOutput(name, 0.0, 0.1, "Pas de news disponibles.")

    from app.agents import llm
    import json

    scores = []
    
    if llm.available() and news:
        try:
            pending = [n for n in news if n.sentiment is None]
            if pending:
                numbered = "\n".join(f"{i + 1}. {n.headline}" for i, n in enumerate(pending))
                prompt = (
                    "Score le sentiment financier de CHAQUE titre d'actualité, de -1.0 (très baissier) "
                    f"à 1.0 (très haussier). Réponds UNIQUEMENT avec un tableau JSON de {len(pending)} "
                    "nombres, dans le même ordre, ex. [0.3,-0.5,0.0].\n"
                    f"Titres :\n{numbered}"
                )
                resp = await llm.complete(prompt, role="fast", max_tokens=300)
                import json as _json
                import re as _re
                match = _re.search(r"\[.*?\]", resp, _re.S)
                if match:
                    arr = _json.loads(match.group(0))
                    if isinstance(arr, list) and len(arr) == len(pending):
                        # Score PAR TITRE : chaque news garde son propre sentiment (consultable).
                        for n, v in zip(pending, arr):
                            try:
                                n.sentiment = max(-1.0, min(1.0, float(v)))
                            except (TypeError, ValueError):
                                pass
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Erreur LLM sentiment : %s", e)

    # Repli lexique PAR TITRE (assigné sur l'objet -> le score reste visible sur la prédiction).
    for n in news:
        if n.sentiment is None:
            n.sentiment = _lexicon_score(n.headline)
    scores = [n.sentiment for n in news]
    news_score = sum(scores) / len(scores) if scores else 0.0

    # Fear & Greed Index (0-100) -> contribue au biais (50 = neutre)
    fg_component = 0.0
    if fear_greed is not None:
        fg_component = (fear_greed - 50) / 50  # -1..+1

    if news and fear_greed is not None:
        score = 0.6 * news_score + 0.4 * fg_component
    elif news:
        score = news_score
    else:
        score = fg_component

    score = max(-1.0, min(1.0, score))
    confidence = min(1.0, 0.3 + 0.1 * len(news) + (0.2 if fear_greed is not None else 0))

    bias = "positif" if score > 0.1 else "négatif" if score < -0.1 else "neutre"
    parts = []
    if news:
        parts.append(f"Sentiment des news {bias} (n={len(news)})")
    if fear_greed is not None:
        fg_label = "avidité" if fear_greed > 55 else "peur" if fear_greed < 45 else "neutre"
        src = "Fear & Greed" if news else "Indice de marché (momentum)"
        parts.append(f"{src} {fear_greed}/100 ({fg_label})")
    if not parts:
        parts.append("aucune source de sentiment")

    # Cadre de la stratégie. Atténuation DOUCE : le sentiment ne lit pas le graphique, il lit ce que
    # le marché raconte. Quand il contredit la tendance, ce n'est pas une erreur de lecture mais une
    # information — souvent la première alerte d'un retournement. L'écraser comme un agent technique
    # reviendrait à supprimer le seul contre-pouvoir du système.
    metrics: dict = {"news_count": len(news), "fear_greed": fear_greed}
    notes: list[str] = []
    score, confidence = apply_playbook(score, confidence, notes, metrics, context, soften=0.7)
    rationale = "Analyse de sentiment : " + " ; ".join(parts + notes) + "."

    return AgentOutput(
        name=name,
        score=round(score, 3),
        confidence=round(confidence, 3),
        rationale=rationale,
        details=metrics,
    )
