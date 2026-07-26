"""Conversion prix <-> PIPS, par classe d'actif.

La stratégie impose un objectif minimum de **100 pips**. Le pip n'a de définition standard qu'en
forex et sur les métaux ; ailleurs on utilise une équivalence honnête et explicite :

- Forex majeur (EUR/USD…)   : 1 pip = 0,0001
- Paires en JPY             : 1 pip = 0,01
- Or (XAU) / Platine / Pall.: 1 pip = 0,1   (convention broker : 100 pips = 10 $)
- Argent (XAG)              : 1 pip = 0,01
- Crypto & actions          : 1 pip = 1 point de base du prix (0,01 %) -> 100 pips = 1 % de mouvement,
  soit exactement l'amplitude que représentent 100 pips sur EUR/USD. L'objectif reste donc
  comparable d'un marché à l'autre au lieu d'être absurde (100 pips « bruts » sur BTC ne veut rien
  dire).

`pip_size` dépend du PRIX pour les actifs en points de base : on l'appelle toujours avec le prix.
"""

from __future__ import annotations

_FX = {"EUR", "USD", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "SEK", "NOK", "MXN", "ZAR"}
_METALS = {"XAU": 0.1, "XPT": 0.1, "XPD": 0.1, "XAG": 0.01}
_CRYPTO_QUOTES = {"USDT", "USDC", "BUSD", "BTC", "ETH", "DAI"}

# Actifs pour lesquels le pip est une convention de marché (et non une équivalence en points de base).
_STANDARD = "standard"
_BASIS = "basis_point"


def pip_kind(symbol: str) -> str:
    """`standard` (forex/métaux : pip conventionnel) ou `basis_point` (crypto/actions : 1 pip = 1 bp)."""
    s = symbol.upper().replace("-", "/")
    if "/" in s:
        base, quote = s.split("/", 1)
        if base in _METALS:
            return _STANDARD
        if quote in _CRYPTO_QUOTES:
            return _BASIS
        if base in _FX and quote in _FX:
            return _STANDARD
    return _BASIS


def pip_size(symbol: str, price: float | None = None) -> float:
    """Valeur d'UN pip en unités de prix pour `symbol`.

    Pour les actifs en points de base (crypto/actions) le pip dépend du prix : 1 pip = prix × 0,0001.
    """
    s = symbol.upper().replace("-", "/")
    if "/" in s:
        base, quote = s.split("/", 1)
        if base in _METALS:
            return _METALS[base]
        if base in _FX and quote in _FX:
            return 0.01 if (quote == "JPY" or base == "JPY") else 0.0001
    # Crypto / actions : 1 pip = 1 point de base du prix (0,01 %).
    ref = price if price and price > 0 else 1.0
    return ref * 0.0001


def to_pips(symbol: str, distance: float, price: float | None = None) -> float:
    """Convertit une distance de prix (valeur absolue) en pips."""
    size = pip_size(symbol, price)
    return abs(distance) / size if size > 0 else 0.0


def from_pips(symbol: str, pips: float, price: float | None = None) -> float:
    """Convertit un nombre de pips en distance de prix."""
    return pips * pip_size(symbol, price)


def label(symbol: str) -> str:
    """Mention à afficher : distingue le pip réel de l'équivalence en points de base."""
    return "pips" if pip_kind(symbol) == _STANDARD else "pips éq. (1 pip = 0,01 % du prix)"
