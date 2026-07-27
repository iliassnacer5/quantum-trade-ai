"""Test de l'endpoint OHLCV (repli synthétique, offline)."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.repositories.store import reset_store


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    reset_store()

    async def fake(*a, **k):
        raise RuntimeError("offline")

    # Force le repli synthétique de l'OHLCV.
    monkeypatch.setattr("app.data.ohlcv._binance_ohlcv", fake)
    yield


def test_ohlcv_requires_auth():
    assert TestClient(app).get("/api/market/ohlcv").status_code == 401


def test_ohlcv_returns_candles():
    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": "c@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    resp = client.get("/api/market/ohlcv?asset=BTC/USDT&timeframe=1h&limit=120", headers=h)
    assert resp.status_code == 200
    body = resp.json()
    # La réponse porte sa SOURCE : une page ne doit jamais afficher du fictif comme du réel.
    assert set(body) >= {"candles", "source", "real"}
    data = body["candles"]
    assert len(data) == 120
    first = data[0]
    assert {"time", "open", "high", "low", "close"} <= set(first)
    assert first["high"] >= first["low"]
    # Horodatages croissants
    assert data[1]["time"] > data[0]["time"]


def test_ohlcv_declares_when_no_real_data_is_available(monkeypatch):
    """Sans source réelle ET sans autorisation de fabriquer : série VIDE, dite explicitement."""
    from app.core.config import get_settings
    from app.data import yahoo

    async def _fail(*a, **k):
        raise RuntimeError("hors ligne")

    monkeypatch.setattr(yahoo, "fetch_ohlcv", _fail)
    s = get_settings()
    s.data_allow_synthetic = False          # comportement de PRODUCTION
    try:
        client = TestClient(app)
        r = client.post("/api/auth/register", json={"email": "nodata@test.com", "password": "password123"})
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        body = client.get("/api/market/ohlcv?asset=EUR/USD&timeframe=1h", headers=h).json()
    finally:
        s.data_allow_synthetic = True
    assert body["candles"] == [] and body["real"] is False
    assert body["source"] == "unavailable"
    assert "Aucune donnée réelle" in body["note"]
