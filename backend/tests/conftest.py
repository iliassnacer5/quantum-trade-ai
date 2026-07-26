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
    settings.block_synthetic_orders = False  # tests déterministes (pas de dépendance réseau)
    # Le PLAYBOOK (stratégie du desk) est un gate à part entière : il exige 4 unités de temps
    # réelles et refuse la plupart des setups synthétiques. Il est désactivé par défaut ici et
    # testé spécifiquement dans tests/test_playbook.py (comme les autres gates : experts,
    # blackout événementiel, sweep de l'edge).
    settings.playbook_enabled = False
    reset_store()
    yield
