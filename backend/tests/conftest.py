"""Configuration partagée des tests : désactive le rate limiting et isole l'état."""

import pytest

from app.core.config import get_settings
from app.repositories.store import reset_store


@pytest.fixture(autouse=True)
def _test_env():
    settings = get_settings()
    settings.rate_limit_enabled = False
    settings.use_in_memory_db = True
    settings.live_ingestion_enabled = False  # jamais de WebSocket réelle pendant les tests
    settings.position_monitor_enabled = False  # pas de boucle de surveillance en test
    settings.learning_enabled = False  # pas de boucle d'apprentissage de fond en test
    settings.strategy_alerts_enabled = False  # pas de boucle d'alertes en test
    settings.paper_portfolio_guard = False  # tests déterministes (gros lots autorisés)
    settings.expert_agents_enabled = False  # path générique par défaut (testé séparément avec mocks)
    settings.event_blackout_enabled = False  # pas d'appel calendrier réseau en test
    settings.edge_sweep_enabled = False      # pas de sweep de fond en test (testé séparément)
    settings.auto_trade_green_only = False   # les tests d'auto-trade n'exigent pas la carte de l'edge
    # Gates du PLAN (verdicts de paires, sizing par conviction, gels de perte, corrélation) :
    # désactivés par défaut — chacun est testé explicitement dans tests/test_plan_phases.py,
    # comme les autres gates. Les activer partout rendrait tous les tests d'exécution dépendants
    # d'un record de verdicts qui n'a rien à voir avec ce qu'ils vérifient.
    settings.playbook_pair_gating = False
    settings.conviction_sizing_enabled = False
    settings.correlation_guard_enabled = False
    settings.loss_freeze_enabled = False
    settings.block_synthetic_orders = False  # tests déterministes (pas de dépendance réseau)
    # DONNÉES FICTIVES : interdites en production (`data_allow_synthetic=False` par défaut — une
    # bougie inventée affichée comme réelle est pire qu'une page vide). La suite de tests, elle,
    # doit tourner HORS LIGNE et de façon déterministe : elle les autorise EXPLICITEMENT ici.
    # Les tests qui vérifient le refus du synthétique le remettent à False localement.
    settings.data_allow_synthetic = True
    # Le PLAYBOOK (stratégie du desk) est un gate à part entière : il exige 4 unités de temps
    # réelles et refuse la plupart des setups synthétiques. Il est désactivé par défaut ici et
    # testé spécifiquement dans tests/test_playbook.py (comme les autres gates : experts,
    # blackout événementiel, sweep de l'edge).
    settings.playbook_enabled = False
    reset_store()
    yield
