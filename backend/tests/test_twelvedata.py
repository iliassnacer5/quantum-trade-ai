"""Twelve Data — FILET DE SÉCURITÉ pour les bougies, jamais une source primaire.

Ce que ces tests garantissent, dans l'ordre des risques :

1. **Le quota est respecté.** ~8 requêtes/minute sur le plan gratuit, contre 420 par cycle de
   balayage : sans régulateur, le crédit part en quelques secondes.
2. **Rien n'est jamais inventé.** Quota atteint, symbole hors plan, réponse illisible -> série vide.
3. **L'ordre chronologique est rétabli.** Twelve Data rend du plus RÉCENT au plus ancien ; servir
   la série telle quelle inverserait toutes les lectures d'indicateurs.
4. **Il reste le DERNIER de la cascade** : un fournisseur primaire qui répond doit lui être préféré.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.data import twelvedata

pytestmark = pytest.mark.asyncio


def test_symbols_are_mapped_and_paid_ones_are_refused():
    """La crypto est cotée en USD chez Twelve Data ; les symboles hors plan ne coûtent aucun crédit."""
    assert twelvedata.to_symbol("BTC/USDT") == "BTC/USD"
    assert twelvedata.to_symbol("EUR/USD") == "EUR/USD"
    assert twelvedata.to_symbol("AAPL") == "AAPL"
    assert twelvedata.to_symbol("XAU/USD") == "XAU/USD"
    # Mesurés « Grow ou Venture » requis : inutile de dépenser un crédit pour se faire refuser.
    assert twelvedata.to_symbol("XAG/USD") is None
    assert twelvedata.to_symbol("GER40") is None
    assert twelvedata.to_symbol("SPX500") is None


async def test_the_quota_is_enforced_before_spending_a_credit():
    """Au-delà du quota de la minute, on REFUSE au lieu de brûler un crédit dans un 429."""
    s = get_settings()
    precedent = s.twelve_data_max_rpm
    s.twelve_data_max_rpm = 3
    twelvedata.reset()
    try:
        assert [await twelvedata._slot() for _ in range(3)] == [True, True, True]  # noqa: SLF001
        assert await twelvedata._slot() is False, "le 4e appel doit être refusé"    # noqa: SLF001
    finally:
        s.twelve_data_max_rpm = precedent
        twelvedata.reset()


async def test_nothing_is_fetched_without_a_key():
    """Sans clé, série vide — jamais de repli inventé."""
    s = get_settings()
    precedent = s.twelve_data_api_key
    s.twelve_data_api_key = ""
    try:
        assert await twelvedata.fetch_ohlcv("EUR/USD", "1h", 100) == []
    finally:
        s.twelve_data_api_key = precedent


async def test_a_symbol_outside_the_plan_costs_no_credit(monkeypatch):
    """Un symbole hors plan ne doit consommer NI crédit NI appel réseau."""
    s = get_settings()
    precedent = s.twelve_data_api_key
    s.twelve_data_api_key = "cle-de-test"
    twelvedata.reset()
    appels = []
    monkeypatch.setattr(twelvedata, "_slot", lambda: appels.append(1) or True)
    try:
        assert await twelvedata.fetch_ohlcv("XAG/USD", "1h", 100) == []
        assert appels == [], "aucun crédit ne doit être réservé pour un symbole hors plan"
    finally:
        s.twelve_data_api_key = precedent
        twelvedata.reset()


async def test_candles_come_back_in_chronological_order(monkeypatch):
    """Twelve Data rend du plus RÉCENT au plus ancien : l'ordre doit être rétabli.

    Servie telle quelle, la série inverserait toutes les moyennes mobiles, le RSI, le MACD — la
    stratégie lirait le passé comme le présent.
    """
    import httpx

    s = get_settings()
    precedent = s.twelve_data_api_key
    s.twelve_data_api_key = "cle-de-test"
    twelvedata.reset()

    charge = {"values": [  # du plus récent au plus ancien, comme l'API réelle
        {"datetime": "2026-08-01 12:00:00", "open": "3", "high": "3", "low": "3", "close": "3"},
        {"datetime": "2026-08-01 11:00:00", "open": "2", "high": "2", "low": "2", "close": "2"},
        {"datetime": "2026-08-01 10:00:00", "open": "1", "high": "1", "low": "1", "close": "1"},
    ]}

    class _Rep:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return charge

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _Rep()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    try:
        rows = await twelvedata.fetch_ohlcv("EUR/USD", "1h", 100)
        assert [r["close"] for r in rows] == [1.0, 2.0, 3.0], "l'ordre doit être chronologique"
        assert rows[0]["time"] < rows[-1]["time"]
    finally:
        s.twelve_data_api_key = precedent
        twelvedata.reset()


async def test_it_is_only_a_last_resort_in_the_cascade(monkeypatch):
    """Un fournisseur primaire qui répond doit être préféré : le quota reste pour les urgences."""
    from app.data import markets
    from app.domain.indicators import Candle

    appels = {"twelve": 0}

    async def _twelve(symbol, interval, limit):  # noqa: ANN001
        appels["twelve"] += 1
        return [Candle(1.0, 1.0, 1.0, 1.0, 1.0) for _ in range(80)]

    async def _yahoo_ok(symbol, interval, limit, **kw):  # noqa: ANN001
        return [Candle(2.0, 2.0, 2.0, 2.0, 2.0) for _ in range(80)]

    monkeypatch.setattr(markets, "_twelve_candles", _twelve)
    monkeypatch.setattr(markets, "_yahoo_candles", _yahoo_ok)
    monkeypatch.setattr(markets, "_oanda_candles",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pas de clé")))
    markets.clear_cache()

    candles = await markets.load_candles("EUR/USD", "1h", 80)
    assert candles and candles[0].close == 2.0, "Yahoo a répondu : il doit primer"
    assert appels["twelve"] == 0, "Twelve Data ne doit pas être interrogé si un autre a servi"
