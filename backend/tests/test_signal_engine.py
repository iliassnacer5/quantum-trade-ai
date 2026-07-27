"""Test du Signal Engine de bout en bout (agents -> master -> risque -> SignalCard)."""

import pytest

from app.data.synthetic import generate_candles
from app.domain.risk import RiskParams
from app.models.signal import Direction, Timeframe
from app.signal_engine.engine import generate_signal

pytestmark = pytest.mark.asyncio


async def test_generate_signal_buy_has_valid_levels():
    candles = generate_candles(n=200, trend=0.004, seed=7)
    risk = RiskParams(capital=10000, risk_per_trade_pct=1.0)
    card = await generate_signal(
        asset="BTC/USDT", candles=candles, news=[], risk=risk, timeframe=Timeframe.H1
    )
    assert card.asset == "BTC/USDT"
    assert 0 <= card.confidence <= 100
    if card.direction == Direction.BUY:
        assert card.stop_loss < card.entry < card.take_profit_1
        assert card.risk_reward > 0
    assert "Master" in card.rationale


async def test_generate_signal_hold_neutral_levels():
    # Tendance quasi nulle -> souvent HOLD ; on vérifie la cohérence des champs.
    candles = generate_candles(n=200, trend=0.0, seed=3)
    risk = RiskParams(capital=5000)
    card = await generate_signal(asset="ETH/USDT", candles=candles, risk=risk)
    assert card.confidence >= 0
    assert card.entry > 0
