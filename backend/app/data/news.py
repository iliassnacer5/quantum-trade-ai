"""Connecteur news / sentiment multi-fournisseurs (M1 + amélioration).

Récupère de vraies actualités financières selon la classe d'actif (crypto / forex / actions) via,
dans l'ordre de disponibilité : newsdata.io, NewsAPI.org, Finnhub. Dégrade gracieusement vers une
liste vide (l'Agent Sentiment bascule alors sur l'indice de marché momentum).
"""

from __future__ import annotations

import logging

from app.agents.sentiment import NewsItem
from app.core.config import get_settings
from app.data import markets

logger = logging.getLogger(__name__)

# Noms lisibles pour la recherche de news par devise/crypto.
_CRYPTO_NAMES = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "XRP": "XRP", "ADA": "Cardano",
    "DOGE": "Dogecoin", "BNB": "Binance Coin", "AVAX": "Avalanche", "MATIC": "Polygon", "DOT": "Polkadot",
}
_FX_NAMES = {
    "EUR": "euro", "USD": "dollar", "GBP": "pound", "JPY": "yen", "CHF": "franc",
    "AUD": "Australian dollar", "CAD": "Canadian dollar", "NZD": "New Zealand dollar",
}


_COMMODITY_QUERIES = {
    "XAU": "gold price Federal Reserve inflation safe haven",
    "XAG": "silver price precious metals",
    "XPT": "platinum price precious metals",
    "XPD": "palladium price precious metals",
}


def _query_for(symbol: str) -> str:
    """Construit une requête de recherche pertinente selon la classe d'actif."""
    cls = markets.asset_class(symbol)
    if cls == "crypto":
        base = symbol.split("/")[0].split("-")[0].upper()
        return _CRYPTO_NAMES.get(base, base) + " crypto"
    if cls == "forex":
        base, quote = (symbol.split("/") + [""])[:2]
        return f"{_FX_NAMES.get(base.upper(), base)} {_FX_NAMES.get(quote.upper(), quote)} forex"
    if cls == "commodity":
        base = symbol.split("/")[0].upper()
        return _COMMODITY_QUERIES.get(base, f"{base} commodity price")
    # actions : le ticker tel quel (AAPL, TSLA…)
    return symbol.split("/")[0].split("-")[0].upper()


async def _newsdata(query: str, limit: int) -> list[NewsItem]:
    s = get_settings()
    if not s.newsdata_key:
        return []
    import httpx

    url = "https://newsdata.io/api/1/latest"
    params = {"apikey": s.newsdata_key, "q": query, "language": "en"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        results = resp.json().get("results", []) or []
    return [NewsItem(headline=a.get("title", "")) for a in results[:limit] if a.get("title")]


async def _newsapi(query: str, limit: int) -> list[NewsItem]:
    s = get_settings()
    if not s.newsapi_key:
        return []
    import httpx

    url = "https://newsapi.org/v2/everything"
    params = {"q": query, "apiKey": s.newsapi_key, "language": "en", "sortBy": "publishedAt", "pageSize": limit}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        articles = resp.json().get("articles", []) or []
    return [NewsItem(headline=a.get("title", "")) for a in articles[:limit] if a.get("title")]


async def _massive(symbol: str, limit: int) -> list[NewsItem]:
    """News Massive (ex-Polygon.io) — le seul de ses services exploitable sur ce plan.

    Mesuré le 01/08/2026 : les agrégats HORAIRES rendent 0 barre et les indices répondent
    « NOT_AUTHORIZED » ; seul le journalier et les actualités sont servis. Comme la stratégie entre
    en 15 min, ses bougies n'apportent rien — ses news, si.

    Le filtre par ticker n'existe que pour les actions ; pour la crypto, le forex et les métaux on
    prend le flux général, qui reste pertinent pour le sentiment de marché.
    """
    s = get_settings()
    if not s.massive_news_key:
        return []
    import httpx

    params: dict[str, object] = {"limit": max(1, min(limit, 50)), "apiKey": s.massive_news_key,
                                 "order": "desc", "sort": "published_utc"}
    if markets.asset_class(symbol) == "stock":
        params["ticker"] = symbol.split("/")[0].split("-")[0].upper()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get("https://api.polygon.io/v2/reference/news", params=params)
        resp.raise_for_status()
        results = resp.json().get("results", []) or []
    return [NewsItem(headline=a.get("title", "")) for a in results[:limit] if a.get("title")]


async def _finnhub(symbol: str, limit: int) -> list[NewsItem]:
    """News Finnhub, adaptées à la classe d'actif.

    - Actions : `company-news` propre au ticker (fenêtre des 14 derniers jours = fraîcheur).
    - Crypto / forex : `news` par catégorie (Finnhub n'expose pas de news par paire) — flux
      d'actualité de marché récent, pertinent pour le sentiment.
    """
    s = get_settings()
    if not s.finnhub_api_key:
        return []
    from datetime import UTC, datetime, timedelta

    import httpx

    cls = markets.asset_class(symbol)
    async with httpx.AsyncClient(timeout=10) as client:
        if cls == "stock":
            base = symbol.split("/")[0].split("-")[0].upper()
            today = datetime.now(UTC).date()
            params = {
                "symbol": base,
                "token": s.finnhub_api_key,
                "from": (today - timedelta(days=14)).isoformat(),
                "to": today.isoformat(),
            }
            resp = await client.get("https://finnhub.io/api/v1/company-news", params=params)
        else:
            category = "crypto" if cls == "crypto" else "forex" if cls == "forex" else "general"
            params = {"category": category, "token": s.finnhub_api_key}
            resp = await client.get("https://finnhub.io/api/v1/news", params=params)
        resp.raise_for_status()
        data = resp.json() or []
    # Finnhub renvoie les plus récents en tête ; on tronque après tri éventuel.
    items = sorted(data, key=lambda i: i.get("datetime", 0), reverse=True)[:limit]
    return [NewsItem(headline=i.get("headline", "")) for i in items if i.get("headline")]


async def fetch_news(symbol: str, limit: int = 20) -> list[NewsItem]:
    """Récupère de vraies news via le 1er fournisseur disponible.

    Ordre adapté à la classe d'actif : pour les ACTIONS, Finnhub est prioritaire (news propres au
    ticker, plus pertinentes que la recherche par mots-clés) ; pour crypto/forex, NewsAPI/newsdata
    (recherche ciblée par nom) d'abord, Finnhub (news de catégorie) en secours.
    """
    query = _query_for(symbol)
    finnhub = ("Finnhub", _finnhub(symbol, limit))
    # Massive (ex-Polygon) : ajouté le 01/08/2026. Ses BOUGIES sont inutilisables ici (0 barre en
    # horaire, indices non autorisés) mais ses news fonctionnent — c'est donc là, et seulement là,
    # qu'il est branché. Un fournisseur de plus, c'est une page de moins qui reste vide.
    massive = ("Massive", _massive(symbol, limit))
    keyword = [("NewsAPI", _newsapi(query, limit)), ("newsdata.io", _newsdata(query, limit))]
    providers = ([finnhub, massive, *keyword] if markets.asset_class(symbol) == "stock"
                 else [*keyword, finnhub, massive])
    for provider, coro in providers:
        try:
            items = await coro
            if items:
                logger.info("News %s via %s : %d titres", symbol, provider, len(items))
                return items
        except Exception as exc:  # noqa: BLE001 — on tente le fournisseur suivant
            logger.warning("News %s via %s échoué (%s: %s)", symbol, provider, type(exc).__name__, exc)
    return []


async def fetch_fear_greed() -> int | None:
    """Récupère l'index Fear & Greed crypto (alternative.me, sans clé)."""
    try:
        import httpx

        url = "https://api.alternative.me/fng/"
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return int(resp.json()["data"][0]["value"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Récupération Fear & Greed échouée (%s)", exc)
        return None
