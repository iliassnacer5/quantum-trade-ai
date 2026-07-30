"""ÉTAPE 3 de la stratégie — où sortir : le stop et les objectifs, calculés AVANT l'ordre.

Le stop et l'objectif sont décidés par les mêmes outils que l'entrée (zones d'offre/demande,
structure du marché, supports et résistances classés, extensions de Fibonacci), sur les unités de
temps 15 min et 1 h. Deux règles gouvernent tout ce module :

- **Le stop n'est jamais une distance, c'est un niveau.** Il est placé là où le scénario devient
  FAUX : sous la zone de demande qui devait tenir, sous le dernier creux plus haut qui portait la
  tendance. Un stop posé « à 50 pips » ne dit rien du marché ; s'il est touché, on ne sait même pas
  si l'idée était mauvaise.
- **L'objectif se pose devant un obstacle réel, pas au bout d'un calcul.** Viser au-delà d'une
  résistance que tout le monde surveille, c'est parier en plus qu'elle sera traversée. On préfère
  sortir juste devant.

Le rapport risque/rendement qui en résulte doit tomber dans la bande configurée (1:2 à 1:3). S'il
n'y tombe pas, il n'y a pas de trade : c'est une condition bloquante, jamais un ajustement.
"""

from __future__ import annotations

from app.domain import indicators as ind
from app.domain import market_structure as ms
from app.domain.indicators import Candle

# Marge laissée entre le niveau et le stop (ou l'objectif), en multiple de l'ATR 15 min. Un stop
# posé pile sur le support serait touché par la première mèche qui vient le tester.
LEVEL_BUFFER_ATR = 0.25
# Un niveau de support/résistance n'est retenu pour porter un stop qu'au-dessus de cette note :
# en dessous, c'est un micro-pivot que le marché ne défend pas.
MIN_LEVEL_SCORE = 0.5


def plan_stop(
    *, entry: float, direction: int, h4: list[Candle], zones: list[dict],
    sr_levels: list[dict], structure: dict, atr15: float,
    min_distance: float, max_distance: float,
    fallback: tuple[float, str] | None = None,
) -> tuple[float, str]:
    """Place le stop au niveau qui INVALIDE le scénario, et le nomme.

    Les candidats sont essayés dans l'ordre de ce qu'ils prouvent :

    1. **la zone d'offre/demande d'où l'on entre** — si le prix la traverse, les ordres qu'on
       attendait n'étaient pas là : l'idée est morte ;
    2. **le dernier creux plus haut (ou sommet plus bas)** — sa perte casse la séquence HH/HL qui
       définissait la tendance ;
    3. **un support/résistance classé** suffisamment solide ;
    4. à défaut, la **structure 4 h** (`fallback`), qui reste la référence quand rien de plus fin
       n'est exploitable.

    Le premier candidat dont la distance tombe dans ``[min_distance, max_distance]`` gagne : c'est
    le stop le plus SERRÉ qui ait encore un sens de marché, donc le meilleur rapport possible.

    Retourne ``(prix, motif)`` — le motif nomme toujours le niveau retenu.
    """
    buffer = LEVEL_BUFFER_ATR * max(atr15, 0.0)
    candidates: list[tuple[float, str]] = []

    kind = "demand" if direction > 0 else "supply"
    for z in zones:
        if z["kind"] != kind:
            continue
        edge = z["low"] if direction > 0 else z["high"]
        side_txt = "de demande" if direction > 0 else "d'offre"
        candidates.append((
            edge - buffer if direction > 0 else edge + buffer,
            f"zone {side_txt} {edge:.6g} (force {z['strength']:.2f}) — traversée, elle invalide "
            f"l'entrée",
        ))

    pivot = structure.get("last_higher_low") if direction > 0 else structure.get("last_lower_high")
    if pivot is not None:
        label = "dernier creux plus haut" if direction > 0 else "dernier sommet plus bas"
        candidates.append((
            pivot - buffer if direction > 0 else pivot + buffer,
            f"{label} {pivot:.6g} — sa perte casse la structure de la tendance",
        ))

    side = "support" if direction > 0 else "resistance"
    for lv in sorted(sr_levels, key=lambda x: abs(x["price"] - entry)):
        if lv["side"] != side or lv["score"] < MIN_LEVEL_SCORE:
            continue
        price = lv["price"]
        if (direction > 0 and price >= entry) or (direction < 0 and price <= entry):
            continue
        tf = "/".join(lv["timeframes"])
        candidates.append((
            price - buffer if direction > 0 else price + buffer,
            f"{'support' if direction > 0 else 'résistance'} {price:.6g} en {tf} "
            f"(touché {lv['touches']} fois, note {lv['score']:.2f})",
        ))

    for price, why in candidates:
        distance = abs(entry - price)
        if min_distance <= distance <= max_distance:
            return price, why
    if fallback is not None:
        return fallback
    # Dernier recours : la distance minimale exigée, en le disant clairement.
    return entry - (1 if direction > 0 else -1) * min_distance, "distance minimale (aucun niveau exploitable)"


def plan_targets(
    *, entry: float, stop: float, direction: int, zones: list[dict], sr_levels: list[dict],
    structure: dict, fib_ext: dict | None, barrier: float | None,
    min_rr: float, max_rr: float, atr15: float, floor_distance: float = 0.0,
) -> dict:
    """Choisit TP1 et TP2 sur des niveaux RÉELS, dans la bande de risque/rendement autorisée.

    Les candidats — résistances classées, bord proche des zones opposées, extensions de Fibonacci,
    dernier sommet de structure — sont triés du plus proche au plus lointain, puis filtrés :

    - **TP1** est le premier candidat situé à au moins ``min_rr × risque`` et pas au-delà de
      ``max_rr × risque``. C'est l'objectif que le prix a le plus de chances d'atteindre, parce
      qu'il est le plus proche tout en payant le risque pris. À défaut de niveau, il est posé
      arithmétiquement à ``min_rr × risque`` — et le motif le dit.
    - **TP2** est le candidat suivant, plafonné au R/R maximum et au niveau majeur opposé. Il vaut
      ``None`` quand il se confond avec TP1 : mieux vaut pas de second objectif qu'un doublon.

    `floor_distance` est un plancher absolu (converti depuis un objectif minimum en pips, quand la
    configuration en impose un). Il RELÈVE la distance minimale exigée au lieu de laisser le setup
    être refusé plus loin : viser plus haut est une décision de construction, pas un rejet.

    Retourne ``{"tp1", "tp2", "rr", "rr_ok", "target_basis", "tp2_basis", "candidates"}``.
    """
    risk = abs(entry - stop)
    sign = 1 if direction > 0 else -1
    buffer = LEVEL_BUFFER_ATR * max(atr15, 0.0)
    if risk <= 0:
        return {"tp1": None, "tp2": None, "rr": 0.0, "rr_ok": False,
                "target_basis": "risque nul", "tp2_basis": "", "candidates": []}

    min_dist = max(min_rr * risk, floor_distance)
    max_dist = max_rr * risk

    candidates: list[tuple[float, str]] = []
    side = "resistance" if direction > 0 else "support"
    for lv in sr_levels:
        if lv["side"] != side:
            continue
        price = lv["price"] - sign * buffer
        tf = "/".join(lv["timeframes"])
        candidates.append((price, f"{'résistance' if direction > 0 else 'support'} "
                                  f"{lv['price']:.6g} en {tf} (note {lv['score']:.2f})"))

    opposite = "supply" if direction > 0 else "demand"
    for z in zones:
        if z["kind"] != opposite:
            continue
        # On vise le bord PROCHE de la zone : c'est là que les ordres opposés commencent.
        edge = z["low"] if direction > 0 else z["high"]
        side_txt = "d'offre" if direction > 0 else "de demande"
        candidates.append((edge - sign * buffer,
                           f"zone {side_txt} {edge:.6g} (force {z['strength']:.2f})"))

    if fib_ext:
        for name, price in fib_ext.get("levels", {}).items():
            candidates.append((price, f"extension de Fibonacci {name} ({price:.6g})"))

    swing = structure.get("last_swing_high") if direction > 0 else structure.get("last_swing_low")
    if swing is not None:
        candidates.append((swing - sign * buffer,
                           f"dernier sommet de structure {swing:.6g}"
                           if direction > 0 else f"dernier creux de structure {swing:.6g}"))

    # Ne restent que les niveaux DEVANT nous, du plus proche au plus lointain.
    ahead = sorted(
        ((p, why) for p, why in candidates if (p - entry) * sign > 0),
        key=lambda c: abs(c[0] - entry),
    )

    tp1 = None
    tp1_basis = ""
    rest: list[tuple[float, str]] = []
    for price, why in ahead:
        distance = abs(price - entry)
        if distance < min_dist:
            continue                       # trop proche : le prix le franchira sans nous payer
        if distance > max_dist:
            break                          # au-delà de la bande : plus rien d'utilisable
        if tp1 is None:
            tp1, tp1_basis = price, why
        else:
            rest.append((price, why))

    if tp1 is None:
        tp1 = entry + sign * min_dist
        tp1_basis = (f"objectif arithmétique au R/R minimum 1:{min_rr:g} — aucun niveau de marché "
                     f"ne tombe dans la bande autorisée")
        if floor_distance > min_rr * risk:
            tp1_basis = ("objectif arithmétique porté au plancher imposé par la configuration — "
                         "aucun niveau de marché ne tombe dans la bande autorisée")

    def _cap(value: float) -> float:
        if barrier is None:
            return value
        return min(value, barrier) if direction > 0 else max(value, barrier)

    if rest:
        tp2, tp2_basis = rest[-1][0], rest[-1][1]
    else:
        tp2 = entry + sign * max_dist
        tp2_basis = f"R/R maximum 1:{max_rr:g}"
    tp2 = _cap(tp2)
    if barrier is not None and abs(tp2 - barrier) < 1e-12:
        tp2_basis += f", plafonné par le niveau majeur {barrier:.6g}"
    # Un TP2 confondu avec TP1 (ou derrière lui) n'est pas un objectif : on n'en affiche pas.
    if (tp2 - entry) * sign <= 0 or abs(tp2 - tp1) <= max(buffer, 1e-9):
        tp2, tp2_basis = None, ""

    rr = abs(tp1 - entry) / risk
    return {
        "tp1": tp1, "tp2": tp2, "rr": rr,
        "rr_ok": min_rr - 0.01 <= rr <= max_rr + 0.01,
        "target_basis": tp1_basis, "tp2_basis": tp2_basis,
        "candidates": [why for _, why in ahead[:5]],
    }


def tp1_lock_stop(entry: float, tp1: float, direction: int, *, fraction: float) -> float:
    """Niveau où remonter le stop quand TP1 est touché et que la course vers TP2 est confirmée.

    On sécurise `fraction` du chemin déjà parcouru (80 % par défaut) : la position garde de quoi
    respirer pour aller chercher TP2, tout en ayant verrouillé l'essentiel du gain acquis.
    """
    sign = 1 if direction > 0 else -1
    return entry + sign * fraction * abs(tp1 - entry)


def momentum_still_supports(candles: list[Candle], direction: int) -> dict:
    """Le momentum confirme-t-il de continuer vers TP2, après que TP1 a été touché ?

    Trois questions, toutes nécessaires :

    - le RSI n'est-il pas épuisé dans notre sens (au-delà de 70 à l'achat, sous 30 à la vente) ?
    - l'histogramme MACD pousse-t-il encore dans notre sens ?
    - aucun changement de caractère (CHOCH) récent ne vient-il d'annoncer un retournement ?

    Retourne ``{"ok", "reasons", "rsi", "macd_hist", "choch"}``. En cas d'historique insuffisant,
    ``ok`` est faux : sans information, on ne prolonge pas le risque, on prend le gain.
    """
    out = {"ok": False, "reasons": [], "rsi": None, "macd_hist": None, "choch": None}
    if not candles or len(candles) < 40 or direction == 0:
        out["reasons"].append("historique insuffisant pour juger le momentum")
        return out

    closes = [c.close for c in candles]
    reasons: list[str] = []

    rsi_val = ind.rsi(closes, 14)
    out["rsi"] = round(rsi_val, 1) if rsi_val is not None else None
    if rsi_val is None:
        reasons.append("RSI non calculable")
    elif (direction > 0 and rsi_val > 70) or (direction < 0 and rsi_val < 30):
        reasons.append(f"RSI épuisé ({rsi_val:.0f}) : le mouvement est étiré")

    macd_all = ind.macd_series(closes)
    if macd_all is None:
        reasons.append("MACD non calculable")
    else:
        hist = macd_all[2][-1]
        out["macd_hist"] = round(hist, 8)
        if hist * direction <= 0:
            reasons.append("le momentum MACD n'est plus dans le sens du trade")

    choch = ms.detect_choch(candles, max_bars_ago=6)
    if choch:
        out["choch"] = choch
        if choch["direction"] == -direction:
            reasons.append("changement de caractère récent à contresens")

    out["reasons"] = reasons
    out["ok"] = not reasons
    return out
