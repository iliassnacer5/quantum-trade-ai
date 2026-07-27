"""EXPERTISE DES AGENTS — ce que l'entraînement de la nuit a appris à chaque agent.

L'entraînement (`services.training_service`) rejoue la stratégie du desk sur l'historique et mesure,
agent par agent, à quelle fréquence ses arguments avaient raison. Deux sorties arrivent ici :

- une **fiche d'expertise** rédigée (3 règles opératoires tirées de ses chiffres), injectée dans le
  prompt de l'agent pour qu'il raisonne avec l'expérience de la veille au lieu de repartir de zéro ;
- une **note de compétence mesurée**, ajoutée à sa justification pour que l'utilisateur sache sur
  quelle expérience l'agent s'appuie.

Aucun chiffre n'est inventé ici : ce module ne fait que relayer ce qui a été mesuré.
"""

from __future__ import annotations


def memo(agent: str) -> str:
    """Fiche d'expertise du jour pour cet agent (chaîne vide si l'entraînement n'a pas encore eu lieu)."""
    from app.services import training_service

    return training_service.expertise(agent)


def competence(agent: str) -> dict | None:
    """Compétence MESURÉE de l'agent : justesse de ses facteurs sur la stratégie."""
    from app.services import training_service

    snap = training_service.snapshot()
    if not snap:
        return None
    owned = [k for k, v in training_service.FACTOR_OWNER.items() if v == agent]
    stats = {k: v for k, v in (snap.get("factor_competence") or {}).items() if k in owned}
    if not stats:
        return None
    total = sum(v["observations"] for v in stats.values())
    if not total:
        return None
    accuracy = sum(v["accuracy"] * v["observations"] for v in stats.values()) / total
    return {
        "accuracy": round(accuracy, 1),
        "observations": total,
        "multiplier": training_service.agent_multipliers().get(agent, 1.0),
        "factors": stats,
        "trained_on": snap.get("date"),
    }


def prompt_prefix(agent: str) -> str:
    """Bloc à placer en tête du prompt d'un agent : son expérience mesurée sur CETTE stratégie."""
    sheet = memo(agent)
    if not sheet:
        return ""
    return (
        "EXPÉRIENCE ACQUISE SUR CETTE STRATÉGIE (entraînement de la nuit, chiffres mesurés sur "
        f"l'historique réel — applique ces règles) :\n{sheet}\n\n"
    )


def note(agent: str) -> str:
    """Ligne de justification à ajouter à la sortie d'un agent (vide si non entraîné)."""
    comp = competence(agent)
    if not comp:
        return ""
    return (
        f"Expertise mesurée (entraînement du {comp['trained_on']}) : {comp['accuracy']}% de justesse "
        f"sur {comp['observations']} observations de cette stratégie → poids ×{comp['multiplier']}."
    )
