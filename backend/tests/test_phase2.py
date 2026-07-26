"""Tests Phase 2 : agents (pattern/volume/macro/fundamental/risk/journal), LLM router,
backtesting (moteur + endpoint), alertes multicanal."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.agents import fundamental, journal, macro, pattern, risk_agent, volume
from app.agents import llm
from app.backtest.engine import run_backtest
from app.backtest.schemas import BacktestConfig
from app.core.config import get_settings
from app.data.synthetic import generate_candles
from app.domain.indicators import Candle
from app.main import app

pytestmark_async = pytest.mark.asyncio


# ---------------- Agents ----------------
@pytest.mark.asyncio
async def test_pattern_agent():
    out = await pattern.run(generate_candles(n=120, trend=0.003, seed=2))
    assert out.name == "pattern"
    assert -1.0 <= out.score <= 1.0


@pytest.mark.asyncio
async def test_volume_agent():
    out = await volume.run(generate_candles(n=120, seed=5))
    assert out.name == "volume"
    assert 0.0 <= out.confidence <= 1.0


@pytest.mark.asyncio
async def test_macro_agent_regime():
    out = await macro.run({"rate_trend": "down", "inflation": 2.0, "vix": 12})
    assert out.score > 0  # contexte favorable au risque


@pytest.mark.asyncio
async def test_fundamental_crypto_neutral():
    out = await fundamental.run("BTC/USDT", None)
    assert out.score == 0.0  # pas de fondamentaux crypto


def test_fundamental_ratios_scoring():
    score, notes = fundamental.score_ratios({"pe": 12, "revenue_growth": 0.2, "debt_to_equity": 0.5, "net_margin": 0.15})
    assert score > 0 and notes


def test_risk_agent_penalty():
    out = risk_agent.run_sync(exposure_pct=80, drawdown_pct=20, correlation=0.9)
    assert out.details["penalty"] > 0
    assert out.confidence < 1.0


def test_journal_multipliers():
    entries = [
        {"outcome": "win", "direction": "BUY", "agent_scores": {"technical": 0.5}},
        {"outcome": "win", "direction": "BUY", "agent_scores": {"technical": 0.5}},
        {"outcome": "win", "direction": "BUY", "agent_scores": {"technical": 0.5}},
    ]
    mult = journal.compute_weight_multipliers(entries)
    assert mult["technical"] > 1.0  # agent fiable -> poids augmenté


# ---------------- LLM router ----------------
def test_llm_router_offline():
    s = get_settings()
    s.anthropic_api_key = ""
    s.google_api_key = ""
    assert llm.available() is False
    assert llm.route("master") is None


def test_llm_router_selects_provider(monkeypatch):
    s = get_settings()
    s.google_api_key = "test-key"
    s.anthropic_api_key = ""
    try:
        # rôle 'fast' -> modèle gemini, dispo car clé google
        assert "gemini" in (llm.route("fast") or "")
        assert llm.available() is True
    finally:
        s.google_api_key = ""


@pytest.mark.asyncio
async def test_llm_failover(monkeypatch):
    s = get_settings()
    s.google_api_key = "k"
    s.anthropic_api_key = ""
    calls = {"n": 0}

    async def fake_acompletion(model, messages, api_key, max_tokens):
        calls["n"] += 1
        return "réponse simulée"

    monkeypatch.setattr("app.agents.llm._acompletion", fake_acompletion)
    try:
        out = await llm.complete("test", role="fast")
        assert out == "réponse simulée" and calls["n"] == 1
    finally:
        s.google_api_key = ""


# ---------------- Backtesting ----------------
@pytest.mark.asyncio
async def test_backtest_engine():
    base = datetime.now(UTC) - timedelta(hours=300)
    candles = [
        Candle(c.open, c.high, c.low, c.close, c.volume, timestamp=base + timedelta(hours=i))
        for i, c in enumerate(generate_candles(n=300, trend=0.002, seed=9))
    ]
    config = BacktestConfig(
        symbol="BTC/USDT", timeframe="1h",
        start_time=base, end_time=datetime.now(UTC), initial_capital=10000,
    )
    report = await run_backtest(config, candles, tenant_id="t1")
    assert report.tenant_id == "t1"
    assert report.metrics.total_trades >= 0
    assert len(report.equity_curve) > 0
    assert report.metrics.max_drawdown_pct >= 0


@pytest.mark.asyncio
async def test_backtest_no_llm_when_disabled(monkeypatch):
    """Régression : un backtest `use_llm=False` ne doit JAMAIS appeler le LLM,
    même si une clé est présente (sinon : 1 appel réseau par bougie -> hang de plusieurs minutes)."""
    s = get_settings()
    s.google_api_key = "k"
    s.llm_enabled = True

    async def boom(*a, **k):
        raise AssertionError("le LLM ne doit pas être appelé en backtest déterministe")

    monkeypatch.setattr("app.agents.llm._acompletion", boom)
    base = datetime.now(UTC) - timedelta(hours=200)
    candles = [
        Candle(c.open, c.high, c.low, c.close, c.volume, timestamp=base + timedelta(hours=i))
        for i, c in enumerate(generate_candles(n=200, trend=0.002, seed=3))
    ]
    config = BacktestConfig(
        symbol="BTC/USDT", timeframe="1h",
        start_time=base, end_time=datetime.now(UTC), initial_capital=10000, use_llm=False,
    )
    try:
        report = await run_backtest(config, candles, tenant_id="t1")
        assert report.metrics.total_trades >= 0
        # l'état global doit être restauré après le run
        assert get_settings().llm_enabled is True
    finally:
        s.google_api_key = ""
        s.llm_enabled = True


def test_backtest_endpoint(monkeypatch):
    async def fake_ohlcv(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr("app.data.ohlcv._binance_ohlcv", fake_ohlcv)
    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": "bt@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    client.post("/api/billing/checkout/pro", headers=h)  # backtest = feature Pro (Phase 3 gating)
    now = datetime.now(UTC)
    cfg = {
        "symbol": "BTC/USDT", "timeframe": "1h",
        "start_time": (now - timedelta(days=20)).isoformat(), "end_time": now.isoformat(),
        "initial_capital": 10000,
    }
    resp = client.post("/api/backtest/run", json=cfg, headers=h)
    assert resp.status_code == 200
    body = resp.json()
    assert "metrics" in body and "equity_curve" in body
    # historique
    assert client.get("/api/backtest/reports", headers=h).status_code == 200


# ---------------- Agents status endpoint ----------------
def test_agents_status_endpoint():
    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": "ag@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    resp = client.get("/api/agents/status", headers=h)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "online"
    # 9 agents : les 8 historiques + le PLAYBOOK (stratégie du desk, avec droit de veto).
    assert len(data["agents"]) == 9
    names = [a["name"] for a in data["agents"]]
    assert names[0] == "playbook"  # agent chef de file
    assert data["strategy"]["min_risk_reward"] == 1.2
    assert data["strategy"]["max_risk_reward"] == 1.3
    assert data["strategy"]["min_target_pips"] == 200.0
    assert data["strategy"]["entry_timeframe"] == "15m"
    assert len(data["strategy"]["steps"]) == 4
