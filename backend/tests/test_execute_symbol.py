"""Tests de l'ouverture démo PAR SYMBOLE (bouton d'une carte « trade du jour »).

Vérifie que la stratégie est RECALCULÉE au moment de l'appel (jamais de niveaux transmis par le
client), qu'un setup exécutable s'ouvre avec ses vrais niveaux, et qu'un setup non exécutable
explique pourquoi sans rien ouvrir.
"""

from __future__ import annotations

import pytest

from tests.test_playbook import (
    _END, _build, _h1, _m15_with_pullback_entry, _no_trigger_15m, _tenant, _tf,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def playbook_on(monkeypatch):
    from app.core.config import get_settings
    from app.data import markets
    from app.services import playbook_service

    s = get_settings()
    s.playbook_enabled = True
    playbook_service.clear_cache()

    # end_close=_END aligne le 15 min sur le prix courant du 4h/journalier/mensuel — sans cet
    # alignement, le stop structurel (recalé sur le 4h quand le 15 min est trop serré) se calcule
    # entre deux échelles de prix incohérentes et le setup est refusé pour un stop artificiellement
    # large.
    series = {
        "1M": _tf("monthly"), "1d": _tf("daily"), "4h": _tf("h4"), "1h": _h1(True),
        "15m": _m15_with_pullback_entry(True, end_close=_END),
    }
    # La garde d'heures de marché est testée dans test_auto_entry : ici on veut vérifier
    # l'ouverture par symbole, indépendamment du jour où la suite tourne.
    previous_gate = s.playbook_trade_only_when_open
    s.playbook_trade_only_when_open = False

    async def _load(symbol, interval="1h", limit=200):  # noqa: ANN001
        return series.get(interval, series["4h"])

    monkeypatch.setattr(markets, "load_candles", _load)
    monkeypatch.setattr(markets, "data_source", lambda symbol: "real")
    yield s
    s.playbook_enabled = False
    s.playbook_trade_only_when_open = previous_gate
    playbook_service.clear_cache()


async def test_execute_symbol_opens_the_recomputed_setup(playbook_on):
    """La stratégie est recalculée : le trade ouvert reflète les niveaux ACTUELS, pas ceux du client."""
    from app.repositories.store import get_store
    from app.services import execution_service

    store = get_store()
    user = _tenant(store, "onesym@test.com")
    setup = _build()
    assert setup.ready, setup.reasons

    report = await execution_service.execute_playbook_symbol(store, user.tenant_id, "EUR/USD")
    assert report["symbol"] == "EUR/USD"
    assert len(report["opened"]) == 1
    o = report["opened"][0]
    assert o["stop_loss"] == setup.stop_loss and o["take_profit"] == setup.take_profit_1
    assert o["side"] == ("buy" if setup.direction == "BUY" else "sell")


async def test_execute_symbol_explains_when_not_ready(playbook_on, monkeypatch):
    """Si le déclencheur 15 min n'est plus actif au moment du clic, rien n'est ouvert."""
    from app.data import markets
    from app.repositories.store import get_store
    from app.services import execution_service, playbook_service

    store = get_store()
    user = _tenant(store, "onesym2@test.com")

    series = {
        "1M": _tf("monthly"), "1d": _tf("daily"), "4h": _tf("h4"),
        "15m": _no_trigger_15m(price=_tf("h4")[-1].close),
    }

    async def _load(symbol, interval="1h", limit=200):  # noqa: ANN001
        return series.get(interval, series["4h"])

    monkeypatch.setattr(markets, "load_candles", _load)
    playbook_service.clear_cache()

    report = await execution_service.execute_playbook_symbol(store, user.tenant_id, "GBP/USD")
    assert report["opened"] == []
    assert "GBP/USD" in report["summary"]
    assert "n'est pas exécutable" in report["summary"]


async def test_execute_symbol_never_opens_twice(playbook_on):
    """Cliquer deux fois de suite n'empile pas la même position."""
    from app.repositories.store import get_store
    from app.services import execution_service

    store = get_store()
    user = _tenant(store, "onesym3@test.com")

    first = await execution_service.execute_playbook_symbol(store, user.tenant_id, "EUR/USD")
    assert len(first["opened"]) == 1

    second = await execution_service.execute_playbook_symbol(store, user.tenant_id, "EUR/USD")
    assert second["opened"] == []
    assert any("déjà ouverte" in s["reason"] for s in second["skipped"])


def test_api_execute_symbol_endpoint(playbook_on):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": "onesymapi@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    resp = client.post("/api/execution/playbook/execute-symbol/EUR%2FUSD", headers=h)
    assert resp.status_code == 201
    body = resp.json()
    assert body["symbol"] == "EUR/USD"
    assert isinstance(body["opened"], list)
