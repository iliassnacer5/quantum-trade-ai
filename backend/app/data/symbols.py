"""Catalogue multi-marchés (crypto / forex / actions / indices / métaux) + recherche.

Sert l'UI (sélecteur de symboles) et valide les actifs. La génération de signal accepte tout symbole
(repli synthétique si le connecteur de marché n'a pas de données), mais le catalogue fournit une
liste organisée des paires/symboles populaires par classe d'actif.

La stratégie du desk s'applique à TOUTES ces classes sans distinction : elle lit une tendance et des
niveaux, ce que tout graphique fournit.
"""

from __future__ import annotations

# --- Crypto (paires sur USDT) ---
_CRYPTO_BASES = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "DOT", "MATIC",
    "LINK", "LTC", "TRX", "SHIB", "UNI", "ATOM", "XLM", "NEAR", "ALGO", "FIL",
    "APT", "ARB", "OP", "INJ", "SUI", "SEI", "TIA", "RNDR", "IMX", "AAVE",
]
CRYPTO = [f"{b}/USDT" for b in _CRYPTO_BASES]

# --- Forex (paires majeures + mineures) ---
_FX = ["EUR", "USD", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"]
FOREX = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD", "NZD/USD",
    "EUR/GBP", "EUR/JPY", "GBP/JPY", "EUR/CHF", "AUD/JPY", "EUR/CAD", "GBP/CHF",
]

# --- Actions (grandes capitalisations US) ---
STOCKS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "NFLX", "AMD", "INTC",
    "JPM", "V", "MA", "DIS", "BA", "KO", "PEP", "WMT", "XOM", "CVX",
    "BABA", "ORCL", "CRM", "ADBE", "PYPL", "UBER", "COIN", "PLTR", "SHOP", "SQ",
]

# --- Métaux précieux (or spot & co, données futures COMEX via Yahoo) ---
COMMODITIES = ["XAU/USD", "XAG/USD", "XPT/USD", "XPD/USD"]

# --- Indices boursiers (noms des brokers CFD ; correspondance Yahoo dans `data.yahoo._INDEX_MAP`) ---
INDICES = [
    "SPX500", "NAS100", "US30",                     # États-Unis
    "GER40", "UK100", "FRA40", "EU50",              # Europe
    "JPN225", "HK50", "AUS200",                     # Asie-Pacifique
]

_LABELS = {"crypto": "Crypto", "forex": "Forex", "stock": "Actions",
           "commodity": "Or & Métaux", "index": "Indices"}


def catalog() -> dict[str, list[str]]:
    return {"crypto": CRYPTO, "forex": FOREX, "stock": STOCKS,
            "commodity": COMMODITIES, "index": INDICES}


def all_symbols() -> list[dict]:
    out: list[dict] = []
    for cls, syms in catalog().items():
        out.extend({"symbol": s, "asset_class": cls, "label": _LABELS[cls]} for s in syms)
    return out


def search(query: str | None = None, asset_class: str | None = None, limit: int = 50) -> list[dict]:
    items = all_symbols()
    if asset_class:
        items = [i for i in items if i["asset_class"] == asset_class]
    if query:
        q = query.strip().upper()
        items = [i for i in items if q in i["symbol"].upper()]
    return items[:limit]


def is_known(symbol: str) -> bool:
    return symbol.upper() in {s.upper() for s in (CRYPTO + FOREX + STOCKS + COMMODITIES + INDICES)}


def normalize(symbol: str) -> str:
    """Normalise un symbole saisi librement (ex. 'btcusdt' -> 'BTC/USDT')."""
    s = symbol.strip().upper().replace("-", "/")
    if "/" not in s:
        for quote in ("USDT", "USDC", "USD"):
            if s.endswith(quote) and len(s) > len(quote):
                return f"{s[:-len(quote)]}/{quote}"
    return s
