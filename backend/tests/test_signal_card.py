"""Validation du schéma SignalCard + consultation détaillée d'une prédiction."""

import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.models.signal import Direction, SignalCard, Timeframe


def test_prediction_detail_endpoint():
    """La prédiction est consultable en détail (id retourné, endpoint, isolation tenant)."""
    client = TestClient(app)

    def reg():
        r = client.post("/api/auth/register", json={"email": f"u{uuid.uuid4().hex[:8]}@t.co", "password": "password123"})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    h = reg()
    sig = client.post("/api/signals/generate", json={"asset": "BTC/USDT", "timeframe": "1h", "notify": False}, headers=h).json()
    assert sig.get("id"), "la réponse doit porter l'id de la prédiction"
    detail = client.get(f"/api/signals/{sig['id']}", headers=h)
    assert detail.status_code == 200
    body = detail.json()
    assert body["asset"] == "BTC/USDT" and "agents" in body and "news" in body and "metrics" in body
    # Isolation : un AUTRE tenant ne peut pas consulter cette prédiction.
    h2 = reg()
    assert client.get(f"/api/signals/{sig['id']}", headers=h2).status_code == 404


def test_every_prediction_can_be_dated():
    """Une prédiction porte TOUJOURS sa date de calcul, sur les trois chemins qui la servent.

    Les cartes sont persistées puis réaffichées telles quelles, narration comprise — et celle-ci
    parle au présent (« Fenêtre actuelle : session asiatique (23:02 UTC) »). Sans `created_at`,
    l'interface ne peut pas distinguer une analyse de l'instant d'une analyse de la veille, et la
    session décrite n'est alors plus celle du marché.
    """
    from datetime import datetime

    client = TestClient(app)
    r = client.post("/api/auth/register",
                    json={"email": f"u{uuid.uuid4().hex[:8]}@t.co", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    generated = client.post("/api/signals/generate",
                            json={"asset": "BTC/USDT", "timeframe": "1h", "notify": False},
                            headers=h).json()
    detail = client.get(f"/api/signals/{generated['id']}", headers=h).json()
    listed = next(s for s in client.get("/api/signals", headers=h).json()
                  if s.get("id") == generated["id"])

    for source, payload in (("généré", generated), ("détail", detail), ("liste", listed)):
        stamp = payload.get("created_at")
        assert stamp, f"la prédiction servie par « {source} » doit porter sa date de calcul"
        # Horodatage lisible ET porteur d'un fuseau : sans lui, l'interface daterait l'analyse dans
        # le fuseau du navigateur et afficherait un décalage silencieux.
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        assert parsed.tzinfo is not None, f"« {source} » : date sans fuseau horaire"


def test_blocked_trade_is_memorized():
    """Un signal gaté vers HOLD mémorise la direction/niveaux bloqués (base des « trades évités »)."""
    from app.services.signal_service import finalize_decision

    card = SignalCard(asset="BTC/USDT", direction=Direction.BUY, entry=100, stop_loss=98,
                      take_profit_1=104, risk_reward=2.0, confidence=80, timeframe=Timeframe.H1,
                      rationale="x", consensus_pct=80, metrics={"adx": 30})
    finalize_decision(card, {"aligned": 0, "total": 3})  # MTF non aligné -> HOLD
    assert card.direction == Direction.HOLD
    assert card.metrics["blocked_direction"] == "BUY"
    assert card.metrics["blocked_sl"] == 98 and card.metrics["blocked_tp"] == 104
    assert card.metrics["blocked_entry"] == 100


def test_a_hold_card_carries_no_levels_at_all():
    """PAS DE TRADE = PAS DE NIVEAUX. Ni le prix courant recopié, ni un R/R de 0.

    Les niveaux étaient aplatis sur le prix d'entrée quand un signal était bloqué : la carte
    affichait « Entrée 100 · Stop 100 · TP 100 · R/R 1:0 », trois niveaux d'apparence exploitable
    qui ne veulent rien dire. Ils vivent désormais dans `blocked_*` (pour mesurer ce que les filtres
    ont évité) et valent `None` sur la carte.
    """
    from app.services.signal_service import finalize_decision

    card = SignalCard(asset="BTC/USDT", direction=Direction.BUY, entry=100, stop_loss=98,
                      take_profit_1=104, risk_reward=2.0, confidence=80, timeframe=Timeframe.H1,
                      rationale="x", consensus_pct=80, metrics={"adx": 30})
    finalize_decision(card, {"aligned": 0, "total": 3})

    assert card.direction == Direction.HOLD
    assert card.entry is None, "un HOLD ne propose pas d'entrée"
    assert card.stop_loss is None and card.take_profit_1 is None
    assert card.risk_reward is None, "un R/R de 0 se lit comme une mesure ; il n'y en a pas"
    # ...mais ce qui a été bloqué reste mesurable après coup.
    assert card.metrics["blocked_entry"] == 100 and card.metrics["blocked_sl"] == 98


def test_track_record_endpoint(monkeypatch):
    """Le track record renvoie les issues observées + les trades évités (rejeu mocké)."""
    from app.data import replay

    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": f"u{uuid.uuid4().hex[:8]}@t.co", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    client.post("/api/signals/generate", json={"asset": "BTC/USDT", "timeframe": "1h", "notify": False}, headers=h)

    async def _lost(*a, **k):
        return "lost", 1.0, 1_700_000_000
    monkeypatch.setattr(replay, "replay_outcome", _lost)
    body = client.get("/api/signals/track-record", headers=h).json()
    assert "observed" in body and "avoided" in body
    assert set(body["avoided"]) == {"blocked", "would_have_lost", "would_have_won", "undecided"}


def test_prediction_contains_full_decision_details():
    """La prédiction stocke la pesée du Master (poids/score/seuil) et les détails structurés des agents."""
    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": f"u{uuid.uuid4().hex[:8]}@t.co", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    sig = client.post("/api/signals/generate", json={"asset": "BTC/USDT", "timeframe": "1h", "notify": False}, headers=h).json()
    detail = client.get(f"/api/signals/{sig['id']}", headers=h).json()
    md = detail["metrics"].get("master_decision")
    assert md and "weights_used" in md and "score" in md and md["threshold"] == 0.12
    # Chaque agent expose ses détails structurés (au moins le champ, même vide).
    assert all("details" in a for a in detail["agents"])
    pattern_agent = next(a for a in detail["agents"] if a["name"] == "pattern")
    assert "patterns" in pattern_agent["details"]


def test_signal_modes_change_strictness():
    """Le même setup passe en 'balanced' mais est filtré en 'strict' (curseur fiabilité/quantité)."""
    from app.services.signal_service import finalize_decision

    def make(rr: float = 2.2):
        return SignalCard(asset="BTC/USDT", direction=Direction.BUY, entry=100, stop_loss=98,
                          take_profit_1=104.4, risk_reward=rr, confidence=55, timeframe=Timeframe.H1,
                          rationale="x", consensus_pct=70,
                          metrics={"adx": 20, "price": 100, "ema200": 99})
    strict = finalize_decision(make(), {"aligned": 2, "total": 3}, mode="strict")
    balanced = finalize_decision(make(), {"aligned": 2, "total": 3}, mode="balanced")
    assert strict.direction == Direction.HOLD       # conf 55<62, ADX 20<22
    assert balanced.direction == Direction.BUY      # conf 55>=52, ADX 20>=18, RR 2.2>=2.0


def test_risk_reward_floor_applies_to_every_mode():
    """La stratégie impose R/R ≥ 1:1,2 : aucun mode, même agressif, ne laisse passer moins."""
    from app.services.signal_service import finalize_decision

    def make(rr: float):
        return SignalCard(asset="BTC/USDT", direction=Direction.BUY, entry=100, stop_loss=98,
                          take_profit_1=102, risk_reward=rr, confidence=90, timeframe=Timeframe.H1,
                          rationale="x", consensus_pct=90,
                          metrics={"adx": 35, "price": 100, "ema200": 99})
    for mode in ("strict", "balanced", "aggressive"):
        refused = finalize_decision(make(1.0), {"aligned": 3, "total": 3}, mode=mode)
        assert refused.direction == Direction.HOLD, f"R/R 1,0 doit être refusé en mode {mode}"
        assert "R/R" in refused.rationale
        # Dans la bande de la stratégie, le setup passe.
        accepted = finalize_decision(make(2.0), {"aligned": 3, "total": 3}, mode=mode)
        assert accepted.direction == Direction.BUY, f"R/R 1:2 doit passer en mode {mode}"


def test_signal_mode_endpoint():
    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": f"u{uuid.uuid4().hex[:8]}@t.co", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/api/signals/mode", headers=h).json()["mode"] == "strict"
    assert client.post("/api/signals/mode?mode=balanced", headers=h).json()["mode"] == "balanced"
    assert client.get("/api/signals/mode", headers=h).json()["mode"] == "balanced"
    assert client.post("/api/signals/mode?mode=nimporte", headers=h).status_code == 400


def test_signal_card_valid():
    card = SignalCard(
        asset="BTC/USDT",
        direction=Direction.BUY,
        entry=64250,
        stop_loss=62800,
        take_profit_1=66000,
        take_profit_2=68500,
        take_profit_3=71000,
        risk_reward=3.2,
        confidence=82,
        timeframe=Timeframe.H1,
        rationale="Cassure de résistance + sentiment positif + momentum haussier",
    )
    assert card.direction == Direction.BUY
    assert 0 <= card.confidence <= 100


def test_confidence_out_of_range():
    with pytest.raises(ValidationError):
        SignalCard(
            asset="BTC/USDT",
            direction=Direction.BUY,
            entry=1,
            stop_loss=1,
            take_profit_1=1,
            risk_reward=1,
            confidence=150,  # invalide
            timeframe=Timeframe.H1,
            rationale="x",
        )
