"""Connecteur Twelve Data — FILET DE SÉCURITÉ pour les bougies, jamais une source primaire.

POURQUOI CE RÔLE, ET PAS UN AUTRE

Mesuré le 01/08/2026 sur le plan gratuit de la clé fournie :

- servis correctement en 15min / 1h / 1day : le **forex** (EUR/USD, GBP/JPY, USD/CHF), la **crypto**
  (BTC/USD, ETH/USD), les **actions** (AAPL, MSFT) et l'**or** (XAU/USD) ;
- refusés faute de plan payant : **XAG/USD** et les **indices** (« This symbol is available starting
  with the Grow or Venture plan ») ;
- et surtout : **~8 requêtes par minute**, au-delà desquelles l'API répond « You have run out of API
  credits for the current minute ».

Le balayage du desk demande 420 requêtes par cycle de 240 s. Faire de Twelve Data une source
primaire l'épuiserait en quelques secondes, et le quota serait consommé par des rafraîchissements
d'affichage au lieu des chemins qui décident réellement d'un trade. Il est donc placé en DERNIER
recours dans la cascade : on ne l'interroge que si tous les autres fournisseurs ont rendu vide.

Un régulateur interne (`_slot`) espace les appels pour rester sous le quota, et un compteur refuse
proprement la requête plutôt que de gaspiller un crédit dans un appel voué au 429.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_URL = "https://api.twelvedata.com/time_series"

# Nos unités de temps -> celles de Twelve Data.
_INTERVAL = {"5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h",
             "1d": "1day", "1w": "1week", "1M": "1month"}

# Symboles refusés par le plan gratuit : on ne dépense pas un crédit pour se faire refuser.
# (Mesuré : XAG/USD et les indices exigent le plan « Grow » ou « Venture ».)
_HORS_PLAN = {"XAG/USD", "XPT/USD", "XPD/USD",
              "SPX500", "NAS100", "US30", "GER40", "UK100", "FRA40", "EU50",
              "JPN225", "HK50", "AUS200"}


def to_symbol(symbol: str) -> str | None:
    """Convertit un symbole interne en symbole Twelve Data — `None` s'il est hors plan.

    La crypto y est cotée en USD et non en USDT : BTC/USDT devient BTC/USD. Les deux suivent le
    même prix à la fraction de pour cent près (USDT est un stablecoin indexé sur le dollar), ce qui
    est sans conséquence pour un filet de secours dont le rôle est d'éviter une page vide.
    """
    s = (symbol or "").upper()
    if s in _HORS_PLAN:
        return None
    if s.endswith("/USDT"):
        return s[: -len("/USDT")] + "/USD"
    return s


# Régulateur de quota : on s'auto-limite au lieu de brûler des crédits dans des refus.
_lock = asyncio.Lock()
_appels: list[float] = []   # horodatages des appels de la dernière minute


async def _slot() -> bool:
    """Réserve un crédit pour la minute en cours. Faux si le quota est déjà consommé.

    On REFUSE plutôt que d'attendre : ce connecteur est un dernier recours, appelé dans une cascade
    dont l'appelant a déjà patienté sur deux fournisseurs. Le faire attendre une minute de plus
    bloquerait la page ; mieux vaut rendre la main et laisser le prochain passage réessayer.
    """
    rpm = get_settings().twelve_data_max_rpm
    if rpm <= 0:
        return True
    async with _lock:
        now = _time.monotonic()
        récents = [t for t in _appels if now - t < 60.0]
        _appels[:] = récents
        if len(récents) >= rpm:
            return False
        _appels.append(now)
        return True


def reset() -> None:
    """Vide le compteur de quota (tests)."""
    _appels.clear()


async def fetch_ohlcv(symbol: str, interval: str = "1h", limit: int = 200) -> list[dict]:
    """Bougies réelles via Twelve Data. Retourne [] si indisponible — ne fabrique jamais rien.

    Le format rendu est celui du projet : [{time, open, high, low, close, volume}], time en
    secondes UNIX et bougies en ordre CHRONOLOGIQUE (Twelve Data les rend du plus récent au plus
    ancien — les servir telles quelles inverserait toutes les lectures d'indicateurs).
    """
    from datetime import UTC, datetime

    import httpx

    s = get_settings()
    if not s.twelve_data_api_key:
        return []
    sym = to_symbol(symbol)
    if sym is None:
        return []                      # hors plan gratuit : inutile d'y dépenser un crédit
    if not await _slot():
        logger.debug("Twelve Data : quota de la minute atteint, %s non demandé", symbol)
        return []

    params = {"symbol": sym, "interval": _INTERVAL.get(interval, "1h"),
              "outputsize": min(max(limit, 1), 5000), "apikey": s.twelve_data_api_key}
    from app.data.http import client as shared_client   # connexion réutilisée (cf. data/http.py)

    client = await shared_client("twelvedata", timeout=httpx.Timeout(20.0, connect=3.0))
    resp = await client.get(_URL, params=params)
    resp.raise_for_status()
    data = resp.json()

    valeurs = data.get("values")
    if not valeurs:
        # `message` porte le motif exact (quota, symbole hors plan…) : on le journalise pour que
        # l'indisponibilité reste explicable, jamais silencieuse.
        logger.debug("Twelve Data sans données pour %s (%s)", symbol,
                     str(data.get("message") or data.get("status"))[:120])
        return []

    lignes: list[dict] = []
    for v in valeurs:
        try:
            dt = datetime.fromisoformat(v["datetime"]).replace(tzinfo=UTC)
            lignes.append({
                "time": int(dt.timestamp()),
                "open": float(v["open"]), "high": float(v["high"]),
                "low": float(v["low"]), "close": float(v["close"]),
                "volume": float(v.get("volume") or 0.0),
            })
        except (KeyError, ValueError, TypeError):
            continue                   # une bougie illisible n'invalide pas la série
    lignes.sort(key=lambda r: r["time"])   # Twelve Data rend du plus RÉCENT au plus ancien
    return lignes[-limit:]
