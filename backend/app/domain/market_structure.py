"""Structure de marché : sommets/creux étiquetés, cassure de structure (BOS) et changement de
caractère (CHOCH).

Un marché ne « monte » pas parce qu'un indicateur le dit : il monte parce qu'il inscrit des sommets
plus hauts (HH) ET des creux plus hauts (HL). C'est la lecture la plus proche de ce qu'un trader
voit sur son écran, et elle sert à deux moments distincts de la stratégie :

- **Étape 1 (tendance)** : `label_swings` confirme que la structure valide la direction mesurée par
  les indicateurs. Une tendance haussière sans HL est une tendance en train de mourir.
- **Étape 2 (entrée)** : `detect_bos` autorise l'entrée seulement APRÈS que la correction a été
  cassée dans le sens de la tendance, et `detect_choch` signale que la correction est terminée.
  Sans eux, on entrerait pendant la correction — c'est-à-dire contre le mouvement en cours.

Tout s'appuie sur `indicators.swing_points` : une seule définition du pivot pour tout le projet.
"""

from __future__ import annotations

from app.domain import indicators as ind
from app.domain.indicators import Candle

# Un pivot doit dominer ce nombre de bougies de chaque côté pour être retenu. À 2, on capte la
# structure fine du 15 min sans transformer chaque respiration en « niveau ».
DEFAULT_STRENGTH = 2


def label_swings(
    candles: list[Candle], *, left: int = DEFAULT_STRENGTH, right: int = DEFAULT_STRENGTH,
    lookback: int = 120,
) -> dict:
    """Étiquette chaque pivot en HH / HL / LH / LL et en déduit l'état de la structure.

    Chaque sommet est comparé au sommet précédent (HH s'il est plus haut, LH sinon) et chaque creux
    au creux précédent (HL s'il est plus haut, LL sinon). Le tout premier pivot de chaque type n'a
    rien à quoi se comparer : il reste non étiqueté plutôt que d'être classé au hasard.

    `state` lit les QUATRE derniers labels : « haussière » exige au moins un HH et un HL sans LL,
    « baissière » au moins un LH et un LL sans HH ; tout le reste est un range. Cette exigence
    croisée évite de déclarer une tendance sur un seul sommet plus haut, qui n'est qu'un rebond.

    Retourne ``{"points", "sequence", "state", "last_swing_high", "last_swing_low",
    "last_higher_low", "last_lower_high"}``.
    """
    empty = {"points": [], "sequence": [], "state": "indéterminée", "last_swing_high": None,
             "last_swing_low": None, "last_higher_low": None, "last_lower_high": None}
    if not candles or len(candles) < left + right + 4:
        return empty
    window = candles[-lookback:] if len(candles) > lookback else candles
    sw = ind.swing_points(window, left, right)
    # Deux sommets « égaux » (à une fraction d'ATR près) ne sont NI plus hauts NI plus bas : c'est
    # un double sommet, la signature d'un range. Sans cette tolérance, le bruit d'un marché plat
    # suffirait à déclarer une tendance, dans un sens ou dans l'autre au hasard des arrondis.
    atr_v = ind.atr(window, 14) or 0.0
    tol = 0.15 * atr_v

    def _label(price: float, previous: float, up_label: str, down_label: str) -> str | None:
        if abs(price - previous) <= tol:
            return "EQ"
        return up_label if price > previous else down_label

    points: list[dict] = []
    prev_high: float | None = None
    for idx, price in sw["highs"]:
        kind = None if prev_high is None else _label(price, prev_high, "HH", "LH")
        points.append({"index": idx, "price": price, "kind": kind, "side": "high"})
        prev_high = price
    prev_low: float | None = None
    for idx, price in sw["lows"]:
        kind = None if prev_low is None else _label(price, prev_low, "HL", "LL")
        points.append({"index": idx, "price": price, "kind": kind, "side": "low"})
        prev_low = price
    points.sort(key=lambda p: p["index"])
    if not points:
        return empty

    sequence = [p["kind"] for p in points if p["kind"]]
    recent = sequence[-4:]
    if "HH" in recent and "HL" in recent and "LL" not in recent:
        state = "haussière"
    elif "LH" in recent and "LL" in recent and "HH" not in recent:
        state = "baissière"
    elif not recent:
        state = "indéterminée"
    else:
        state = "range"

    highs = [p for p in points if p["side"] == "high"]
    lows = [p for p in points if p["side"] == "low"]
    higher_lows = [p for p in lows if p["kind"] == "HL"]
    lower_highs = [p for p in highs if p["kind"] == "LH"]
    return {
        "points": points,
        "sequence": sequence,
        "state": state,
        "last_swing_high": highs[-1]["price"] if highs else None,
        "last_swing_low": lows[-1]["price"] if lows else None,
        # Le dernier HL est le niveau dont la perte invalide une tendance haussière : c'est le
        # candidat naturel pour poser un stop (et le LH, symétriquement, à la vente).
        "last_higher_low": higher_lows[-1]["price"] if higher_lows else None,
        "last_lower_high": lower_highs[-1]["price"] if lower_highs else None,
    }


def detect_bos(
    candles: list[Candle], direction: int, *, lookback: int = 60,
    left: int = DEFAULT_STRENGTH, right: int = DEFAULT_STRENGTH, max_bars_ago: int = 10,
) -> dict | None:
    """Break Of Structure **de reprise** : la correction vient d'être cassée dans le sens voulu.

    On prend le dernier pivot formé PENDANT la correction (le dernier sommet pour un achat, le
    dernier creux pour une vente) et on vérifie qu'une bougie a CLÔTURÉ au-delà. La clôture est
    exigée volontairement : une mèche qui dépasse puis rentre n'est pas une cassure, c'est un piège.

    `max_bars_ago` borne la fraîcheur : une cassure vieille de 20 bougies n'est plus un déclencheur
    d'entrée, le mouvement est déjà parti sans nous.

    Retourne ``{"level", "index", "bars_ago", "close"}`` ou ``None``.
    """
    if direction == 0 or not candles or len(candles) < left + right + 6:
        return None
    window = candles[-lookback:] if len(candles) > lookback else candles
    sw = ind.swing_points(window, left, right)
    pivots = sw["highs"] if direction > 0 else sw["lows"]
    if not pivots:
        return None
    n = len(window)
    # On remonte du pivot le plus récent vers les plus anciens. Le pivot tout juste formé n'a
    # souvent pas encore été franchi (il vient d'être confirmé) : c'est alors le précédent qui
    # porte la cassure. On s'arrête au premier pivot dont la PREMIÈRE clôture au-delà est encore
    # fraîche — la première, car c'est elle qui casse la structure ; les suivantes ne font que
    # prolonger un mouvement déjà lancé.
    for pivot_idx, level in reversed(pivots):
        for i in range(pivot_idx + 1, n):
            close = window[i].close
            if (direction > 0 and close > level) or (direction < 0 and close < level):
                bars_ago = n - 1 - i
                if bars_ago <= max_bars_ago:
                    return {"level": round(level, 8), "index": i, "bars_ago": bars_ago,
                            "close": round(close, 8)}
                break        # cassure trop vieille : ce pivot ne déclenche plus rien
    return None


def detect_choch(
    candles: list[Candle], *, lookback: int = 60, left: int = DEFAULT_STRENGTH,
    right: int = DEFAULT_STRENGTH, max_bars_ago: int = 10,
) -> dict | None:
    """Change Of Character : la micro-tendance en cours vient d'être cassée à contresens.

    Concrètement : dans une micro-baisse (la correction d'une tendance haussière), le prix clôture
    au-dessus du dernier sommet. Ce n'est pas encore un retournement de fond, c'est le signe que
    LA CORRECTION est terminée — exactement ce qu'on attend pour rentrer dans le sens de la
    tendance principale.

    `against` donne le sens de la micro-tendance qui vient d'être cassée : `-1` = une baisse a été
    cassée à la hausse (signal haussier), `+1` = l'inverse.

    Retourne ``{"level", "index", "bars_ago", "against", "direction"}`` ou ``None``.
    """
    if not candles or len(candles) < left + right + 6:
        return None
    window = candles[-lookback:] if len(candles) > lookback else candles
    micro = label_swings(window, left=left, right=right, lookback=lookback)
    state = micro["state"]
    if state == "haussière":
        micro_dir = 1
    elif state == "baissière":
        micro_dir = -1
    else:
        return None      # sans micro-tendance lisible, il n'y a pas de « caractère » à changer
    # On cherche la cassure du dernier pivot situé À CONTRESENS de la micro-tendance.
    sw = ind.swing_points(window, left, right)
    pivots = sw["highs"] if micro_dir < 0 else sw["lows"]
    if not pivots:
        return None
    breakout_dir = -micro_dir
    n = len(window)
    for pivot_idx, level in reversed(pivots):
        for i in range(pivot_idx + 1, n):
            close = window[i].close
            if (breakout_dir > 0 and close > level) or (breakout_dir < 0 and close < level):
                bars_ago = n - 1 - i
                if bars_ago <= max_bars_ago:
                    return {"level": round(level, 8), "index": i, "bars_ago": bars_ago,
                            "against": micro_dir, "direction": breakout_dir}
                break
    return None
