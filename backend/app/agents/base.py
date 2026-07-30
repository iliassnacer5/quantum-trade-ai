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
    *, soften: float = 0.35,
) -> tuple[float, float]:
    """Aligne la sortie d'un agent sur LA STRATÉGIE du desk (playbook).

    Tous les agents raisonnent dans le même cadre professionnel :
    - on ne prend jamais un biais CONTRE la tendance validée par le moteur multi-indicateurs
      (fortement atténué) ; un biais aligné est renforcé ;
    - le score de confiance de la tendance, les zones d'offre/demande et les supports/résistances
      classés sont rappelés dans la justification — c'est la MÊME lecture du marché pour tous ;
    - les niveaux MAJEURS et la fenêtre de session sont rappelés ;
    - hors ouverture de Londres / New York / chevauchement, la confiance est réduite.

    `soften` module l'atténuation d'un avis contraire. À 0,35 (défaut), un agent qui lit le PRIX et
    contredit la tendance est presque réduit au silence : deux lectures du même graphique ne
    peuvent pas diverger sans que l'une se trompe. Les agents volontairement orthogonaux au prix
    (sentiment, macro) reçoivent une valeur plus douce : leur désaccord est une INFORMATION, pas une
    erreur de lecture, et l'écraser reviendrait à se priver du seul contre-pouvoir du système.

    Sans contexte playbook (ou données insuffisantes), la sortie est renvoyée inchangée.
    """
    pb = (context or {}).get("playbook")
    if not pb or pb.get("insufficient"):
        return score, confidence

    bias = pb.get("bias", 0)
    metrics["playbook_bias"] = bias
    metrics["playbook_direction"] = pb.get("direction")

    # La tendance telle que le moteur multi-indicateurs l'a établie : le même chiffre pour tous.
    trend = pb.get("trend") or {}
    if trend.get("status"):
        metrics["trend_status"] = trend["status"]
        metrics["trend_confidence"] = trend.get("score_100")
        adx = trend.get("adx") or {}
        if adx.get("journalier") is not None:
            metrics["trend_adx"] = adx["journalier"]
        if trend["status"] == "valid":
            notes.append(
                f"Tendance du desk : confiance {trend.get('score_100')}/100, "
                f"ADX journalier {adx.get('journalier', 0):.0f} — mesurée sur quatre unités de "
                "temps et figée pour toute l'analyse"
            )

    if bias != 0:
        sens = "haussière" if bias > 0 else "baissière"
        if score * bias < 0:
            score *= soften
            notes.append(f"Stratégie : biais contraire à la tendance de fond {sens} → atténué")
        else:
            score = max(-1.0, min(1.0, 0.75 * score + 0.25 * bias))
            notes.append(f"Stratégie : aligné sur la tendance de fond {sens} (D1 + 4 h + 1 h)")

    levels = pb.get("levels") or {}
    if levels.get("major_support") is not None or levels.get("major_resistance") is not None:
        metrics["major_support"] = levels.get("major_support")
        metrics["major_resistance"] = levels.get("major_resistance")
        notes.append(
            f"Niveaux majeurs : support {levels.get('major_support')} / "
            f"résistance {levels.get('major_resistance')}"
        )

    # Zones d'offre/demande et supports/résistances classés : les niveaux fins sur lesquels la
    # stratégie pose son entrée, son stop et son objectif.
    entry_levels = pb.get("entry_levels") or {}
    ranked = (entry_levels.get("ranked") or [])[:3]
    if ranked:
        metrics["sr_levels"] = ranked
        notes.append("Niveaux d'exécution : " + " · ".join(
            f"{lv['price']:.6g} ({lv['side']}, note {lv['score']:.2f})" for lv in ranked))
    sd_zones = (entry_levels.get("zones") or [])[:2]
    if sd_zones:
        metrics["sd_zones"] = sd_zones
        notes.append("Zones offre/demande : " + " · ".join(
            f"{z['kind']} {z['low']:.6g}–{z['high']:.6g} (force {z['strength']:.2f})"
            for z in sd_zones))

    session = pb.get("session") or {}
    if session.get("quality") is not None:
        metrics["session_quality"] = session["quality"]
        metrics["session_label"] = session.get("label")
        confidence *= 0.80 + 0.20 * float(session["quality"])
        notes.append(f"Fenêtre de session : {session.get('label')}")

    if pb.get("ready"):
        notes.append(f"Déclencheur 15 min actif ({pb.get('trigger')})")
        confirmations = pb.get("entry_confirmations") or []
        if confirmations:
            metrics["entry_confirmations"] = [c["key"] for c in confirmations]
            notes.append("Confirmations d'entrée : " + ", ".join(
                f"{c['key']} ({c['contribution']:.2f})" for c in confirmations))
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
