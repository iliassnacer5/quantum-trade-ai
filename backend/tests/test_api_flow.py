"""Test d'intégration de la boucle de valeur via l'API (offline, déterministe).

Couvre la Definition of Done Phase 1 : inscription -> onboarding -> génération de signal
explicable -> historique -> isolation multi-tenant -> facturation.
"""

import pytest
from fastapi.testclient import TestClient

from app.data.synthetic import generate_candles
from app.main import app
from app.repositories.store import reset_store


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """État propre + forcer le repli synthétique (pas d'appel réseau Binance)."""
    reset_store()

    async def fake_fetch(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("offline test")

    monkeypatch.setattr("app.data.binance.fetch_klines", fake_fetch)
    yield


def _auth(client: TestClient, email: str) -> dict:
    r = client.post("/api/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_full_value_loop():
    client = TestClient(app)
    h = _auth(client, "trader@test.com")

    # Onboarding
    r = client.post(
        "/api/onboarding",
        json={"risk_profile": "moderate", "capital": 10000, "watchlist": ["BTC/USDT"]},
        headers=h,
    )
    assert r.status_code == 200 and r.json()["onboarded"] is True

    # Génération de signal
    r = client.post("/api/signals/generate", json={"asset": "BTC/USDT", "timeframe": "1h"}, headers=h)
    assert r.status_code == 200
    card = r.json()
    assert card["asset"] == "BTC/USDT"
    assert 0 <= card["confidence"] <= 100
    assert card["rationale"]  # justification présente

    # Historique
    r = client.get("/api/signals", headers=h)
    assert r.status_code == 200 and len(r.json()) == 1


def test_auth_required():
    client = TestClient(app)
    assert client.get("/api/signals").status_code == 401
    assert client.get("/api/auth/me").status_code == 401


def test_tenant_isolation():
    client = TestClient(app)
    h1 = _auth(client, "a@test.com")
    h2 = _auth(client, "b@test.com")
    client.post("/api/signals/generate", json={"asset": "BTC/USDT"}, headers=h1)
    # Le tenant B ne voit pas les signaux du tenant A
    assert client.get("/api/signals", headers=h2).json() == []
    assert len(client.get("/api/signals", headers=h1).json()) == 1


def test_free_plan_market_gating():
    client = TestClient(app)
    h = _auth(client, "free@test.com")
    client.post("/api/onboarding", json={"risk_profile": "moderate", "capital": 10000, "watchlist": ["BTC/USDT"]}, headers=h)
    # Free = 1 marché : BTC ok
    assert client.post("/api/signals/generate", json={"asset": "BTC/USDT"}, headers=h).status_code == 200
    # 2e marché refusé en Free
    assert client.post("/api/signals/generate", json={"asset": "ETH/USDT"}, headers=h).status_code == 402
    # Upgrade Starter -> autorisé
    assert client.post("/api/billing/checkout/starter", headers=h).status_code == 200
    assert client.post("/api/signals/generate", json={"asset": "ETH/USDT"}, headers=h).status_code == 200


def test_synthetic_generator_deterministic():
    a = generate_candles(seed=42)
    b = generate_candles(seed=42)
    assert [c.close for c in a] == [c.close for c in b]
