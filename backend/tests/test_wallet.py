"""Tests du portefeuille virtuel : solde, P&L réalisé, statistiques, reset."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.main import app


def _client_with_paper():
    client = TestClient(app)
    email = f"u{uuid.uuid4().hex[:8]}@test.com"
    r = client.post("/api/auth/register", json={"email": email, "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    cid = client.post("/api/execution/brokers", json={"broker": "paper", "mode": "paper"}, headers=h).json()["id"]
    return client, h, cid


def test_wallet_empty_starts_at_default():
    client, h, _ = _client_with_paper()
    w = client.get("/api/wallet", headers=h).json()
    assert w["starting_balance"] == 10000.0 and w["balance"] == 10000.0
    assert w["stats"]["trades"] == 0


def test_wallet_reflects_closed_trade(monkeypatch):
    """Un trade clôturé en gain augmente le solde et les stats."""
    from app.data import replay

    client, h, cid = _client_with_paper()
    order = client.post(
        "/api/execution/orders",
        json={"conn_id": cid, "symbol": "BTC/USDT", "side": "buy", "qty": 0.01, "stop_loss": 1.0, "take_profit": 1_000_000.0},
        headers=h,
    ).json()
    entry = order["filled_price"]

    # Le rejeu renvoie un VERDICT motivé (et non plus un tuple) : il doit pouvoir dire « je ne sais
    # pas » sans que l'appelant clôture au hasard. Ici on simule un objectif réellement atteint,
    # avec une clôture POSTÉRIEURE à l'ouverture (une clôture antérieure serait rejetée).
    async def _won(symbol, direction, e, sl, tp, since, interval="15m"):  # noqa: ANN001
        return {"outcome": "won", "exit_price": tp, "closed_ts": replay.iso_to_unix(since) + 900,
                "reason": "objectif atteint", "data_real": True, "covered_until": None, "bars": 1}
    monkeypatch.setattr(replay, "replay_outcome", _won)
    client.post(f"/api/execution/orders/{order['id']}/check", headers=h)  # clôture en 'won'

    w = client.get("/api/wallet", headers=h).json()
    assert w["stats"]["trades"] == 1 and w["stats"]["wins"] == 1
    assert w["realized_pnl"] > 0 and w["balance"] > w["starting_balance"]
    assert w["realized_pnl"] == round((1_000_000.0 - entry) * 0.01, 2)


def test_wallet_reset():
    client, h, _ = _client_with_paper()
    w = client.post("/api/wallet/reset?starting_balance=5000&clear_orders=true", headers=h).json()
    assert w["starting_balance"] == 5000.0 and w["balance"] == 5000.0
