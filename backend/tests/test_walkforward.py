"""Tests de la validation walk-forward (out-of-sample) + verdict honnête."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.backtest import walkforward
from app.main import app


def _register(client: TestClient) -> dict:
    email = f"u{uuid.uuid4().hex[:8]}@test.com"
    r = client.post("/api/auth/register", json={"email": email, "password": "password123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _upgrade(client: TestClient, h: dict, plan: str) -> None:
    assert client.post(f"/api/billing/checkout/{plan}", headers=h).status_code == 200


def _fake_rows(n: int) -> list[dict]:
    """Fabrique des bougies horodatées (montée régulière) pour un walk-forward déterministe."""
    rows = []
    price = 100.0
    for i in range(n):
        price += 0.1
        rows.append({"time": 1_700_000_000 + i * 3600, "open": price, "high": price + 1,
                     "low": price - 1, "close": price + 0.5, "volume": 10.0})
    return rows


async def test_walk_forward_real_data_produces_verdict(monkeypatch):
    async def _rows(symbol, interval="1h", limit=1000, **kw):  # noqa: ANN001
        return _fake_rows(1000)

    monkeypatch.setattr(walkforward, "get_ohlcv", _rows)
    res = await walkforward.walk_forward("BTC/USDT", "1h", folds=4)
    assert res["data_real"] is True
    assert res["verdict"] in {"robuste", "fragile", "non_prouve", "insuffisant"}
    assert len(res["folds"]) >= 1
    assert "consistency" in res


async def test_walk_forward_synthetic_is_unproven(monkeypatch):
    async def _empty(symbol, interval="1h", limit=1000, **kw):  # noqa: ANN001
        return []  # données indisponibles -> repli synthétique

    monkeypatch.setattr(walkforward, "get_ohlcv", _empty)
    res = await walkforward.walk_forward("FOO/USDT", "1h", folds=4)
    assert res["data_real"] is False
    assert res["verdict"] == "non_prouve"


def test_track_record_endpoint_requires_pro():
    client = TestClient(app)
    h = _register(client)  # FREE
    assert client.get("/api/backtest/track-record", headers=h).status_code == 402


def test_track_record_endpoint_pro(monkeypatch):
    # Évite de vrais backtests longs : on stub le walk-forward.
    async def _fake_wf(symbol, timeframe="1h", folds=4):  # noqa: ANN001
        return {"symbol": symbol, "timeframe": timeframe, "folds": [], "folds_evaluated": 0,
                "total_trades": 0, "profitable_folds": 0, "consistency": 0.0, "avg_win_rate": 0.0,
                "avg_profit_factor": 0.0, "avg_pnl_pct": 0.0, "data_real": True,
                "verdict": "insuffisant", "label": "test"}

    monkeypatch.setattr("app.backtest.walkforward.walk_forward", _fake_wf)
    client = TestClient(app)
    h = _register(client)
    _upgrade(client, h, "pro")
    r = client.get("/api/backtest/track-record", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert "validation" in body and "observed" in body and "disclaimer" in body
