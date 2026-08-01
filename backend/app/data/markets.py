"""Routage multi-marchés (Phase 2) : crypto / actions / forex.

Détermine la classe d'actif d'un symbole et charge les bougies via le bon connecteur :
- crypto  -> Binance (existant)
- actions -> Alpaca (si clé) sinon synthétique
- forex   -> OANDA (si clé) sinon synthétique

Tous les connecteurs dégradent gracieusement vers des données synthétiques hors-ligne.
"""

from __future__ import annotations

import asyncio
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
    # `sort=desc` — LE PLUS RÉCENT D'ABORD. Sans ce tri, Alpaca renvoie les barres en avançant
    # depuis `start` et les tronque à `limit` : on recevait donc la fenêtre la plus ANCIENNE de
    # l'intervalle demandé, jamais la plus récente. Mesuré sur JPM le 01/08/2026, avec `start` à
    # 5 × la profondeur demandée :
    #   1 j  -> dernière bougie 2025-06-11 (13 MOIS de retard), close 268.16
    #   4 h  -> dernière bougie 2026-03-25 (4 mois de retard),  close 295.50
    #   1 h  -> dernière bougie 2026-07-29 (3 jours de retard), close 349.64
    # alors que Yahoo servait 351.79 sur TOUS les intervalles le même jour.
    #
    # Conséquence directe sur la stratégie : les étapes 1 à 3 (tendance mensuelle et journalière,
    # confirmation 4 h) étaient calculées sur des données vieilles de plusieurs mois, tandis que le
    # déclencheur 15 min voyait le prix du jour. Le stop et l'objectif étaient donc posés sur des
    # niveaux périmés, à un prix courant — c'est ce qui produisait des analyses où le « niveau
    # majeur » annoncé se situait 30 % sous le prix d'entrée.
    #
    # Le tri décroissant borne la fenêtre sur MAINTENANT quelle que soit la profondeur demandée ;
    # on la remet ensuite en ordre chronologique, seul ordre que les indicateurs sachent lire.
    params = {"timeframe": tf, "limit": min(limit * 2, 1000), "start": start,
              "feed": "iex", "sort": "desc"}
    # Client partagé : connexion réutilisée entre les 150 chargements d'actions d'un cycle
    # (cf. `data/http.py` — mesuré, divise par 2,5 les échecs de connexion).
    from app.data.http import client as shared_client

    client = await shared_client("alpaca", timeout=_timeout(limit), headers=headers)
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    bars = resp.json().get("bars", [])
    bars.reverse()  # desc -> chronologique
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
    from app.data.http import client as shared_client   # connexion réutilisée (cf. data/http.py)

    client = await shared_client("oanda", timeout=_timeout(limit), headers=headers)
    resp = await client.get(url, params={"granularity": gran, "count": limit, "price": "M"})
    resp.raise_for_status()
    candles = resp.json().get("candles", [])
    out = []
    for c in candles:
        m = c["mid"]
        out.append(Candle(float(m["o"]), float(m["h"]), float(m["l"]), float(m["c"]), float(c.get("volume", 0))))
    return out


# RÉGULATEUR DE DÉBIT YAHOO — on s'auto-limite au lieu de se faire refuser.
#
# Mesuré le 01/08/2026 : en rafale, 0/12 requêtes aboutissent (HTTP 429 systématique) ; espacées de
# 1,5 s, 17/17 aboutissent, toutes classes d'actifs confondues. Le fournisseur n'est pas bloqué,
# c'est le BALAYAGE qui dépasse son quota (84 symboles × 5 unités = 420 requêtes / 240 s).
#
# Un espacement minimal vaut mieux qu'un flot refusé : la même requête passe, simplement décalée.
_yahoo_lock = asyncio.Lock()
_yahoo_next_at = 0.0


async def _yahoo_slot() -> None:
    """Attend son tour avant d'interroger Yahoo (espacement minimal entre deux requêtes)."""
    global _yahoo_next_at

    rps = get_settings().yahoo_max_rps
    if rps <= 0:
        return
    gap = 1.0 / rps
    async with _yahoo_lock:
        now = _time.monotonic()
        wait = _yahoo_next_at - now
        _yahoo_next_at = max(now, _yahoo_next_at) + gap
    if wait > 0:
        await asyncio.sleep(wait)


async def _twelve_candles(symbol: str, interval: str, limit: int) -> list[Candle]:
    """DERNIER RECOURS : Twelve Data, quand aucun autre fournisseur n'a rendu de bougies.

    Placé en fin de cascade à cause de son quota — ~8 requêtes/minute sur le plan gratuit, contre
    420 requêtes par cycle de balayage. Il ne peut donc pas porter le trafic courant, mais il évite
    la page vide quand Yahoo limite le débit ou qu'un connecteur tombe. Il refuse proprement quand
    son quota est atteint (cf. `twelvedata._slot`) : la cascade rend alors une série vide, et le
    projet affiche « données indisponibles » — jamais une donnée inventée.
    """
    from app.data import twelvedata

    rows = await twelvedata.fetch_ohlcv(symbol, interval, limit)
    return [Candle(r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]


async def _yahoo_candles(symbol: str, interval: str, limit: int, *, fresh: bool = False) -> list[Candle]:
    """Bougies réelles via Yahoo Finance (actions & forex, sans clé).

    `fresh=True` COURT-CIRCUITE le régulateur. C'est le garde-fou essentiel : les lectures fraîches
    sont celles qui engagent de l'argent (remplissage d'un ordre, déplacement d'un stop, clôture) ou
    qui décident d'une entrée. Les faire patienter derrière un balayage de fond de 420 requêtes
    reviendrait à rater un déclencheur 15 min pour économiser du quota — exactement le compromis
    qu'il ne faut pas faire. Elles sont rares (quelques-unes par minute) face au balayage, donc les
    laisser passer ne remet pas la régulation en cause.
    """
    from app.data import yahoo

    if not fresh:
        await _yahoo_slot()
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
            # Type inclus : une erreur réseau au message vide rendait cette trace inexploitable.
            logger.debug("Fournisseur indisponible (%s: %s)",
                         type(exc).__name__, exc or "sans message")
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


# Durée d'une bougie, par unité de temps. Sert à dimensionner le cache : on ne garde jamais une
# série assez longtemps pour masquer l'apparition d'une NOUVELLE bougie.
_BAR_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400,
                "1d": 86400, "1w": 604800, "1M": 2592000}


def _ttl_for(interval: str) -> float:
    """Durée de cache pour cette unité de temps — proportionnelle à la durée de la bougie.

    Un TTL unique de 20 s pour toutes les unités traitait une bougie MENSUELLE comme une bougie de
    15 minutes : on la redemandait 4 320 fois par jour pour une valeur qui change une fois par
    mois. C'est ce gaspillage qui saturait le quota du fournisseur.

    Le TTL vaut une petite fraction de la durée de la bougie (`market_cache_ttl_ratio`, 1/60e par
    défaut), borné en bas par `market_cache_ttl` — aucune unité n'est donc cachée MOINS longtemps
    qu'avant — et en haut par `market_cache_ttl_max`. Concrètement : 15 min → 20 s (inchangé),
    1 h → 60 s, 4 h → 240 s, 1 j → 24 min, 1 mois → 30 min (plafond).

    Pourquoi c'est sûr pour la stratégie : à 1/60e, le cache expire toujours BIEN avant la fermeture
    de la bougie suivante, donc aucune nouvelle bougie ne peut être manquée. Et surtout, les
    chemins qui ENGAGENT de l'argent (remplissage d'ordre, déplacement de stop, clôture) lisent
    avec `fresh=True` et ne consultent jamais ce cache.
    """
    s = get_settings()
    bar = _BAR_SECONDS.get(interval, 3600)
    return max(s.market_cache_ttl, min(bar * s.market_cache_ttl_ratio, s.market_cache_ttl_max))


def clear_cache() -> None:
    """Vide le cache de bougies (tests, et rafraîchissement forcé)."""
    _CACHE.clear()


def data_source(symbol: str) -> str:
    """Source du dernier chargement de `symbol` : 'live' | 'real' | 'synthetic' | 'unknown'."""
    return _LAST_SOURCE.get(symbol.upper(), "unknown")


def is_real(symbol: str) -> bool:
    """Vrai si les dernières données de `symbol` sont réelles (flux live ou REST), pas synthétiques."""
    return data_source(symbol) in {"live", "real"}


async def load_candles(symbol: str, interval: str = "1h", limit: int = 200,
                       *, fresh: bool = False) -> list[Candle]:
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

    `fresh=True` IGNORE le cache court en lecture (il continue de l'alimenter). Réservé aux chemins
    qui ENGAGENT de l'argent — prix de remplissage d'un ordre, déplacement d'un stop — où une donnée
    vieille de 20 s n'est pas un détail d'affichage mais un prix faux inscrit dans le portefeuille.
    L'affichage, lui, garde le cache : c'est exactement la frontière moteur / cache.
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
    if hit and hit[0] > now and not fresh:
        _LAST_SOURCE[key] = hit[2]
        return hit[1]

    candles: list[Candle] = []
    try:
        if cls == "crypto":
            # Binance d'abord (gratuit, sans quota, très fiable) ; Twelve Data en secours quand il
            # tombe — c'est ce qui évite qu'un hoquet réseau vide toute la page crypto.
            candles = await _cascade([
                lambda: binance.fetch_klines(symbol, interval=interval, limit=limit),
                lambda: _twelve_candles(symbol, interval, limit),
            ], min_needed)
        elif cls == "stock":
            # Alpaca puis Yahoo. Mesurés COMPLÉMENTAIRES (chacun sert des symboles que l'autre
            # rate : Alpaca WMT/MSFT/AAPL, Yahoo V/PEP/GOOGL), donc les deux restent nécessaires —
            # mais en séquence, pas en parallèle (cf. `_cascade`).
            candles = await _cascade([
                lambda: _alpaca_candles(symbol, interval, limit),
                lambda: _yahoo_candles(symbol, interval, limit, fresh=fresh),
                lambda: _twelve_candles(symbol, interval, limit),
            ], min_needed)
        elif cls == "forex":
            candles = await _cascade([
                lambda: _oanda_candles(symbol, interval, limit),
                lambda: _yahoo_candles(symbol, interval, limit, fresh=fresh),
                lambda: _twelve_candles(symbol, interval, limit),
            ], min_needed)
        elif cls in ("commodity", "index"):
            # Métaux : futures COMEX via Yahoo (GC=F/SI=F). Indices : leur cotation Yahoo (^GSPC,
            # ^GDAXI…). Dans les deux cas des données réelles, avec volume, et sans clé d'API.
            # Twelve Data ne sert en secours que l'or : l'argent, le platine, le palladium et les
            # indices exigent chez lui un plan payant (il rend `None` et n'est même pas interrogé).
            candles = await _cascade([
                lambda: _yahoo_candles(symbol, interval, limit, fresh=fresh),
                lambda: _twelve_candles(symbol, interval, limit),
            ], min_needed)
        if len(candles) >= min_needed:
            _LAST_SOURCE[key] = "real"
            _CACHE[cache_key] = (now + _ttl_for(interval), candles, "real")
            return candles
        logger.warning("Backfill %s (%s) insuffisant", symbol, cls)
    except Exception as exc:  # noqa: BLE001
        # Le TYPE d'exception est journalisé en plus du message : beaucoup d'erreurs réseau
        # (`httpx.ReadError`, `ConnectError`…) ont un message VIDE, et la ligne se lisait alors
        # « Connecteur crypto indisponible pour ETH/USDT () » — un diagnostic impossible, qui m'a
        # fait conclure à tort à un blocage Binance alors qu'il s'agissait d'une saturation passagère.
        logger.warning("Connecteur %s indisponible pour %s (%s: %s)",
                       cls, symbol, type(exc).__name__, exc or "sans message")
    # Par défaut on ne FABRIQUE pas de bougies : une série vide est honnête, une série inventée ne
    # l'est pas. Les appelants (playbook, entraînement, backtest) refusent déjà d'agir sans données.
    if not get_settings().data_allow_synthetic:
        _LAST_SOURCE[key] = "unavailable"
        # L'ÉCHEC est mémorisé aussi, et c'est délibéré : sans ça, un symbole que personne ne sert
        # est réinterrogé à chaque appel (donc à chaque rafraîchissement de page), et l'on repaie le
        # délai de connexion en boucle. 20 s d'attente avant de réessayer est le bon compromis.
        #
        # ATTENTION — un échec garde TOUJOURS le TTL COURT (`market_cache_ttl`), jamais celui de
        # l'unité de temps. Mémoriser 24 minutes l'indisponibilité d'une bougie journalière
        # rendrait le symbole aveugle tout ce temps après un simple hoquet réseau, et l'auto-entrée
        # cesserait de le surveiller : on retomberait exactement dans le « les agents ouvrent moins
        # de positions ». Une donnée réussie se garde longtemps ; un échec se réessaie vite.
        _CACHE[cache_key] = (now + get_settings().market_cache_ttl, [], "unavailable")
        return []
    # Repli déterministe (graine basée sur le symbole pour la cohérence par actif)
    _LAST_SOURCE[key] = "synthetic"
    return generate_candles(seed=abs(hash(symbol)) % 10_000)
