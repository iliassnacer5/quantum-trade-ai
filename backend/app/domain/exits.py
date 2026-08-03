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

# --- FIABILITÉ D'UN NIVEAU QUI PORTE LE STOP ---------------------------------------------------
# Le stop ne va plus au niveau le plus PROCHE mais au plus SOLIDE : un support testé douze fois sur
# trois unités de temps protège mieux qu'un micro-pivot touché une fois, même s'il est plus loin.
# Chaque poids est multiplié par la solidité MESURÉE du niveau (note du S/R, force de la zone,
# nombre de pivots alignés du pool), pour que ce soit le graphique qui tranche et non un classement
# figé. Un `LEVEL_WEIGHT` à 1,00 sur une note de 0,5 vaut donc 0,50, sous une zone à 0,95.
#
# L'ordre traduit ce que chaque niveau PROUVE, pas un gain constaté : il n'a pas été validé par un
# backtest, et c'est écrit ici pour que personne ne le prenne pour une mesure.
LEVEL_WEIGHT = 1.00        # S/R classé : la note intègre déjà touches et multi-unités de temps
LIQUIDITY_WEIGHT = 0.95    # au-delà d'un pool : le seul placement qui protège d'un balayage
ZONE_WEIGHT = 0.90         # zone d'offre/demande d'entrée : sa traversée tue l'idée
BREAK_WEIGHT = 0.85        # niveau cassé par le BOS/CHOCH, pondéré par la fraîcheur de la cassure
PIVOT_WEIGHT = 0.70        # dernier HL/LH : solide, mais un seul point de contact
# Un stop placé entre le prix et un pool de liquidité sera servi lors du balayage : on le dégrade
# fortement plutôt que de l'interdire — mieux vaut un stop exposé que pas de trade du tout.
SWEEP_PENALTY = 0.35


def plan_stop(
    *, entry: float, direction: int, h4: list[Candle], zones: list[dict],
    sr_levels: list[dict], structure: dict, atr15: float,
    min_distance: float, max_distance: float,
    fallback: tuple[float, str] | None = None,
    liquidity: list[dict] | None = None,
    bos: dict | None = None, choch: dict | None = None,
) -> tuple[float, str]:
    """Place le stop au niveau qui INVALIDE le scénario, et le nomme.

    Chaque candidat est un NIVEAU qui, s'il est traversé, rend le scénario faux :

    1. **la zone d'offre/demande d'où l'on entre** — si le prix la traverse, les ordres qu'on
       attendait n'étaient pas là : l'idée est morte ;
    2. **le dernier creux plus haut (ou sommet plus bas)** — sa perte casse la séquence HH/HL qui
       définissait la tendance ;
    3. **le niveau cassé par le BOS / CHOCH** — on est entré PARCE QUE ce niveau a cédé ; si le prix
       repasse derrière, la cassure était fausse et il n'y a plus de raison d'être là ;
    4. **au-delà d'un pool de liquidité** — voir plus bas, c'est le seul candidat qui protège ;
    5. **un support/résistance classé** suffisamment solide ;
    6. à défaut, la **structure 4 h** (`fallback`).

    **Le choix ne se fait plus sur la distance, mais sur la FIABILITÉ du niveau.** Le code retenait
    auparavant le premier candidat de la liste tombant dans la bande, c'est-à-dire le plus serré :
    un micro-pivot touché une fois passait devant une résistance testée douze fois sur trois unités
    de temps, au seul motif qu'il était plus proche. On classe désormais par solidité MESURÉE du
    niveau — nombre de touches, confirmation multi-unités de temps, force de la zone, nombre de
    pivots alignés — et la distance ne sert plus qu'à départager deux niveaux aussi solides (le plus
    serré gagne alors, il donne le meilleur R/R).

    **Les pools de liquidité sont traités à part, parce qu'ils se comportent à l'envers.** Un stop
    posé ENTRE le prix et un amas de creux égaux est le premier servi quand le marché vient balayer
    cet amas — souvent juste avant de repartir dans notre sens. Un tel candidat est donc fortement
    dégradé (`SWEEP_PENALTY`), et le passage AU-DELÀ du pool est proposé comme candidat à part
    entière. Quand il tient dans la bande de risque, c'est lui qui gagne.

    Réserve à ne pas cacher : cette hiérarchie de fiabilité s'appuie sur des grandeurs réellement
    mesurées sur le graphique (touches, unités de temps, alignements), mais l'ORDRE lui-même n'a pas
    été validé par un backtest — il traduit ce que les niveaux prouvent, pas un gain constaté.

    Retourne ``(prix, motif)`` — le motif nomme toujours le niveau retenu.
    """
    buffer = LEVEL_BUFFER_ATR * max(atr15, 0.0)
    sign = 1 if direction > 0 else -1
    # (prix, motif, fiabilité) — la fiabilité décide, la distance ne fait que départager.
    candidates: list[tuple[float, str, float]] = []

    def _beyond(level: float) -> float:
        """Le stop se pose TOUJOURS derrière le niveau, jamais dessus (une mèche suffirait)."""
        return level - sign * buffer

    kind = "demand" if direction > 0 else "supply"
    for z in zones:
        if z["kind"] != kind:
            continue
        edge = z["low"] if direction > 0 else z["high"]
        side_txt = "de demande" if direction > 0 else "d'offre"
        candidates.append((
            _beyond(edge),
            f"zone {side_txt} {edge:.6g} (force {z['strength']:.2f}) — traversée, elle invalide "
            f"l'entrée",
            ZONE_WEIGHT * float(z["strength"]),
        ))

    pivot = structure.get("last_higher_low") if direction > 0 else structure.get("last_lower_high")
    if pivot is not None:
        label = "dernier creux plus haut" if direction > 0 else "dernier sommet plus bas"
        candidates.append((
            _beyond(pivot),
            f"{label} {pivot:.6g} — sa perte casse la structure de la tendance",
            PIVOT_WEIGHT,
        ))

    # --- BOS / CHOCH : le niveau dont la cassure a justifié l'entrée ---------------------------
    # Sa reprise en sens inverse est l'invalidation la plus littérale qui soit : on est entré parce
    # qu'il a cédé. Plus la cassure est FRAÎCHE, plus ce niveau est pertinent — une cassure de
    # vingt bougies a déjà été digérée par le marché.
    for label, event in (("cassure de structure (BOS)", bos), ("changement de caractère (CHOCH)", choch)):
        if not event or event.get("level") is None:
            continue
        level = float(event["level"])
        # Un niveau du mauvais côté du prix ne peut pas porter un stop.
        if (direction > 0 and level >= entry) or (direction < 0 and level <= entry):
            continue
        freshness = max(0.0, 1.0 - float(event.get("bars_ago", 0)) / 20.0)
        candidates.append((
            _beyond(level),
            f"{label} {level:.6g} — le repasser annule la cassure qui a justifié l'entrée",
            BREAK_WEIGHT * (0.6 + 0.4 * freshness),
        ))

    # --- Pools de liquidité : passer AU-DELÀ, jamais devant --------------------------------------
    # Côté du stop : sous le prix pour un achat (creux égaux), au-dessus pour une vente.
    pool_side = "low" if direction > 0 else "high"
    threats: list[dict] = []
    for pool in (liquidity or []):
        if pool["side"] != pool_side:
            continue
        level = float(pool["price"])
        if (direction > 0 and level >= entry) or (direction < 0 and level <= entry):
            continue
        threats.append(pool)
        kind_txt = "creux égaux" if direction > 0 else "sommets égaux"
        candidates.append((
            _beyond(level),
            f"au-delà du pool de liquidité {level:.6g} ({pool['touches']} {kind_txt}) — "
            f"un stop posé avant lui serait balayé",
            LIQUIDITY_WEIGHT * float(pool["strength"]),
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
            _beyond(price),
            f"{'support' if direction > 0 else 'résistance'} {price:.6g} en {tf} "
            f"(touché {lv['touches']} fois, note {lv['score']:.2f})",
            LEVEL_WEIGHT * float(lv["score"]),
        ))

    # --- Dégradation des candidats exposés à un balayage ----------------------------------------
    # Un stop situé ENTRE le prix et un pool sera pris quand le marché ira chercher ce pool. On ne
    # l'interdit pas (il vaut mieux un stop exposé que pas de trade), on le fait perdre.
    def _exposed(price: float) -> bool:
        return any(
            (price > float(p["price"]) if direction > 0 else price < float(p["price"]))
            for p in threats
        )

    scored = [
        (price, why, reliability * (SWEEP_PENALTY if _exposed(price) else 1.0))
        for price, why, reliability in candidates
    ]

    # Seuls les candidats dont la distance tient dans la bande de risque sont éligibles.
    eligible = [
        (price, why, reliability) for price, why, reliability in scored
        if min_distance <= abs(entry - price) <= max_distance
    ]
    if eligible:
        # Fiabilité d'abord ; à fiabilité égale, le stop le plus serré (meilleur R/R).
        best = max(eligible, key=lambda c: (round(c[2], 6), -abs(entry - c[0])))
        return best[0], best[1]
    if fallback is not None:
        return fallback
    # Dernier recours : la distance minimale exigée, en le disant clairement.
    return entry - sign * min_distance, "distance minimale (aucun niveau exploitable)"


def plan_targets(
    *, entry: float, stop: float, direction: int, zones: list[dict], sr_levels: list[dict],
    structure: dict, fib_ext: dict | None, barrier: float | None,
    min_rr: float, max_rr: float, atr15: float, floor_distance: float = 0.0,
    liquidity: list[dict] | None = None,
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

    # --- Pools de liquidité DEVANT nous : des aimants, pas des obstacles -------------------------
    # Côté opposé au stop : les sommets égaux au-dessus pour un achat, les creux égaux en dessous
    # pour une vente. Le prix va souvent les chercher précisément parce qu'il y a des ordres à y
    # prendre — c'est une destination probable, donc un bon objectif. On vise JUSTE AVANT l'amas
    # (`- sign * buffer`) : sortir dedans, c'est espérer être servi au milieu du balayage.
    target_pool_side = "high" if direction > 0 else "low"
    for pool in (liquidity or []):
        if pool["side"] != target_pool_side:
            continue
        level = float(pool["price"])
        kind_txt = "sommets égaux" if direction > 0 else "creux égaux"
        candidates.append((
            level - sign * buffer,
            f"pool de liquidité {level:.6g} ({pool['touches']} {kind_txt}) — "
            f"le prix va souvent y chercher les ordres accumulés",
        ))

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
