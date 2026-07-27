"""Routage multi-marchés (Phase 2) : crypto / actions / forex.

Détermine la classe d'actif d'un symbole et charge les bougies via le bon connecteur :
- crypto  -> Binance (existant)
- actions -> Alpaca (si clé) sinon synthétique
- forex   -> OANDA (si clé) sinon synthétique

Tous les connecteurs dégradent gracieusement vers des données synthétiques hors-ligne.
"""

from __future__ import annotations

import logging

from app.core.config import get_settings
from app.data import binance
from app.data.synthetic import generate_candles
from app.domain.indicators import Candle

logger = logging.getLogger(__name__)

_FOREX = {"EUR", "USD", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"}
_CRYPTO_QUOTE = {"USDT", "USDC", "BTC", "ETH", "BUSD"}
_COMMODITY_BASES = {"XAU", "XAG", "XPT", "XPD"}  # or, argent, platine, palladium


def asset_class(symbol: str) -> str:
    if "/" in symbol:
        base, quote = symbol.split("/", 1)
        if base in _COMMODITY_BASES:
            return "commodity"  # métaux précieux (XAU/USD = or spot)
        if quote in _CRYPTO_QUOTE:
            return "crypto"
        if base in _FOREX and quote in _FOREX:
            return "forex"
        return "crypto"
    return "stock"  # ex. AAPL, TSLA


async def _alpaca_candles(symbol: str, interval: str, limit: int) -> list[Candle]:
    s = get_settings()
    if not s.alpaca_api_key:
        raise RuntimeError("pas de clé Alpaca")
    import httpx

    tf = {"5m": "5Min", "15m": "15Min", "1h": "1Hour", "4h": "4Hour", "1d": "1Day",
          "1w": "1Week", "1M": "1Month"}.get(interval, "1Hour")
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
    headers = {"APCA-API-KEY-ID": s.alpaca_api_key, "APCA-API-SECRET-KEY": s.alpaca_api_secret}
    # Sans `start`, Alpaca ne renvoie que la journée en cours (~7 bougies 1h) -> repli Yahoo forcé.
    # On remonte assez loin (marchés actions ~7h/jour, week-ends fermés) puis on tronque à `limit`.
    from datetime import UTC, datetime, timedelta

    _secs = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400,
             "1w": 604800, "1M": 2592000}.get(interval, 3600)
    start = (datetime.now(UTC) - timedelta(seconds=_secs * limit * 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"timeframe": tf, "limit": min(limit * 2, 1000), "start": start, "feed": "iex"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        bars = resp.json().get("bars", [])
    return [Candle(b["o"], b["h"], b["l"], b["c"], b["v"]) for b in bars][-limit:]


async def _oanda_candles(symbol: str, interval: str, limit: int) -> list[Candle]:
    s = get_settings()
    if not s.oanda_api_key:
        raise RuntimeError("pas de clé OANDA")
    import httpx

    gran = {"5m": "M5", "15m": "M15", "1h": "H1", "4h": "H4", "1d": "D",
            "1w": "W", "1M": "M"}.get(interval, "H1")
    instr = symbol.replace("/", "_")
    url = f"https://api-fxtrade.oanda.com/v3/instruments/{instr}/candles"
    headers = {"Authorization": f"Bearer {s.oanda_api_key}"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params={"granularity": gran, "count": limit, "price": "M"}, headers=headers)
        resp.raise_for_status()
        candles = resp.json().get("candles", [])
    out = []
    for c in candles:
        m = c["mid"]
        out.append(Candle(float(m["o"]), float(m["h"]), float(m["l"]), float(m["c"]), float(c.get("volume", 0))))
    return out


async def _yahoo_candles(symbol: str, interval: str, limit: int) -> list[Candle]:
    """Bougies réelles via Yahoo Finance (actions & forex, sans clé)."""
    from app.data import yahoo

    rows = await yahoo.fetch_ohlcv(symbol, interval, limit)
    return [Candle(r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]


# Source de données du DERNIER chargement par symbole (qualité des données).
# Valeurs : "live" (flux WS), "real" (REST réel), "synthetic" (repli factice).
_LAST_SOURCE: dict[str, str] = {}


def data_source(symbol: str) -> str:
    """Source du dernier chargement de `symbol` : 'live' | 'real' | 'synthetic' | 'unknown'."""
    return _LAST_SOURCE.get(symbol.upper(), "unknown")


def is_real(symbol: str) -> bool:
    """Vrai si les dernières données de `symbol` sont réelles (flux live ou REST), pas synthétiques."""
    return data_source(symbol) in {"live", "real"}


async def load_candles(symbol: str, interval: str = "1h", limit: int = 200) -> list[Candle]:
    """Charge les bougies réelles selon la classe d'actif, avec repli synthétique.

    crypto -> Binance ; actions -> Alpaca (si clé) sinon Yahoo ; forex -> OANDA (si clé) sinon Yahoo.
    Enregistre la source du chargement (`data_source`) pour signaler les données factices.
    """
    cls = asset_class(symbol)
    key = symbol.upper()
    # Minimum de bougies exploitables : les unités LONGUES (hebdo/mensuel, étape 1 du playbook) en
    # ont mécaniquement moins — exiger 60 bougies mensuelles enverrait tout vers le synthétique.
    min_needed = {"1M": 12, "1w": 30}.get(interval, 60)
    # Cache temps réel (crypto) : si le flux WS a chauffé le cache, on évite un appel REST.
    if cls == "crypto":
        from app.realtime import market_stream

        if market_stream.is_live(symbol, interval):
            cached = market_stream.get_cached(symbol, interval, limit)
            if cached and len(cached) >= min(limit, 60):
                _LAST_SOURCE[key] = "live"
                return cached
    try:
        if cls == "crypto":
            candles = await binance.fetch_klines(symbol, interval=interval, limit=limit)
        elif cls == "stock":
            try:
                candles = await _alpaca_candles(symbol, interval, limit)
                if len(candles) < min_needed:
                    candles = await _yahoo_candles(symbol, interval, limit)
            except Exception:  # noqa: BLE001 — pas de clé Alpaca -> Yahoo
                candles = await _yahoo_candles(symbol, interval, limit)
        elif cls == "forex":
            try:
                candles = await _oanda_candles(symbol, interval, limit)
                if len(candles) < min_needed:
                    candles = await _yahoo_candles(symbol, interval, limit)
            except Exception:  # noqa: BLE001 — pas de clé OANDA -> Yahoo
                candles = await _yahoo_candles(symbol, interval, limit)
        elif cls == "commodity":
            # Or/métaux : futures COMEX via Yahoo (GC=F/SI=F — réels, avec volume, sans clé).
            candles = await _yahoo_candles(symbol, interval, limit)
        else:
            candles = []
        if len(candles) >= min_needed:
            _LAST_SOURCE[key] = "real"
            return candles
        logger.warning("Backfill %s (%s) insuffisant", symbol, cls)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Connecteur %s indisponible pour %s (%s)", cls, symbol, exc)
    # Par défaut on ne FABRIQUE pas de bougies : une série vide est honnête, une série inventée ne
    # l'est pas. Les appelants (playbook, entraînement, backtest) refusent déjà d'agir sans données.
    from app.core.config import get_settings

    if not get_settings().data_allow_synthetic:
        _LAST_SOURCE[key] = "unavailable"
        return []
    # Repli déterministe (graine basée sur le symbole pour la cohérence par actif)
    _LAST_SOURCE[key] = "synthetic"
    return generate_candles(seed=abs(hash(symbol)) % 10_000)
