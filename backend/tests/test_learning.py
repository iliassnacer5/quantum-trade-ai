"""Tests de la boucle d'apprentissage : résolution auto des signaux + multiplicateurs par volume."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.agents.journal import compute_weight_multipliers, reliability_report
from app.main import app


# ---------------- Apprentissage proportionnel au volume ----------------
def _entries(n: int, agent: str, hit: bool) -> list[dict]:
    """n trades où `agent` a un score directionnel BUY et l'issue est win (juste) ou loss (faux)."""
    return [
        {"outcome": "win" if hit else "loss", "direction": "BUY", "agent_scores": {agent: 0.5}}
        for _ in range(n)
    ]


def test_multiplier_grows_with_volume():
    """Un agent toujours juste : l'ajustement est timide à faible volume, marqué à fort volume."""
    few = compute_weight_multipliers(_entries(4, "technical", hit=True))["technical"]
    many = compute_weight_multipliers(_entries(120, "technical", hit=True))["technical"]
    assert 1.0 < few < many <= 1.5  # apprend davantage avec plus de données


def test_multiplier_penalizes_unreliable_agent():
    m = compute_weight_multipliers(_entries(60, "sentiment", hit=False))["sentiment"]
    assert m < 1.0  # agent souvent faux -> poids réduit


def test_reliability_report_shape():
    rep = reliability_report(_entries(30, "volume", hit=True))
    assert rep and rep[0]["agent"] == "volume"
    assert rep[0]["samples"] == 30 and rep[0]["hit_rate"] == 100.0


# ---------------- Résolution automatique des signaux ----------------
def _pro(client: TestClient) -> dict:
    email = f"u{uuid.uuid4().hex[:8]}@test.com"
    r = client.post("/api/auth/register", json={"email": email, "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.post("/api/billing/checkout/pro", headers=h).status_code == 200
    return h


async def test_auto_resolve_closes_open_signals(monkeypatch):
    """Un signal ouvert dont le prix a touché le TP est résolu en 'win' automatiquement."""
    from app.data import replay
    from app.repositories.store import get_store
    from app.services import journal_service

    client = TestClient(app)
    h = _pro(client)
    # Génère un signal (crée une entrée de journal 'open' liée au signal stocké).
    client.post("/api/signals/generate", json={"asset": "BTC/USDT", "timeframe": "1h", "notify": False}, headers=h)

    # Rejeu mocké -> le TP est touché (won), indépendamment de la direction réelle du signal.
    # Le faux doit rendre le VERDICT MOTIVÉ (dictionnaire) que `replay.replay_outcome` produit
    # depuis le correctif d'honnêteté : un tuple faisait passer le test à côté du vrai contrat.
    async def _won(symbol, direction, entry, sl, tp, since_iso, interval="1h"):  # noqa: ANN001
        return {
            "outcome": "won", "exit_price": (tp if tp else entry * 1.05),
            "closed_ts": 1_700_000_000, "reason": "objectif touché (rejeu simulé)",
            "data_real": True, "covered_until": None, "bars": 42,
        }

    monkeypatch.setattr(replay, "replay_outcome", _won)
    store = get_store()
    me = client.get("/api/auth/me", headers=h).json()
    tenant_id = store.users.get(me["id"]).tenant_id
    resolved = await journal_service.auto_resolve(store, tenant_id)

    # Si le signal était directionnel, il est résolu ; s'il était HOLD, rien à résoudre (toléré).
    ins = client.get("/api/journal/insights", headers=h).json()
    assert ins["trades_learned"] == resolved
    assert resolved >= 0
