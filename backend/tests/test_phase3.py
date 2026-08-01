"""Tests Phase 3 : gating des plans, Copilot, Journal & apprentissage, équipe."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core import plans
from app.core.config import get_settings
from app.main import app
from app.services import journal_service


def _register(client: TestClient, email: str | None = None) -> dict:
    email = email or f"u{uuid.uuid4().hex[:8]}@test.com"
    r = client.post("/api/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code in (200, 201), r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _upgrade(client: TestClient, headers: dict, plan_id: str) -> None:
    assert client.post(f"/api/billing/checkout/{plan_id}", headers=headers).status_code == 200


# ---------------- Plans / gating ----------------
def test_plan_matrix():
    assert plans.plan_allows("pro", "backtesting") is True
    assert plans.plan_allows("free", "backtesting") is False
    assert plans.plan_allows("starter", "copilot") is False
    assert plans.plan_allows("elite", "api_access") is True
    assert plans.plan_allows("pro", "api_access") is False


def test_plan_endpoint_reflects_upgrade():
    client = TestClient(app)
    h = _register(client)
    data = client.get("/api/plan", headers=h).json()
    assert data["plan"] == "free"
    assert data["features"]["copilot"] is False
    _upgrade(client, h, "pro")
    data = client.get("/api/plan", headers=h).json()
    assert data["plan"] == "pro"
    assert data["features"]["copilot"] is True
    assert data["features"]["backtesting"] is True


def test_gating_blocks_free_user():
    client = TestClient(app)
    h = _register(client)
    # Copilot et Journal réservés Pro -> 402 Payment Required
    assert client.post("/api/copilot/ask", json={"asset": "BTC/USDT", "message": "salut"}, headers=h).status_code == 402
    assert client.get("/api/journal", headers=h).status_code == 402


# ---------------- Copilot ----------------
def test_copilot_deterministic_fallback(monkeypatch):
    s = get_settings()
    s.anthropic_api_key = ""
    s.google_api_key = ""
    client = TestClient(app)
    h = _register(client)
    _upgrade(client, h, "pro")
    r = client.post("/api/copilot/ask", json={"asset": "BTC/USDT", "message": "Quel est le biais ?"}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["asset"] == "BTC/USDT"
    assert "déterministe" in body["answer"].lower() or len(body["answer"]) > 0


def test_copilot_stream_sse():
    client = TestClient(app)
    h = _register(client)
    _upgrade(client, h, "pro")
    with client.stream("POST", "/api/copilot/chat", json={"asset": "BTC/USDT", "message": "analyse"}, headers=h) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        chunks = "".join(list(r.iter_text()))
    assert "[DONE]" in chunks


# ---------------- Journal ----------------
def test_journal_lifecycle(monkeypatch):
    from app.data import markets
    from app.domain.indicators import Candle

    # Tendance haussière franche -> signal directionnel garanti (seuls les BUY/SELL créent une
    # entrée journal ; les HOLD ne sont plus enregistrés car sans trade il n'y a rien à apprendre).
    async def _uptrend(symbol, interval="1h", limit=200, **kw):  # noqa: ANN001
        out, p = [], 100.0
        for _ in range(max(limit, 210)):
            p *= 1.004
            out.append(Candle(p * 0.997, p * 1.006, p * 0.996, p, 1000.0))
        return out
    monkeypatch.setattr(markets, "load_candles", _uptrend)

    client = TestClient(app)
    h = _register(client)
    _upgrade(client, h, "pro")
    client.post("/api/signals/mode?mode=aggressive", headers=h)
    client.post("/api/signals/generate", json={"asset": "BTC/USDT", "timeframe": "1h", "notify": False}, headers=h)
    entries = client.get("/api/journal", headers=h).json()
    assert len(entries) >= 1, "un signal directionnel doit créer une entrée journal"
    entry_id = entries[0]["id"]
    # Clôture le trade en gain
    r = client.post(f"/api/journal/{entry_id}/close", json={"outcome": "win", "pnl": 120.5}, headers=h)
    assert r.status_code == 200 and r.json()["outcome"] == "win"
    # Insights : stats + multiplicateurs appris
    ins = client.get("/api/journal/insights", headers=h).json()
    assert ins["stats"]["closed"] >= 1 and ins["stats"]["wins"] >= 1
    assert "weight_multipliers" in ins
    # Explication IA (repli déterministe sans LLM)
    exp = client.post(f"/api/journal/{entry_id}/explain", headers=h)
    assert exp.status_code == 200 and len(exp.json()["explanation"]) > 0


# ---------------- Journal : trades PLAYBOOK réunis avec les signaux (28/07/2026) ----------------
def _open_playbook_order(store, tenant_id: str, *, symbol="GOOGL", side="buy"):
    import asyncio

    from app.services import execution_service

    conn_id = execution_service.ensure_paper_connection(store, tenant_id)
    return asyncio.run(
        execution_service.place_order(
            store, tenant_id, conn_id=conn_id, symbol=symbol, side=side, qty=1.0,
            stop_loss=95.0, take_profit=110.0,
        )
    )


def test_journal_shows_playbook_trades_not_just_signals(monkeypatch):
    """LE bug rapporté : un compte avec des positions démo (auto-entrée / « Ouvrir en démo »)
    voyait le Journal afficher zéro partout, alors que ces trades sont bien réels — ils vivaient
    juste dans un flux distinct (`execution_service`) que le Journal ne lisait jamais."""
    from app.data import markets
    from app.domain.indicators import Candle
    from app.repositories.store import get_store
    from app.services import journal_service

    async def _flat(symbol, interval="1h", limit=200, **kw):  # noqa: ANN001
        return [Candle(100.0, 100.0, 100.0, 100.0, 1000.0)] * 60

    monkeypatch.setattr(markets, "load_candles", _flat)

    client = TestClient(app)
    h = _register(client)
    _upgrade(client, h, "pro")
    store = get_store()
    user = store.users.list_all()[-1]   # le compte qu'on vient d'enregistrer
    _open_playbook_order(store, user.tenant_id)

    # Bout en bout, par l'API que la page appelle réellement.
    entries = client.get("/api/journal", headers=h).json()
    assert any(e.get("source") == "playbook" for e in entries), entries
    ins = client.get("/api/journal/insights", headers=h).json()
    assert ins["stats"]["open"] >= 1

    # Détail au niveau service.
    playbook_rows = journal_service.playbook_entries(store, user.tenant_id)
    assert len(playbook_rows) == 1
    assert playbook_rows[0]["source"] == "playbook"
    assert playbook_rows[0]["direction"] == "BUY"
    assert playbook_rows[0]["outcome"] == "open"


def test_playbook_entries_exclude_neutral_outcomes(monkeypatch):
    """Une position `reset` ou `invalid` n'a jamais été jouée jusqu'au bout : elle ne doit ni
    gonfler le compteur « ouverts » ni fausser le taux de réussite du Journal."""
    from app.data import markets
    from app.domain.indicators import Candle
    from app.repositories.store import get_store
    from app.services import execution_service, journal_service

    async def _flat(symbol, interval="1h", limit=200, **kw):  # noqa: ANN001
        return [Candle(100.0, 100.0, 100.0, 100.0, 1000.0)] * 60

    monkeypatch.setattr(markets, "load_candles", _flat)

    store = get_store()
    client = TestClient(app)
    h = _register(client)
    _upgrade(client, h, "pro")
    user = store.users.list_all()[-1]
    order = _open_playbook_order(store, user.tenant_id)
    store.records.put(execution_service.ORDER, order["id"],
                      {**order, "outcome": "reset"}, tenant_id=user.tenant_id)

    rows = journal_service.playbook_entries(store, user.tenant_id)
    assert rows == []


def test_reliability_falls_back_to_training_when_only_playbook_trades_exist(monkeypatch):
    """Sans signal classique mais avec un entraînement nocturne déjà passé, la fiabilité par
    agent doit venir de LÀ plutôt que d'afficher un « pas assez de trades » trompeur."""
    from app.data import markets
    from app.domain.indicators import Candle
    from app.repositories.store import get_store
    from app.services import training_service

    async def _flat(symbol, interval="1h", limit=200, **kw):  # noqa: ANN001
        return [Candle(100.0, 100.0, 100.0, 100.0, 1000.0)] * 60

    monkeypatch.setattr(markets, "load_candles", _flat)
    training_service._STATE = {
        "trades": 42,
        "factor_competence": {"ma": {"observations": 12, "accuracy": 66.7}},
    }
    try:
        client = TestClient(app)
        h = _register(client)
        _upgrade(client, h, "pro")
        store = get_store()
        user = store.users.list_all()[-1]
        _open_playbook_order(store, user.tenant_id)

        ins = client.get("/api/journal/insights", headers=h).json()
        assert ins["reliability_source"] == "training"
        assert ins["reliability"][0]["agent"] == "ma"
        assert ins["trades_learned"] == 42
    finally:
        training_service._STATE = {}


def test_journal_stats_helper():
    entries = [
        {"outcome": "win", "pnl": 100},
        {"outcome": "loss", "pnl": -40},
        {"outcome": "open", "pnl": None},
    ]
    st = journal_service.stats(entries)
    assert st["closed"] == 2 and st["wins"] == 1 and st["losses"] == 1
    assert st["win_rate"] == 50.0 and st["total_pnl"] == 60.0


# ---------------- Push mobile ----------------
def test_push_token_roundtrip():
    client = TestClient(app)
    h = _register(client)
    assert client.get("/api/settings", headers=h).json()["push_enabled"] is False
    r = client.patch("/api/settings", json={"push_token": "ExponentPushToken[abc123]"}, headers=h)
    assert r.status_code == 200 and r.json()["push_enabled"] is True


# ---------------- Équipe ----------------
def test_team_invite_and_list():
    client = TestClient(app)
    h = _register(client)
    _upgrade(client, h, "pro")
    inv = client.post("/api/team/invite", json={"email": f"col{uuid.uuid4().hex[:6]}@test.com"}, headers=h)
    assert inv.status_code == 201
    assert inv.json()["temp_password"]
    members = client.get("/api/team", headers=h).json()
    assert len(members["members"]) == 2
    assert any(m["role"] == "owner" for m in members["members"])
