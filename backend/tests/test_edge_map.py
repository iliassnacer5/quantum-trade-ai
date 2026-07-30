"""Tests de la carte de l'edge (Phase B du plan maître) : sweep, classification, gating auto-trade."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import edge_map_service


def _pro(client: TestClient) -> dict:
    r = client.post("/api/auth/register", json={"email": f"u{uuid.uuid4().hex[:8]}@t.co", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.post("/api/billing/checkout/pro", headers=h).status_code == 200
    return h


def test_classification():
    assert edge_map_service._classify(5.0, 1.4, trades=20) == "green"
    assert edge_map_service._classify(5.0, 10.0, trades=1) == "yellow"  # 1 trade chanceux ≠ edge
    assert edge_map_service._classify(2.0, 1.05, trades=30) == "yellow"
    assert edge_map_service._classify(-3.0, 1.5, trades=30) == "red"   # PF élevé mais sous le buy&hold
    assert edge_map_service._classify(0.0, 0.8, trades=30) == "red"


async def test_sweep_maps_where_the_desk_strategy_works(monkeypatch):
    """Le sweep dit OÙ la stratégie du desk a un edge, et n'accorde le vert qu'à ce qui tient."""
    from app.repositories.store import get_store

    async def _fake_wf(symbol, tf, folds=4, preloaded=None, **kw):  # noqa: ANN001
        # La stratégie du desk marche sur BTC dans ce scénario, et nulle part ailleurs.
        good = symbol == "BTC/USDT"
        return {"avg_alpha_pct": 8.0 if good else -5.0, "avg_profit_factor": 1.3 if good else 0.6,
                "avg_win_rate": 45.0, "total_trades": 20, "verdict": "robuste" if good else "non_prouve",
                "data_real": True}

    async def _fake_preload(symbol, tf):  # pas de réseau
        from app.domain.indicators import Candle
        return [Candle(100, 101, 99, 100, 10)] * 1000, True

    import app.backtest.walkforward as wf_mod
    monkeypatch.setattr(wf_mod, "walk_forward", _fake_wf)
    monkeypatch.setattr(edge_map_service, "_preload", _fake_preload)

    store = get_store()
    p1 = await edge_map_service.run_edge_sweep(store, timeframes=["4h"], markets=["crypto"])
    assert p1["greens"] == 1
    green = next(r for r in p1["rows"] if r["status"] == "green")
    # Une seule stratégie balayée : toutes les lignes portent son identifiant.
    assert {r["strategy"] for r in p1["rows"]} == {"playbook"}
    assert green["symbol"] == "BTC/USDT" and green["green_streak"] == 1
    # 2e sweep -> streak = 2 (stabilité)
    p2 = await edge_map_service.run_edge_sweep(store, timeframes=["4h"], markets=["crypto"])
    green2 = next(r for r in p2["rows"] if r["status"] == "green")
    assert green2["green_streak"] == 2
    # is_combo_green respecte le streak minimal
    assert edge_map_service.is_combo_green(store, "playbook", "BTC/USDT", min_streak=2)
    assert not edge_map_service.is_combo_green(store, "playbook", "ETH/USDT")


def test_the_sweep_covers_every_market_including_indices():
    """La stratégie s'appliquant à tous les marchés, la carte doit tous les balayer.

    Et pas un échantillon : l'univers de la carte est désormais le catalogue COMPLET, sinon elle
    répond « où gagne-t-on parmi ceux que j'ai choisi de regarder », ce qui n'est pas la question.
    """
    from app.data import symbols as symbols_catalog

    uni = edge_map_service.universe()
    assert set(uni) == {"forex", "commodity", "index", "stock", "crypto"}
    assert "SPX500" in uni["index"]
    assert "GER40" in uni["index"]
    assert sum(len(v) for v in uni.values()) == len(symbols_catalog.all_symbols())


def test_edge_map_endpoint_empty_then_available():
    client = TestClient(app)
    h = _pro(client)
    body = client.get("/api/backtest/edge-map", headers=h).json()
    assert "rows" in body and "note" in body  # avant tout sweep : structure vide honnête


def test_clear_journal_endpoint():
    """Phase A : DELETE /api/journal repart d'un thermomètre propre."""
    from app.repositories.store import get_store

    client = TestClient(app)
    h = _pro(client)
    me = client.get("/api/auth/me", headers=h).json()
    store = get_store()
    tenant_id = store.users.get(me["id"]).tenant_id
    store.journal.add(tenant_id, {"symbol": "BTC/USDT", "direction": "BUY", "outcome": "win",
                                  "pnl": 10, "agent_scores": {"technical": 0.5}})
    assert len(client.get("/api/journal", headers=h).json()) == 1
    r = client.delete("/api/journal", headers=h)
    assert r.status_code == 200 and r.json()["cleared"] == 1
    assert client.get("/api/journal", headers=h).json() == []
