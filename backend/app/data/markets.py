"""Routage multi-marchés (Phase 2) : crypto / actions / forex.

Détermine la classe d'actif d'un symbole et charge les bougies via le bon connecteur :
- crypto  -> Binance (existant)
- actions -> Alpaca (si clé) sinon synthétique
- forex   -> OANDA (si clé) sinon synthétique

Tous les connecteurs dégradent gracieusement vers des données synthétiques hors-ligne.
"""

from __future__ import annotations

import logging
import time as _time

from app.core.config import get_settings
from app.data import binance
from app.data.synthetic import generate_candles
from app.domain.indicators import Candle

logger = logging.getLogger(__name__)


def _timeout(limit: int = 200):  # noqa: ANN201 — httpx.Timeout, importé paresseusement
    """Délais réseau d'un connecteur de marché : connexion COURTE, lecture plus généreuse.

    MESURÉ le 30/07/2026 en conteneur, sur 6 actions : Alpaca a échoué 3 fois sur 6 et Yahoo 3 fois
    sur 6 — et **chaque** échec était un `ConnectTimeout` de 10 à 12 s, jamais une réponse lente.
    Une connexion TCP qui ne s'établit pas en 3 s ne s'établira pas : tout le reste est du temps
    mort. C'est cette cascade (10 s Alpaca + 12 s Yahoo) qui produisait les 22 s mesurées sur
    `/api/execution/positions`.

    Le délai de LECTURE reste large pour les requêtes profondes (backtest : plusieurs milliers de
    bougies), où une réponse lente est normale — mais la CONNEXION garde le même délai court.
    """
    import httpx

    s = get_settings()
    read = s.market_deep_read_timeout if limit >= s.market_deep_limit else s.market_read_timeout
    return httpx.Timeout(read, connect=s.market_connect_timeout)


_FOREX = {"EUR", "USD", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"}
_CRYPTO_QUOTE = {"USDT", "USDC", "BTC", "ETH", "BUSD"}
_COMMODITY_BASES = {"XAU", "XAG", "XPT", "XPD"}  # or, argent, platine, palladium

# Indices boursiers, sous les noms qu'emploient les brokers CFD (et non les tickers Yahoo, qui sont
# illisibles). La correspondance vers Yahoo vit dans `data.yahoo._INDEX_MAP`.
INDEX_SYMBOLS = {
    "SPX500",   # S&P 500
    "NAS100",   # Nasdaq 100
    "US30",     # Dow Jones Industrial Average
    "GER40",    # DAX
    "UK100",    # FTSE 100
    "FRA40",    # CAC 40
    "EU50",     # Euro Stoxx 50
    "JPN225",   # Nikkei 225
    "HK50",     # Hang Seng
    "AUS200",   # ASX 200
}


def asset_class(symbol: str) -> str:
    s = symbol.upper()
    if s in INDEX_SYMBOLS:
        return "index"
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
    async with httpx.AsyncClient(timeout=_timeout(limit)) as client:
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
    async with httpx.AsyncClient(timeout=_timeout(limit)) as client:
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


async def _cascade(loaders: list, min_needed: int) -> list[Candle]:  # noqa: ANN001
    """Essaie les fournisseurs L'UN APRÈS L'AUTRE, et retient la première réponse exploitable.

    Le SÉQUENTIEL est ici un choix mesuré, pas une facilité. Une version parallèle (les deux
    fournisseurs lancés ensemble, le plus rapide gagne) a été implémentée puis **abandonnée sur
    mesure** le 30/07/2026 : elle double le nombre de connexions simultanées, et sur ce réseau
    c'est la connexion elle-même qui est le goulot. Résultat mesuré du parallèle sur 6 actions
    chargées ensemble (12 connexions d'un coup) : **0 symbole servi sur 6**, tout en délai de
    connexion — contre 3 sur 6 par fournisseur en séquentiel. Aller plus vite en demandant plus
    n'a pas fonctionné : la saturation coûte plus que la cascade.

    Ce qui rend la cascade acceptable, c'est le délai de CONNEXION court (`_timeout`) : le pire cas
    passe de 10 s + 12 s = 22 s à 3 s + 3 s = 6 s, et le cas courant reste celui du premier
    fournisseur qui répond (0,4 s mesuré sur Alpaca).
    """
    best: list[Candle] = []
    for loader in loaders:
        try:
            candles = await loader()
        except Exception as exc:  # noqa: BLE001 — un fournisseur KO n'invalide pas le suivant
            logger.debug("Fournisseur indisponible (%s)", exc)
            continue
        if len(candles) >= min_needed:
            return candles
        # Série trop courte : gardée en réserve au cas où AUCUN fournisseur ne fasse mieux.
        if len(candles) > len(best):
            best = candles
    return best


# Source de données du DERNIER chargement par symbole (qualité des données).
# Valeurs : "live" (flux WS), "real" (REST réel), "synthetic" (repli factice).
_LAST_SOURCE: dict[str, str] = {}

# CACHE COURT des bougies chargées — clé (symbole, unité, limite), valeur (expiration, bougies, source).
#
# `load_candles` est le SECOND chemin de chargement du projet (l'autre étant
# `data.ohlcv.get_ohlcv_with_source`, mis en cache le 29/07/2026) et il n'en avait aucun : chaque
# appel repartait sur le réseau. Or il est appelé en rafale par les chemins les plus chauds —
# `positions_snapshot`, les quatre passages successifs de `positions_loop` sur les mêmes symboles,
# l'ouverture d'ordre, `_reference_price` — qui redemandaient donc les mêmes bougies à quelques
# secondes d'intervalle.
#
# La TTL est volontairement la MÊME que celle de `data.ohlcv` (`market_cache_ttl`) : deux fonctions
# qui chargent les mêmes bougies ne doivent pas avoir deux politiques de fraîcheur différentes —
# un seul chiffre à régler. 20 s reste très en dessous de la granularité de la bougie la plus fine
# (15 min) et de l'ordre de grandeur du cache de prix déjà accepté (15 s).
_CACHE: dict[tuple[str, str, int], tuple[float, list[Candle], str]] = {}


def clear_cache() -> None:
    """Vide le cache de bougies (tests, et rafraîchissement forcé)."""
    _CACHE.clear()


def data_source(symbol: str) -> str:
    """Source du dernier chargement de `symbol` : 'live' | 'real' | 'synthetic' | 'unknown'."""
    return _LAST_SOURCE.get(symbol.upper(), "unknown")


def is_real(symbol: str) -> bool:
    """Vrai si les dernières données de `symbol` sont réelles (flux live ou REST), pas synthétiques."""
    return data_source(symbol) in {"live", "real"}


async def load_candles(symbol: str, interval: str = "1h", limit: int = 200) -> list[Candle]:
    """Charge les bougies réelles selon la classe d'actif, avec repli synthétique.

    crypto -> Binance ; actions -> Alpaca **et** Yahoo en parallèle ; forex -> OANDA **et** Yahoo en
    parallèle ; métaux/indices -> Yahoo. Enregistre la source du chargement (`data_source`) pour
    signaler les données factices.

    Deux optimisations, toutes deux adossées à une mesure (cf. globale/07_PLAN_PERFORMANCE.md) :

    1. **Cache court** (`market_cache_ttl`, 20 s) : les chemins chauds redemandaient les mêmes
       bougies à quelques secondes d'intervalle (les quatre passages de `positions_loop`, le
       rafraîchissement de la page toutes les 10 s…).
    2. **Fournisseurs en PARALLÈLE** au lieu d'une cascade : la cascade payait le délai de connexion
       du premier PUIS du second quand le premier ne servait pas le symbole (10 s + 12 s = les 22 s
       mesurées sur `/api/execution/positions`). Cf. `_race`.
    """
    cls = asset_class(symbol)
    key = symbol.upper()
    # Minimum de bougies exploitables : les unités LONGUES (hebdo/mensuel, étape 1 du playbook) en
    # ont mécaniquement moins — exiger 60 bougies mensuelles enverrait tout vers le synthétique.
    min_needed = {"1M": 12, "1w": 30}.get(interval, 60)
    # Cache temps réel (crypto) : si le flux WS a chauffé le cache, on évite un appel REST. Il passe
    # AVANT le cache court : une bougie poussée par le flux est plus fraîche que tout ce qu'on a pu
    # mémoriser d'un appel REST.
    if cls == "crypto":
        from app.realtime import market_stream

        if market_stream.is_live(symbol, interval):
            cached = market_stream.get_cached(symbol, interval, limit)
            if cached and len(cached) >= min(limit, 60):
                _LAST_SOURCE[key] = "live"
                return cached

    # Cache court. La clé inclut `limit` : rendre 60 bougies à un appelant qui en demande 200
    # tronquerait silencieusement son analyse.
    cache_key = (key, interval, limit)
    now = _time.monotonic()
    hit = _CACHE.get(cache_key)
    if hit and hit[0] > now:
        _LAST_SOURCE[key] = hit[2]
        return hit[1]

    candles: list[Candle] = []
    try:
        if cls == "crypto":
            candles = await binance.fetch_klines(symbol, interval=interval, limit=limit)
        elif cls == "stock":
            # Alpaca puis Yahoo. Mesurés COMPLÉMENTAIRES (chacun sert des symboles que l'autre
            # rate : Alpaca WMT/MSFT/AAPL, Yahoo V/PEP/GOOGL), donc les deux restent nécessaires —
            # mais en séquence, pas en parallèle (cf. `_cascade`).
            candles = await _cascade([
                lambda: _alpaca_candles(symbol, interval, limit),
                lambda: _yahoo_candles(symbol, interval, limit),
            ], min_needed)
        elif cls == "forex":
            candles = await _cascade([
                lambda: _oanda_candles(symbol, interval, limit),
                lambda: _yahoo_candles(symbol, interval, limit),
            ], min_needed)
        elif cls in ("commodity", "index"):
            # Métaux : futures COMEX via Yahoo (GC=F/SI=F). Indices : leur cotation Yahoo (^GSPC,
            # ^GDAXI…). Dans les deux cas des données réelles, avec volume, et sans clé d'API.
            candles = await _yahoo_candles(symbol, interval, limit)
        if len(candles) >= min_needed:
            _LAST_SOURCE[key] = "real"
            _CACHE[cache_key] = (now + get_settings().market_cache_ttl, candles, "real")
            return candles
        logger.warning("Backfill %s (%s) insuffisant", symbol, cls)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Connecteur %s indisponible pour %s (%s)", cls, symbol, exc)
    # Par défaut on ne FABRIQUE pas de bougies : une série vide est honnête, une série inventée ne
    # l'est pas. Les appelants (playbook, entraînement, backtest) refusent déjà d'agir sans données.
    if not get_settings().data_allow_synthetic:
        _LAST_SOURCE[key] = "unavailable"
        # L'ÉCHEC est mémorisé aussi, et c'est délibéré : sans ça, un symbole que personne ne sert
        # est réinterrogé à chaque appel (donc à chaque rafraîchissement de page), et l'on repaie le
        # délai de connexion en boucle. 20 s d'attente avant de réessayer est le bon compromis.
        _CACHE[cache_key] = (now + get_settings().market_cache_ttl, [], "unavailable")
        return []
    # Repli déterministe (graine basée sur le symbole pour la cohérence par actif)
    _LAST_SOURCE[key] = "synthetic"
    return generate_candles(seed=abs(hash(symbol)) % 10_000)
