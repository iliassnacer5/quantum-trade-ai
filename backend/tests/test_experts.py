"""Tests Phase 1 : expert crypto, routeur technical, filtre événementiel, gate blackout."""

from __future__ import annotations

import pytest

from app.agents import crypto_expert, technical
from app.core.config import get_settings
from app.domain.indicators import Candle
from app.models.signal import Direction


def _trend(n: int = 120, up: bool = True) -> list[Candle]:
    out, p = [], 100.0
    for _ in range(n):
        p += 0.8 if up else -0.8
        out.append(Candle(p - 0.3, p + 0.6, p - 0.6, p, 100.0))
    return out


# ---------------- Expert crypto ----------------
async def test_crypto_expert_runs_and_uses_funding(monkeypatch):
    async def _funding(symbol):  # funding élevé -> biais contrarien baissier
        return 0.002
    async def _btc():
        return 0.5
    monkeypatch.setattr(crypto_expert.cross_asset, "get_funding_rates", _funding)
    monkeypatch.setattr(crypto_expert.cross_asset, "get_btc_lead", _btc)

    out = await crypto_expert.run(_trend(up=True), symbol="ETH/USDT")
    assert out.name == "technical"  # même nom -> compatible Master/Journal
    assert "expert crypto" in out.rationale.lower()
    assert "funding" in out.rationale.lower()
    assert -1.0 <= out.score <= 1.0


async def test_crypto_expert_graceful_when_data_down(monkeypatch):
    async def _none(*a, **k):
        return None
    monkeypatch.setattr(crypto_expert.cross_asset, "get_funding_rates", _none)
    monkeypatch.setattr(crypto_expert.cross_asset, "get_btc_lead", _none)
    out = await crypto_expert.run(_trend(up=False), symbol="SOL/USDT")  # pas de crash
    assert out.name == "technical" and -1.0 <= out.score <= 1.0


# ---------------- Routeur ----------------
async def test_router_crypto_to_expert(monkeypatch):
    get_settings().expert_agents_enabled = True
    async def _none(*a, **k):
        return None
    monkeypatch.setattr(crypto_expert.cross_asset, "get_funding_rates", _none)
    monkeypatch.setattr(crypto_expert.cross_asset, "get_btc_lead", _none)
    out = await technical.run(_trend(), symbol="BTC/USDT", context={"market_type": "crypto"})
    get_settings().expert_agents_enabled = False
    assert "expert crypto" in out.rationale.lower()


async def test_router_forex_to_expert(monkeypatch):
    from app.agents import forex_expert
    get_settings().expert_agents_enabled = True
    async def _dxy():
        return 0.5
    monkeypatch.setattr(forex_expert.cross_asset, "get_dxy_signal", _dxy)
    out = await technical.run(_trend(), symbol="EUR/USD", context={"market_type": "forex"})
    get_settings().expert_agents_enabled = False
    assert "expert forex" in out.rationale.lower() and out.details.get("expert") is True


async def test_router_stock_to_expert(monkeypatch):
    from app.agents import stocks_expert
    get_settings().expert_agents_enabled = True
    async def _regime():
        return "risk_off"
    monkeypatch.setattr(stocks_expert.cross_asset, "get_spx_regime", _regime)
    out = await technical.run(_trend(up=True), symbol="AAPL", context={"market_type": "stock"})
    get_settings().expert_agents_enabled = False
    assert "expert actions" in out.rationale.lower() and out.details.get("spx_regime") == "risk_off"


# ---------------- Marché OR (commodity) ----------------
def test_asset_class_commodity():
    from app.data.markets import asset_class
    assert asset_class("XAU/USD") == "commodity"
    assert asset_class("XAG/USD") == "commodity"
    assert asset_class("EUR/USD") == "forex"        # non régressé
    assert asset_class("BTC/USDT") == "crypto"      # non régressé


def test_yahoo_gold_mapping():
    from app.data.yahoo import to_yahoo_symbol
    assert to_yahoo_symbol("XAU/USD") == "GC=F"
    assert to_yahoo_symbol("XAG/USD") == "SI=F"
    assert to_yahoo_symbol("EUR/USD") == "EURUSD=X"  # non régressé


async def test_gold_expert_applies_gold_drivers(monkeypatch):
    """Dollar fort + taux en hausse -> biais baissier or ; VIX élevé -> soutien refuge. Tout tracé."""
    from app.agents import gold_expert

    async def _dxy():
        return 0.8   # dollar très fort -> baissier or
    async def _macro():
        return {"vix": 30.0, "rate_trend": "up", "inflation": 2.0}
    monkeypatch.setattr(gold_expert.cross_asset, "get_dxy_signal", _dxy)
    monkeypatch.setattr(gold_expert.cross_asset, "get_macro_snapshot", _macro)

    out = await gold_expert.run(_trend(up=True), symbol="XAU/USD")
    assert out.name == "technical" and out.details.get("expert") is True
    assert "expert or" in out.rationale.lower()
    assert "dollar" in out.rationale.lower() and "taux" in out.rationale.lower()
    assert out.details.get("dxy") == 0.8 and out.details.get("vix") == 30.0


async def test_gold_expert_graceful_without_data(monkeypatch):
    from app.agents import gold_expert

    async def _none(*a, **k):
        return None
    async def _empty():
        return {"vix": None, "rate_trend": None, "inflation": None}
    monkeypatch.setattr(gold_expert.cross_asset, "get_dxy_signal", _none)
    monkeypatch.setattr(gold_expert.cross_asset, "get_macro_snapshot", _empty)
    out = await gold_expert.run(_trend(up=False), symbol="XAU/USD")
    assert -1.0 <= out.score <= 1.0  # pas de crash, repli gracieux


async def test_router_commodity_to_gold_expert(monkeypatch):
    from app.agents import gold_expert

    get_settings().expert_agents_enabled = True
    async def _none(*a, **k):
        return None
    async def _empty():
        return {"vix": None, "rate_trend": None, "inflation": None}
    monkeypatch.setattr(gold_expert.cross_asset, "get_dxy_signal", _none)
    monkeypatch.setattr(gold_expert.cross_asset, "get_macro_snapshot", _empty)
    out = await technical.run(_trend(), symbol="XAU/USD", context={"market_type": "commodity"})
    get_settings().expert_agents_enabled = False
    assert "expert or" in out.rationale.lower()


# ---------------- Phase 3 : apprentissage par marché + scores qualité ----------------
def test_quality_scores_present():
    from app.models.signal import SignalCard, Timeframe
    from app.signal_engine import quality
    card = SignalCard(asset="BTC/USDT", direction=Direction.BUY, entry=100, stop_loss=98,
                      take_profit_1=104, risk_reward=2.0, confidence=80, timeframe=Timeframe.H1,
                      rationale="x", consensus_pct=80, metrics={"adx": 30, "atr_pct": 1.2, "adx_state": "tendance forte"},
                      mtf={"aligned": 3, "total": 3})
    assert 0 <= quality.context_score(card) <= 100
    assert 0 <= quality.timing_score(card) <= 100


def test_master_expert_bonus():
    """Un agent expert (details.expert=True) pèse plus que le même agent générique."""
    from app.agents.base import AgentOutput
    from app.agents.master import decide
    base = [AgentOutput("technical", 0.5, 1.0, "t"), AgentOutput("sentiment", -0.5, 1.0, "s")]
    expert = [AgentOutput("technical", 0.5, 1.0, "t", details={"expert": True}), AgentOutput("sentiment", -0.5, 1.0, "s")]
    # Avec le bonus expert, le score penche davantage vers le technical (+0.5).
    assert decide(expert).score > decide(base).score


# ---------------- Calendrier économique ----------------
async def test_blackout_fomc_window(monkeypatch):
    from datetime import UTC, datetime

    from app.data import economic_calendar
    s = get_settings()
    s.event_blackout_enabled = True
    s.fomc_dates = datetime.now(UTC).date().isoformat()  # FOMC aujourd'hui -> blackout
    try:
        bo, reason = await economic_calendar.is_news_blackout("BTC/USDT", "crypto")
        assert bo and "FOMC" in reason
    finally:
        s.event_blackout_enabled = False
        s.fomc_dates = ""


async def test_blackout_disabled_returns_false():
    from app.data import economic_calendar
    assert await economic_calendar.is_news_blackout("BTC/USDT", "crypto") == (False, "")


# ---------------- Gate blackout dans finalize_decision ----------------
def test_finalize_decision_blackout_forces_hold():
    from app.models.signal import SignalCard, Timeframe
    from app.services.signal_service import finalize_decision

    card = SignalCard(asset="BTC/USDT", direction=Direction.BUY, entry=100, stop_loss=98,
                      take_profit_1=104, risk_reward=2.0, confidence=80, timeframe=Timeframe.H1,
                      rationale="x", consensus_pct=80, metrics={"adx": 30})
    finalize_decision(card, {"aligned": 3, "total": 3}, blackout=(True, "earnings AAPL"))
    assert card.direction == Direction.HOLD and "EVENT_LOCK" in card.rationale
    assert card.high_conviction is False
