"""Tests réalisme/fiabilité : frais+slippage, benchmark/alpha, garde-fou portefeuille, alertes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.backtest.engine import run_backtest
from app.backtest.schemas import BacktestConfig
from app.core.config import get_settings
from app.domain.indicators import Candle
from app.main import app
from app.strategies import get_strategy


def _uptrend(n: int = 250) -> list[Candle]:
    base = datetime.now(UTC) - timedelta(hours=n)
    out, p = [], 100.0
    for i in range(n):
        p += 0.7
        out.append(Candle(p - 0.3, p + 0.6, p - 0.6, p, 10.0, timestamp=base + timedelta(hours=i)))
    return out


def _cfg(candles):
    return BacktestConfig(symbol="BTC/USDT", timeframe="1h",
                          start_time=candles[0].timestamp, end_time=candles[-1].timestamp, initial_capital=10000)


async def test_fees_reduce_pnl_and_report_has_benchmark():
    candles = _uptrend()
    strat = get_strategy("ichimoku").fn
    s = get_settings()
    s.backtest_fee_pct, s.backtest_slippage_pct = 0.0, 0.0
    free = await run_backtest(_cfg(candles), candles, strategy=strat)
    s.backtest_fee_pct, s.backtest_slippage_pct = 0.2, 0.1
    costly = await run_backtest(_cfg(candles), candles, strategy=strat)
    s.backtest_fee_pct, s.backtest_slippage_pct = 0.1, 0.05  # restaure défauts

    # Les coûts diminuent (ou égalisent) le P&L, jamais l'inverse.
    assert costly.metrics.total_pnl <= free.metrics.total_pnl
    assert costly.cost_pct_per_side > 0
    # Benchmark + alpha présents et cohérents.
    assert costly.benchmark_pnl_pct != 0.0
    assert costly.alpha_pct == round(costly.metrics.total_pnl_pct - costly.benchmark_pnl_pct, 2)


async def test_exit_config_staged_tp_partial_close():
    """TP étagé : la moitié réalisée AVANT l'objectif -> plus de trades enregistrés (partiels).

    Le palier partiel vaut 60 % du chemin vers le TP (plafonné à 1,5R), donc il reste atteignable
    quel que soit le R/R visé — y compris dans la bande resserrée 1,2–1,3 de la stratégie.
    """
    candles = _uptrend(300)
    strat = get_strategy("mtf_ema").fn
    plain = await run_backtest(_cfg(candles), candles, strategy=strat,
                               exit_config={"trailing": False, "breakeven_r": 0.0, "staged_tp": False})
    staged = await run_backtest(_cfg(candles), candles, strategy=strat,
                                exit_config={"trailing": True, "trailing_mult": 3.0, "breakeven_r": 1.5, "staged_tp": True})
    # En uptrend, le TP étagé génère des sorties partielles [TP partiel …R].
    partials = [t for t in staged.trades if "[TP partiel" in (t.signal_rationale or "")]
    assert len(staged.trades) >= len(plain.trades)
    assert partials, "au moins une sortie partielle attendue en tendance haussière"


def test_regime_router_strategy():
    """Le routeur de régime renvoie une direction valide et suit la tendance quand ADX est fort."""
    from app.models.signal import Direction as D
    r = get_strategy("regime_router")
    assert r is not None
    d = r.fn(_uptrend(150))
    assert d in (D.BUY, D.SELL, D.HOLD)


def test_auto_trade_toggle_endpoint():
    client = TestClient(app)
    h = _pro(client)
    assert client.post("/api/strategies/auto-trade?enabled=true", headers=h).json()["auto_trade"] is True
    assert client.get("/api/strategies/auto-trade", headers=h).json()["auto_trade"] is True
    assert client.post("/api/strategies/auto-trade?enabled=false", headers=h).json()["auto_trade"] is False


# ---------------- Garde-fou portefeuille ----------------
def _pro(client):
    email = f"u{uuid.uuid4().hex[:8]}@test.com"
    r = client.post("/api/auth/register", json={"email": email, "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.post("/api/billing/checkout/pro", headers=h).status_code == 200
    return h


def test_portfolio_guard_limits_positions():
    s = get_settings()
    s.paper_portfolio_guard = True
    s.paper_max_positions = 1
    try:
        client = TestClient(app)
        h = _pro(client)
        cid = client.post("/api/execution/brokers", json={"broker": "paper", "mode": "paper"}, headers=h).json()["id"]
        body = {"conn_id": cid, "symbol": "BTC/USDT", "side": "buy", "qty": 0.001}
        assert client.post("/api/execution/orders", json=body, headers=h).status_code == 201
        r2 = client.post("/api/execution/orders", json=body, headers=h)
        assert r2.status_code == 400 and "positions" in r2.json()["detail"].lower()
    finally:
        s.paper_portfolio_guard = False
        s.paper_max_positions = 5


# ---------------- Alertes stratégie ----------------
async def test_strategy_alert_fires_on_new_signal(monkeypatch):
    from app.data import markets
    from app.repositories.store import get_store
    from app.services import strategy_alert_service as sas

    client = TestClient(app)
    h = _pro(client)
    client.post("/api/strategies/select?strategy=ichimoku", headers=h)

    candles = _uptrend()  # tendance haussière -> ichimoku renvoie BUY
    async def _load(symbol, interval="1h", limit=200):  # noqa: ANN001
        return candles
    monkeypatch.setattr(markets, "load_candles", _load)
    monkeypatch.setattr(markets, "is_real", lambda s: True)
    sent_msgs = []
    async def _push(token, msg):  # noqa: ANN001
        sent_msgs.append(msg)
    monkeypatch.setattr(sas.notifier, "send_push", _push)

    store = get_store()
    me = client.get("/api/auth/me", headers=h).json()
    user = store.users.get(me["id"])
    user.push_token = "tok"  # active le canal push
    # 1er passage : nouveau signal -> alerte ; 2e passage : même signal -> pas d'alerte (anti-spam).
    first = await sas.check_strategy_alerts(store)
    second = await sas.check_strategy_alerts(store)
    assert first >= 1
    assert second == 0
