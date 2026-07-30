"""Zones d'offre/demande et niveaux de support/résistance CLASSÉS par importance.

Deux façons complémentaires de lire les mêmes graphiques :

- **Supply & Demand** (`supply_demand_zones`) : des ZONES (une fourchette de prix), là où un
  déséquilibre a laissé des ordres non exécutés. On les reconnaît à leur signature : une phase de
  calme (petits corps, le marché hésite) immédiatement suivie d'une impulsion violente. Si le prix
  y revient, les ordres restants ont de bonnes chances de le repousser.
- **Support & Résistance** (`ranked_levels`) : des NIVEAUX (un prix), lus sur plusieurs unités de
  temps et notés. Tous les niveaux ne se valent pas — un sommet 4 h touché quatre fois la semaine
  dernière pèse plus lourd qu'un micro-pivot 15 min d'il y a trois jours, et la stratégie doit
  pouvoir faire la différence pour poser un stop ou un objectif au bon endroit.

Les deux servent aux trois étapes : confirmer la tendance, choisir le moment d'entrée, et placer
le stop et l'objectif.
"""

from __future__ import annotations

from app.domain import indicators as ind
from app.domain.indicators import Candle

# Poids d'une unité de temps dans la note d'un niveau : plus l'unité est haute, plus le niveau est
# surveillé par du monde, donc plus il est défendu.
TIMEFRAME_WEIGHT = {"4h": 1.0, "1h": 0.8, "15m": 0.6}


def supply_demand_zones(
    candles: list[Candle], *, lookback: int = 200, impulse_atr: float = 1.5,
    max_zones: int = 6, tf: str = "",
) -> list[dict]:
    """Zones d'offre et de demande de qualité, de la plus proche du prix à la plus lointaine.

    Signature retenue : une **bougie de base** (corps < 50 % de son amplitude — le marché piétine)
    suivie d'une **impulsion** d'au moins `impulse_atr` × ATR sur les trois bougies suivantes. Le
    départ brutal est la preuve qu'il restait des ordres à ce prix ; sans lui, la zone n'est qu'une
    pause sans intérêt.

    `touches` compte les retours du prix dans la zone depuis sa création, et `fresh` dit si elle
    est encore vierge. Une zone déjà retestée trois fois a consommé ses ordres : sa force baisse.

    ``strength`` ∈ [0, 1] combine l'ampleur de l'impulsion et l'usure :
    ``0.4 + 0.3 × min(1, impulsion / (2 × seuil)) + 0.3 × (1 si vierge, sinon 1 / (1 + touches))``.

    Retourne une liste de ``{"kind", "low", "high", "mid", "index", "touches", "fresh",
    "strength", "timeframe"}``. Liste vide si l'historique ne suffit pas — jamais de zone inventée.
    """
    if not candles or len(candles) < 30:
        return []
    window = candles[-lookback:] if len(candles) > lookback else candles
    atr_v = ind.atr(window, 14)
    if not atr_v or atr_v <= 0:
        return []
    threshold = impulse_atr * atr_v
    zones: list[dict] = []
    # On s'arrête 4 bougies avant la fin : une zone a besoin de son impulsion pour exister.
    for i in range(len(window) - 4):
        base = window[i]
        span = base.high - base.low
        if span <= 0 or abs(base.close - base.open) > 0.5 * span:
            continue                      # pas une bougie de base : le marché tranchait déjà
        after = window[i + 1 : i + 4]
        up_move = max(c.high for c in after) - base.high
        down_move = base.low - min(c.low for c in after)
        if up_move >= threshold and up_move >= down_move:
            kind, impulse = "demand", up_move
        elif down_move >= threshold:
            kind, impulse = "supply", down_move
        else:
            continue
        zones.append({"kind": kind, "low": base.low, "high": base.high, "index": i,
                      "impulse": impulse, "timeframe": tf})

    price = window[-1].close
    out: list[dict] = []
    for z in zones:
        # Retours du prix dans la zone après sa formation : chaque test consomme des ordres.
        touches = sum(
            1 for c in window[z["index"] + 4 :]
            if c.low <= z["high"] and c.high >= z["low"]
        )
        fresh = touches == 0
        wear = 1.0 if fresh else 1.0 / (1.0 + touches)
        strength = 0.4 + 0.3 * min(1.0, z["impulse"] / (2 * threshold)) + 0.3 * wear
        out.append({
            "kind": z["kind"], "low": round(z["low"], 8), "high": round(z["high"], 8),
            "mid": round((z["low"] + z["high"]) / 2, 8), "index": z["index"],
            "touches": touches, "fresh": fresh, "strength": round(min(1.0, strength), 3),
            "timeframe": z["timeframe"],
        })
    # Les zones les plus récentes priment en cas d'égalité de distance : elles sont les plus vives.
    out.sort(key=lambda z: (abs(z["mid"] - price), -z["index"]))
    return out[:max_zones]


def zone_containing(zones: list[dict], price: float, kind: str) -> dict | None:
    """Zone du type demandé (``"demand"`` / ``"supply"``) dans laquelle le prix se trouve."""
    for z in zones:
        if z["kind"] == kind and z["low"] <= price <= z["high"]:
            return z
    return None


def ranked_levels(
    m15: list[Candle], h1: list[Candle] | None, h4: list[Candle] | None, price: float,
    *, lookback: int = 250, strength: int = 2, max_levels: int = 8,
) -> list[dict]:
    """Supports et résistances des trois unités de temps d'exécution, notés par importance.

    Les pivots des trois unités sont regroupés (un même prix vu en 4 h et en 15 min est UN niveau,
    pas deux) puis notés :

        score = poids_unité × poids_touches × poids_récence

    - `poids_unité` = la plus haute unité qui voit ce niveau (4 h 1,0 · 1 h 0,8 · 15 min 0,6) ;
    - `poids_touches` = ``min(1, 0.4 + 0.2 × nombre de touches)`` — un niveau touché quatre fois
      est un niveau que le marché défend, un pivot isolé ne prouve rien ;
    - `poids_récence` = 1,0 si la dernière touche date de moins de 50 bougies, puis décroît
      linéairement jusqu'à 0,5 à 250 bougies. Un niveau vieux d'un an n'a pas disparu, mais il a
      moins de participants qui s'en souviennent.

    Retourne une liste triée par distance au prix : ``{"price", "side", "score", "touches",
    "timeframes", "last_touch_bars"}``. `side` est lu par rapport au prix courant.
    """
    sources = [(m15, "15m"), (h1 or [], "1h"), (h4 or [], "4h")]
    raw: list[dict] = []
    atrs: list[float] = []
    for candles, tf in sources:
        if not candles or len(candles) < 20:
            continue
        window = candles[-lookback:] if len(candles) > lookback else candles
        atr_v = ind.atr(window, 14)
        if atr_v:
            atrs.append(atr_v)
        sw = ind.swing_points(window, strength, strength)
        n = len(window)
        for idx, lvl in sw["highs"] + sw["lows"]:
            raw.append({"price": lvl, "tf": tf, "bars_ago": n - 1 - idx, "window": window})
    if not raw:
        return []
    # Tolérance de regroupement : une fraction de l'ATR le plus FIN disponible, pour ne pas fondre
    # deux niveaux 15 min distincts sous prétexte que l'ATR 4 h est large.
    tolerance = (min(atrs) * 0.5) if atrs else (price * 0.0002)

    raw.sort(key=lambda r: r["price"])
    clusters: list[dict] = []
    for item in raw:
        if clusters and abs(item["price"] - clusters[-1]["price"]) <= tolerance:
            grp = clusters[-1]
            n = grp["members"]
            grp["price"] = (grp["price"] * n + item["price"]) / (n + 1)
            grp["members"] = n + 1
            grp["timeframes"].add(item["tf"])
            grp["bars_ago"] = min(grp["bars_ago"], item["bars_ago"])
        else:
            clusters.append({"price": item["price"], "members": 1, "bars_ago": item["bars_ago"],
                             "timeframes": {item["tf"]}, "window": item["window"]})

    out: list[dict] = []
    for grp in clusters:
        lvl = grp["price"]
        touches = _count_touches(grp["window"], lvl, tolerance)
        tf_w = max(TIMEFRAME_WEIGHT.get(tf, 0.6) for tf in grp["timeframes"])
        touch_w = min(1.0, 0.4 + 0.2 * touches)
        bars = grp["bars_ago"]
        if bars <= 50:
            recency_w = 1.0
        else:
            recency_w = max(0.5, 1.0 - 0.5 * (bars - 50) / 200)
        out.append({
            "price": round(lvl, 8),
            "side": "support" if lvl < price else "resistance",
            "score": round(tf_w * touch_w * recency_w, 3),
            "touches": touches,
            "timeframes": sorted(grp["timeframes"]),
            "last_touch_bars": bars,
        })
    out.sort(key=lambda lv: abs(lv["price"] - price))
    return out[:max_levels]


def _count_touches(candles: list[Candle], level: float, tolerance: float) -> int:
    """Nombre de fois où le prix est venu chercher ce niveau (mèche comprise).

    On compte les VISITES, pas les bougies : dix bougies collées au niveau pendant une
    consolidation forment une seule touche, sinon un range trivial vaudrait un support majeur.
    """
    touches = 0
    inside = False
    for c in candles:
        near = (c.low - tolerance) <= level <= (c.high + tolerance)
        if near and not inside:
            touches += 1
        inside = near
    return touches


def nearest_level(levels: list[dict], price: float, side: str, *, min_score: float = 0.0) -> dict | None:
    """Niveau le plus proche du prix, du côté demandé, au-dessus d'une note minimale."""
    candidates = [lv for lv in levels if lv["side"] == side and lv["score"] >= min_score]
    if not candidates:
        return None
    return min(candidates, key=lambda lv: abs(lv["price"] - price))
