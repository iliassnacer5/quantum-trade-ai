"""Récupération OHLCV avec horodatage, pour l'affichage graphique (TradingView).

Renvoie une liste de dicts {time, open, high, low, close, volume} où `time` est un timestamp
UNIX en secondes (format attendu par lightweight-charts). Binance en priorité, repli synthétique.
"""

from __future__ import annotations

import logging
import time as _time

from app.data import binance
from app.data.synthetic import generate_candles

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

# CACHE COURT sur les résultats de `get_ohlcv_with_source` — clé (symbole, unité, limite).
#
# Sans lui, chaque appel refaisait un aller-retour RÉSEAU réel vers Yahoo/Binance, y compris depuis
# des pages qui se rafraîchissent seules toutes les 10 s (scanner, tableau de bord, exécution,
# agents). Résultat mesuré : pages lentes, et Yahoo qui finit par répondre 422 (limite de débit)
# quand plusieurs symboles sont demandés en rafale — cf. le commentaire sur `playbook_max_parallel`
# dans config.py, qui documente le même phénomène.
#
# La TTL (20 s) reste très en dessous de la granularité de la bougie la plus fine (15 min = 900 s) :
# aucune fraîcheur n'est perdue, la bougie en cours de formation n'a de toute façon pas changé de
# résultat définitif entre deux appels à 20 s d'intervalle. Elle sert aussi à absorber le cas 422 :
# un échec récent n'est pas retenté en boucle, il attend la même fenêtre.
_CACHE: dict[tuple[str, str, int], tuple[float, dict]] = {}
_CACHE_TTL = 20.0


async def _binance_ohlcv(symbol: str, interval: str, limit: int) -> list[dict]:
    import httpx

    from app.data.markets import _timeout

    params = {"symbol": binance.to_binance_symbol(symbol), "interval": interval, "limit": limit}
    async with httpx.AsyncClient(timeout=_timeout(limit)) as client:
        resp = await client.get(binance.REST_URL, params=params)
        resp.raise_for_status()
        rows = resp.json()
    # [open_time(ms), open, high, low, close, volume, ...]
    return [
        {
            "time": int(r[0] // 1000),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
        }
        for r in rows
    ]


def _synthetic_ohlcv(interval: str, limit: int) -> list[dict]:
    step = INTERVAL_SECONDS.get(interval, 3600)
    candles = generate_candles(n=limit)
    start = int(_time.time()) - len(candles) * step
    return [
        {
            "time": start + i * step,
            "open": round(c.open, 2),
            "high": round(c.high, 2),
            "low": round(c.low, 2),
            "close": round(c.close, 2),
            "volume": round(c.volume, 2),
        }
        for i, c in enumerate(candles)
    ]


async def get_ohlcv_with_source(symbol: str, interval: str = "1h", limit: int = 200) -> dict:
    """OHLCV horodaté AVEC sa provenance — la fonction de référence du module.

    La source est déterminée par le chargement lui-même, jamais déduite d'un état global posé
    ailleurs : un appelant qui doit décider si de l'argent a été gagné ne peut pas s'appuyer sur la
    trace laissée par un autre chargeur, pour un autre symbole, à un autre moment.

    Si la source réelle est indisponible, on ne FABRIQUE rien par défaut : `candles` est vide et
    `real` vaut False. Une bougie inventée présentée comme réelle est pire qu'une page vide
    (cf. `data_allow_synthetic`).

    Mémorisé 20 s (cf. `_CACHE`) : sans ça, chaque page qui se rafraîchit seule (scanner, tableau de
    bord…) refaisait un aller-retour réseau à chaque tick, pour un résultat identique.
    """
    key = (symbol.upper(), interval, limit)
    now = _time.monotonic()
    cached = _CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]

    from app.core.config import get_settings
    from app.data import markets, yahoo

    cls = markets.asset_class(symbol)
    payload: dict
    try:
        if cls == "crypto":
            data = await _binance_ohlcv(symbol, interval, limit)
        else:  # actions / forex / métaux -> Yahoo Finance (réel, sans clé)
            data = await yahoo.fetch_ohlcv(symbol, interval, limit)
        if len(data) >= 10:
            payload = {"candles": data, "source": "real", "real": True, "note": ""}
        else:
            payload = None
    except Exception as exc:  # noqa: BLE001
        logger.warning("OHLCV %s (%s) indisponible (%s)", symbol, cls, exc)
        payload = None

    if payload is None:
        if not get_settings().data_allow_synthetic:
            logger.warning("OHLCV %s (%s) : aucune donnée RÉELLE — série vide (pas de fabrication)",
                           symbol, cls)
            payload = {"candles": [], "source": "unavailable", "real": False,
                       "note": f"Aucune donnée réelle disponible pour {symbol} en {interval}."}
        else:
            payload = {"candles": _synthetic_ohlcv(interval, limit), "source": "synthetic",
                       "real": False, "note": "Données de démonstration — non exploitables pour trader."}

    _CACHE[key] = (now + _CACHE_TTL, payload)
    return payload


async def get_ohlcv(symbol: str, interval: str = "1h", limit: int = 200) -> list[dict]:
    """Bougies seules. Préférer `get_ohlcv_with_source` dès qu'une DÉCISION en dépend."""
    return (await get_ohlcv_with_source(symbol, interval, limit))["candles"]
