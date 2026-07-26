"""Types partagés pour les agents IA."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.signal import Direction


@dataclass
class AgentOutput:
    """Sortie normalisée d'un agent spécialisé.

    score : biais directionnel dans [-1, +1] (-1 = fortement baissier, +1 = fortement haussier)
    confidence : [0, 1] — confiance de l'agent dans son propre score
    rationale : justification en langage naturel
    """

    name: str
    score: float
    confidence: float
    rationale: str
    details: dict = field(default_factory=dict)

    def direction(self) -> Direction:
        if self.score > 0.15:
            return Direction.BUY
        if self.score < -0.15:
            return Direction.SELL
        return Direction.HOLD


def apply_playbook(
    score: float, confidence: float, notes: list[str], metrics: dict, context: dict | None,
) -> tuple[float, float]:
    """Aligne la sortie d'un agent expert sur LA STRATÉGIE du desk (playbook).

    Tous les agents raisonnent dans le même cadre professionnel :
    - on ne prend jamais un biais CONTRE la tendance de fond mensuelle/journalière (fortement atténué) ;
    - un biais aligné est renforcé ;
    - les niveaux MAJEURS et la fenêtre de session sont rappelés dans la justification ;
    - hors ouverture de Londres / New York / chevauchement, la confiance est réduite.

    Sans contexte playbook (ou données insuffisantes), la sortie est renvoyée inchangée.
    """
    pb = (context or {}).get("playbook")
    if not pb or pb.get("insufficient"):
        return score, confidence

    bias = pb.get("bias", 0)
    metrics["playbook_bias"] = bias
    metrics["playbook_direction"] = pb.get("direction")
    if bias != 0:
        sens = "haussière" if bias > 0 else "baissière"
        if score * bias < 0:
            score *= 0.35
            notes.append(f"Stratégie : biais contraire à la tendance de fond {sens} → fortement atténué")
        else:
            score = max(-1.0, min(1.0, 0.75 * score + 0.25 * bias))
            notes.append(f"Stratégie : aligné sur la tendance de fond {sens} (mensuel + journalier)")

    levels = pb.get("levels") or {}
    if levels.get("major_support") is not None or levels.get("major_resistance") is not None:
        metrics["major_support"] = levels.get("major_support")
        metrics["major_resistance"] = levels.get("major_resistance")
        notes.append(
            f"Niveaux majeurs : support {levels.get('major_support')} / "
            f"résistance {levels.get('major_resistance')}"
        )

    session = pb.get("session") or {}
    if session.get("quality") is not None:
        metrics["session_quality"] = session["quality"]
        metrics["session_label"] = session.get("label")
        confidence *= 0.80 + 0.20 * float(session["quality"])
        notes.append(f"Fenêtre de session : {session.get('label')}")

    if pb.get("ready"):
        notes.append(f"Déclencheur 15 min actif ({pb.get('trigger')})")
    elif pb.get("veto"):
        notes.append("Stratégie : conditions d'entrée non réunies (" + "; ".join(pb.get("reasons") or []) + ")")
    return score, max(0.05, min(1.0, confidence))


def enrich(deterministic: str, llm_text: str | None) -> str:
    """Ajoute un commentaire IA à l'analyse déterministe, SANS jamais la remplacer.

    Garde-fou anti-troncature : on n'ajoute le texte LLM que s'il est complet (se termine par une
    ponctuation de fin de phrase). Un fragment tronqué (modèle « thinking » à court de tokens) est
    ignoré → l'utilisateur voit toujours une analyse cohérente et complète.
    """
    if not llm_text:
        return deterministic
    text = llm_text.strip()
    if len(text) < 15 or text[-1] not in ".!?…":
        return deterministic
    return f"{deterministic} 💬 {text}"
