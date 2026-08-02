"""Attribution des facteurs de la stratégie aux agents — logique PARTAGÉE entre deux sources.

Deux boucles d'apprentissage distinctes existent dans le projet, et c'est volontaire :

1. `training_service` — rejoue la stratégie sur l'HISTORIQUE RÉEL du marché, hors ligne, sur
   planification. Mesure la justesse de chaque facteur (MA, RSI, MACD, VWAP, structure,
   divergence) sur l'univers complet.
2. `journal_service` — apprend de l'EXPÉRIENCE VÉCUE de chaque compte : les trades réellement
   ouverts (auto-entrée, « Ouvrir en démo ») et leur issue réelle.

Elles restent MULTIPLICATIVES et jamais fusionnées (cf. `signal_service.learning_multipliers`) :
la première est contrôlée et porte sur tout l'univers, la seconde est en direct et soumise aux
conditions réelles du compte (remplissage, garde-fous de portefeuille, gel hebdomadaire...). Ce
module porte la seule chose qu'elles doivent PARTAGER : la table qui dit quel agent porte quel
facteur, pour que les deux boucles parlent des mêmes agents et ne divergent jamais sur ce point.
"""

from __future__ import annotations

# Quel agent porte quel facteur de la stratégie. C'est la table qui transforme « le RSI avait
# raison 58 % du temps » en « l'agent technique mérite un poids un peu plus élevé ».
FACTOR_OWNER = {
    "ma": "technical",
    "rsi": "technical",
    "macd": "technical",
    "vwap": "volume",
    "volume": "volume",
    "structure": "pattern",
    "divergence": "pattern",
    "fibonacci": "playbook",
}


def factor_votes(setup_layers: dict, direction: str) -> dict[str, int]:
    """Pour chaque facteur : a-t-il plaidé DANS le sens du trade (+1), contre (-1), ou pas (0) ?

    On agrège les couches journalier / 4 h / 15 min (celles qui portent la décision d'entrée) :
    c'est la compétence du facteur SUR CETTE STRATÉGIE que l'on mesure, pas dans l'absolu.
    """
    want = 1 if direction == "BUY" else -1
    votes: dict[str, int] = {}
    for layer_name in ("daily", "h4", "m15"):
        for f in (setup_layers.get(layer_name) or {}).get("factors", []):
            score = f.get("score", 0)
            if not score:
                continue
            votes[f["key"]] = votes.get(f["key"], 0) + (1 if (score > 0) == (want > 0) else -1)
    return votes


def agent_scores_from_votes(votes: dict[str, int], direction: str) -> dict[str, float]:
    """Convertit un `factor_votes` (« ce facteur était-il d'accord avec le trade ? ») en scores
    DIRECTIONNELS par agent, dans la forme qu'attend `compute_weight_multipliers` — un score
    « ce facteur était-il haussier ou baissier ? », indépendant du sens du trade.

    C'est le pont entre les deux représentations : `factor_votes` dit « accord/désaccord avec CE
    trade », `compute_weight_multipliers` veut « avis absolu du facteur », et compare cet avis à
    l'issue lui-même. Un facteur qui a voté « accord » sur un ACHAT était haussier ; le même vote
    « accord » sur une VENTE était baissier — d'où le retournement de signe par la direction.

    La magnitude est FIXE (0.4, au-delà du seuil ±0.15 de `compute_weight_multipliers`) : le
    nombre de couches où un facteur apparaît n'est pas une mesure de force du signal, seul le signe
    porte une information fiable. Un agent dont les facteurs se contredisent d'une couche à l'autre
    (vote net nul) est omis — pas de signal net, pas d'avis à attribuer.
    """
    flip = 1.0 if direction == "BUY" else -1.0
    per_agent: dict[str, list[float]] = {}
    for factor, vote in votes.items():
        if vote == 0:
            continue
        owner = FACTOR_OWNER.get(factor)
        if not owner:
            continue
        per_agent.setdefault(owner, []).append(vote * flip)

    out: dict[str, float] = {}
    for agent, vals in per_agent.items():
        net = sum(vals)
        if net > 0:
            out[agent] = 0.4
        elif net < 0:
            out[agent] = -0.4
        # net == 0 : le facteur a voté dans les deux sens selon la couche -> pas de signal net.
    return out
