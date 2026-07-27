"""Connecteur Yahoo Finance (gratuit, sans clé) — données réelles actions & forex.

Fournit l'OHLCV pour les actions (AAPL, TSLA…) et le forex (EUR/USD…) que Binance ne couvre pas.
Utilisé par les deux chemins de données : le graphique (get_ohlcv) et la génération de signal
(markets.load_candles). Dégrade gracieusement (lève en cas d'échec -> repli synthétique en amont).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; QuantumTradeAI/1.0)"}

# Yahoo ne propose pas 4h : on rabat sur 1h (position trading reste pertinent).
# "1M" = MENSUEL (étape 1 du playbook) — 10 ans d'historique pour des niveaux majeurs solides.
_INTERVAL = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "1h", "1d": "1d", "1w": "1wk", "1M": "1mo"}
# Plage par défaut : assez pour l'analyse courante, sans télécharger des années inutilement.
_RANGE = {"5m": "5d", "15m": "1mo", "1h": "3mo", "4h": "3mo", "1d": "2y", "1w": "5y", "1M": "10y"}

# Plage MAXIMALE réellement servie par Yahoo, par intervalle. Mesuré, pas supposé : au-delà, l'API
# répond 422 « data not available for startTime=… ». C'est ce qui borne la profondeur d'un backtest
# intraday — le 15 min ne remonte pas au-delà de ~60 jours, quoi qu'on demande.
_RANGE_DEEP = {"5m": "60d", "15m": "60d", "1h": "730d", "4h": "730d",
               "1d": "10y", "1w": "10y", "1M": "10y"}
# Au-delà de ce nombre de bougies demandées, on bascule sur la plage maximale (mode backtest).
_DEEP_THRESHOLD = 1200


def _range_for(interval: str, limit: int) -> str:
    """Plage à demander : la plus courte qui couvre `limit` bougies, pour ne pas surcharger l'API."""
    if limit >= _DEEP_THRESHOLD:
        return _RANGE_DEEP.get(interval, _RANGE.get(interval, "3mo"))
    return _RANGE.get(interval, "3mo")


# Métaux précieux -> futures COMEX Yahoo (réels, AVEC volume, sans clé).
_COMMODITY_MAP = {"XAU/USD": "GC=F", "XAG/USD": "SI=F", "XPT/USD": "PL=F", "XPD/USD": "PA=F"}


def to_yahoo_symbol(symbol: str) -> str:
    """Convertit un symbole interne en symbole Yahoo (forex -> 'EURUSD=X', or -> 'GC=F', action -> ticker)."""
    s = symbol.upper()
    if s in _COMMODITY_MAP:
        return _COMMODITY_MAP[s]
    if "/" in s:  # forex (les paires crypto passent par Binance, pas ici)
        base, quote = s.split("/", 1)
        return f"{base}{quote}=X"
    return s


async def fetch_ohlcv(symbol: str, interval: str = "1h", limit: int = 200) -> list[dict]:
    """Retourne [{time, open, high, low, close, volume}] (time = UNIX secondes). Lève si indispo."""
    import httpx

    ysym = to_yahoo_symbol(symbol)
    params = {"interval": _INTERVAL.get(interval, "1h"), "range": _range_for(interval, limit)}
    async with httpx.AsyncClient(timeout=12, headers=_HEADERS) as client:
        resp = await client.get(_CHART_URL.format(symbol=ysym), params=params)
        resp.raise_for_status()
        data = resp.json()

    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise RuntimeError(f"Yahoo : pas de données pour {ysym}")
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    opens, highs = quote.get("open") or [], quote.get("high") or []
    lows, closes, vols = quote.get("low") or [], quote.get("close") or [], quote.get("volume") or []

    rows: list[dict] = []
    for i, t in enumerate(timestamps):
        o, h, low, c = opens[i], highs[i], lows[i], closes[i]
        if None in (o, h, low, c):  # bougies incomplètes (jours fériés, gaps)
            continue
        rows.append({
            "time": int(t), "open": float(o), "high": float(h), "low": float(low),
            "close": float(c), "volume": float(vols[i] or 0) if i < len(vols) else 0.0,
        })
    # Yahoo ne sert pas de 4 h : on AGRÈGE les bougies horaires au lieu de les renvoyer telles
    # quelles. Sans cette étape, l'étape 3 de la stratégie (« le 4 h confirme ») analyserait en
    # réalité du 1 h — deux unités de temps différentes portant le même nom.
    if interval == "4h":
        rows = resample(rows, 4 * 3600)
    return rows[-limit:]


def resample(rows: list[dict], bucket_seconds: int) -> list[dict]:
    """Agrège des bougies en bougies plus longues, alignées sur l'horloge UTC.

    L'alignement sur l'horloge (00:00, 04:00, 08:00…) et non sur la première bougie reçue est
    important : c'est le découpage que voient les autres intervenants, donc celui sur lequel se
    forment les niveaux que l'on cherche à lire.
    """
    out: list[dict] = []
    current: dict | None = None
    for r in rows:
        bucket = (r["time"] // bucket_seconds) * bucket_seconds
        if current is None or current["time"] != bucket:
            if current is not None:
                out.append(current)
            current = {"time": bucket, "open": r["open"], "high": r["high"],
                       "low": r["low"], "close": r["close"], "volume": r["volume"]}
            continue
        current["high"] = max(current["high"], r["high"])
        current["low"] = min(current["low"], r["low"])
        current["close"] = r["close"]
        current["volume"] += r["volume"]
    if current is not None:
        out.append(current)
    return out
