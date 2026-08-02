"""`domain.factor_attribution` — le pont entre les facteurs de la stratégie et les agents.

C'est la pièce qui permet au Journal d'apprendre des trades RÉELLEMENT clôturés (pas seulement du
walk-forward nocturne) : on vérifie ici la conversion factor_votes -> agent_scores, indépendamment
de tout store ou service, avant de tester son intégration dans `journal_service`.
"""

from __future__ import annotations

from app.domain import factor_attribution as fa


def test_factor_votes_agree_when_layers_are_silent():
    assert fa.factor_votes({}, "BUY") == {}


def test_factor_votes_aggregates_across_layers():
    """+1 par couche où le facteur a plaidé dans le sens du trade, -1 sinon."""
    layers = {
        "daily": {"factors": [{"key": "ma", "score": 0.6}]},     # haussier, trade = BUY -> accord
        "h4": {"factors": [{"key": "ma", "score": 0.4}]},        # idem -> accord
        "m15": {"factors": [{"key": "ma", "score": -0.2}]},      # baissier -> désaccord
    }
    votes = fa.factor_votes(layers, "BUY")
    assert votes["ma"] == 1     # +1 +1 -1 = +1 net


def test_agent_scores_ignore_unowned_factors():
    """Un facteur sans propriétaire connu (FACTOR_OWNER) ne doit contribuer à AUCUN agent."""
    scores = fa.agent_scores_from_votes({"facteur_inconnu": 5}, "BUY")
    assert scores == {}


def test_agent_scores_flip_sign_with_direction():
    """Un vote « accord » sur un ACHAT dit que le facteur était haussier ; le MÊME vote sur une
    VENTE dit qu'il était baissier — deux avis opposés, malgré un vote identique."""
    votes = {"ma": 1}   # le facteur a plaidé DANS le sens du trade
    buy = fa.agent_scores_from_votes(votes, "BUY")
    sell = fa.agent_scores_from_votes(votes, "SELL")
    assert buy["technical"] > 0
    assert sell["technical"] < 0
    assert buy["technical"] == -sell["technical"]


def test_agent_scores_average_multiple_owned_factors():
    """« ma » et « rsi » appartiennent tous deux à `technical` : un seul score par agent, pas un
    par facteur — sinon un agent aux nombreux facteurs pèserait mécaniquement plus lourd."""
    scores = fa.agent_scores_from_votes({"ma": 1, "rsi": 1}, "BUY")
    assert list(scores.keys()) == ["technical"]
    assert scores["technical"] > 0


def test_agent_scores_omit_contradictory_agents():
    """Deux facteurs du même agent qui se contredisent net à zéro : pas de signal fiable, on omet
    l'agent plutôt que d'inventer un avis moyen qui ne représente ni l'un ni l'autre."""
    scores = fa.agent_scores_from_votes({"ma": 1, "rsi": -1}, "BUY")
    assert "technical" not in scores


def test_agent_scores_cross_the_reliability_threshold():
    """La magnitude doit dépasser le seuil ±0.15 qu'utilise `compute_weight_multipliers`, sinon le
    vote serait calculé pour rien — silencieusement ignoré en aval."""
    scores = fa.agent_scores_from_votes({"ma": 2}, "BUY")
    assert abs(scores["technical"]) > 0.15
