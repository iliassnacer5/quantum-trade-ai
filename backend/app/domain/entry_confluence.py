"""ÉTAPE 2 de la stratégie — QUAND entrer, une fois la tendance validée et figée.

Aucun outil de ce module ne discute la direction : elle vient de `domain.trend` et n'est plus
rediscutée. Ils répondent tous à une seule question — *est-ce le bon moment ?* — et chacun apporte
une **confirmation** pondérée.

La règle de décision est volontairement souple : exiger que tous les signaux s'alignent ne produit
presque jamais d'opportunité. On applique donc une **règle du minimum de confirmations** :

    on entre dès que la somme pondérée des confirmations atteint le seuil (3,0 par défaut),
    avec au moins trois confirmations distinctes, dont au moins une « forte ».

Les outils ne pèsent pas tous pareil. Ce que le prix FAIT (zone d'offre/demande défendue, cassure
de structure, figure de retournement, rebond sur un support majeur) pèse le double de ce qui n'est
qu'un indicateur dérivé du prix (RSI, VWAP, moyennes mobiles). Trois indicateurs secondaires
d'accord entre eux ne valent pas une zone de demande défendue par une avalée haussière.
"""

from __future__ import annotations

from app.domain import indicators as ind
from app.domain import market_structure as ms
from app.domain import price_action as pa
from app.domain import zones as zones_mod
from app.domain.indicators import Candle

# Poids par outil. Les quatre premiers sont les confirmations « fortes » : elles décrivent le
# comportement du prix lui-même. Les suivantes le commentent.
CONFIRMATION_WEIGHTS = {
    "supply_demand": 1.0,
    "structure": 1.0,          # BOS de reprise ou CHOCH de fin de correction
    "price_action": 1.0,
    "support_resistance": 1.0,
    "fibonacci": 0.75,
    "rsi": 0.5,
    "vwap": 0.5,
    "ema_dynamic": 0.5,
    "volume": 0.5,
}
STRONG_KEYS = ("supply_demand", "structure", "price_action", "support_resistance")

MIN_CONFIRMATION_SCORE = 3.0      # somme pondérée minimale
MIN_CONFIRMATIONS = 3             # nombre minimal d'outils distincts
MIN_STRONG_CONFIRMATIONS = 1      # dont au moins une confirmation forte

# Au-delà de ce RSI (en dessous, à la vente), l'entrée est refusée — sauf tendance exceptionnelle.
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
STRONG_TREND_CONFIDENCE = 0.75    # « tendance extrêmement forte » qui lève la garde RSI
RSI_EXHAUSTED_HIGH = 78.0         # épuisement caractérisé : refus absolu, sans exception
RSI_EXHAUSTED_LOW = 22.0
# Un niveau majeur opposé plus proche que cela (en multiple du risque estimé) ne laisse pas la
# place de travailler : le potentiel est bouché avant d'avoir payé le risque.
MIN_ROOM_RR = 2.0
MAJOR_LEVEL_SCORE = 0.7


def parse_weights(raw: str, defaults: dict[str, float] | None = None) -> dict[str, float]:
    """Lit des poids depuis une chaîne ``"supply_demand:1.2,rsi:0.3"`` (cf. `domain.trend`)."""
    out = dict(defaults or CONFIRMATION_WEIGHTS)
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        key, _, value = part.partition(":")
        key = key.strip()
        if key not in out:
            continue
        try:
            out[key] = max(0.0, float(value))
        except ValueError:
            continue
    return out


def evaluate_entry(
    candles: list[Candle], *, direction: int, trend: dict | None = None,
    zones: list[dict] | None = None, sr_levels: list[dict] | None = None,
    metrics: dict | None = None, session: dict | None = None,
    weights: dict[str, float] | None = None,
    min_score: float = MIN_CONFIRMATION_SCORE,
    risk_hint: float = 0.0,
) -> dict:
    """Évalue le moment d'entrée par confluence, dans le sens déjà décidé par la tendance.

    Chaque outil rend une confirmation ou rien ; la contribution vaut ``poids × qualité``, où la
    qualité (entre 0 et 1) dit à quel point le signal est franc — la force de la zone, la note du
    niveau, la netteté de la figure.

    Deux **gardes** se déclenchent avant tout comptage, parce qu'aucune accumulation de
    confirmations ne les rachète :

    - le RSI est étiré dans notre sens (au-delà de 70, ou sous 30) sans que la tendance soit
      exceptionnellement forte : on achèterait un mouvement déjà fait ;
    - un niveau majeur opposé est si proche que l'objectif ne pourrait pas payer le risque.

    Retourne ``{"fired", "score", "min_score", "confirmations", "rejections", "entry", "reason",
    "type"}``.
    """
    w = weights or CONFIRMATION_WEIGHTS
    zones = zones or []
    sr_levels = sr_levels or []
    m = metrics or {}
    out: dict = {"fired": False, "type": "confluence", "score": 0.0, "min_score": min_score,
                 "confirmations": [], "rejections": [], "reason": "", "entry": None}

    if direction == 0 or not candles or len(candles) < 40:
        out["reason"] = "données 15 min insuffisantes pour juger le timing"
        return out

    closes = [c.close for c in candles]
    last = candles[-1]
    price = last.close
    out["entry"] = price
    atr = ind.atr(candles, 14) or (price * 0.001)
    bull = direction > 0
    # Bougie de reprise : le prix s'est retourné dans notre sens sur la dernière bougie. Presque
    # toutes les confirmations l'exigent — sans elle, on attrape un couteau qui tombe.
    reprise = (last.close > last.open) if bull else (last.close < last.open)

    rsi_s = ind.rsi_series(closes, 14)
    rsi = next((v for v in reversed(rsi_s) if v is not None), None) if rsi_s else None
    rsi_prev = next((v for v in reversed(rsi_s[:-1]) if v is not None), rsi) if rsi_s else None
    confidence = float((trend or {}).get("confidence") or 0.0)

    # --- GARDE 1 : RSI étiré -------------------------------------------------------------------
    if rsi is not None:
        if (bull and rsi > RSI_EXHAUSTED_HIGH) or (not bull and rsi < RSI_EXHAUSTED_LOW):
            out["reason"] = f"RSI 15 min en zone d'épuisement ({rsi:.0f}) — entrée trop tardive"
            out["rejections"].append(out["reason"])
            return out
        stretched = (bull and rsi > RSI_OVERBOUGHT) or (not bull and rsi < RSI_OVERSOLD)
        if stretched and confidence < STRONG_TREND_CONFIDENCE:
            out["reason"] = (
                f"RSI {rsi:.0f} : mouvement déjà étiré et la tendance n'est pas assez forte "
                f"(confiance {confidence:.2f} < {STRONG_TREND_CONFIDENCE:g}) pour l'ignorer"
            )
            out["rejections"].append(out["reason"])
            return out

    # --- GARDE 2 : potentiel bouché par un niveau majeur opposé --------------------------------
    opposite = "resistance" if bull else "support"
    blocking = zones_mod.nearest_level(sr_levels, price, opposite, min_score=MAJOR_LEVEL_SCORE)
    risk = risk_hint if risk_hint > 0 else atr
    if blocking is not None and abs(blocking["price"] - price) < MIN_ROOM_RR * risk:
        out["reason"] = (
            f"{'résistance' if bull else 'support'} majeur à {blocking['price']:.6g} "
            f"(note {blocking['score']:.2f}) trop proche : le potentiel ne paierait pas le risque"
        )
        out["rejections"].append(out["reason"])
        return out

    confirmations: list[dict] = []

    def confirm(key: str, quality: float, reading: str) -> None:
        quality = max(0.0, min(1.0, quality))
        confirmations.append({
            "key": key, "weight": w.get(key, 0.0), "quality": round(quality, 3),
            "contribution": round(w.get(key, 0.0) * quality, 3), "reading": reading,
            "strong": key in STRONG_KEYS,
        })

    # --- Supply & Demand : le prix est DANS une zone alignée, et il y réagit -------------------
    kind = "demand" if bull else "supply"
    zone = zones_mod.zone_containing(zones, price, kind)
    if zone is None:
        # Le prix vient peut-être de la quitter en réagissant : on accepte le contact récent.
        recent_low = min(c.low for c in candles[-3:])
        recent_high = max(c.high for c in candles[-3:])
        for z in zones:
            if z["kind"] != kind:
                continue
            touched = (recent_low <= z["high"] and recent_low >= z["low"]) if bull else \
                      (recent_high >= z["low"] and recent_high <= z["high"])
            if touched:
                zone = z
                break
    if zone is not None and zone["strength"] >= 0.5:
        if reprise:
            zone_txt = "de demande" if bull else "d'offre"
            confirm("supply_demand", zone["strength"],
                    f"prix dans la zone {zone_txt} {zone['low']:.6g}–{zone['high']:.6g} "
                    f"(force {zone['strength']:.2f}) avec bougie de réaction")
        else:
            # Le simple contact ne suffit JAMAIS : sans réaction, la zone n'a rien prouvé.
            out["rejections"].append("zone touchée mais aucune bougie de réaction — on attend")

    # --- Structure : BOS de reprise, ou CHOCH de fin de correction ------------------------------
    bos = ms.detect_bos(candles, direction)
    if bos is not None:
        confirm("structure", 1.0,
                f"cassure de structure confirmée au-dessus de {bos['level']:.6g}"
                if bull else f"cassure de structure sous {bos['level']:.6g}")
    else:
        choch = ms.detect_choch(candles)
        if choch is not None and choch["direction"] == direction:
            confirm("structure", 0.7,
                    f"changement de caractère à {choch['level']:.6g} : la correction est terminée")

    # --- Price action : au moins une figure de retournement -------------------------------------
    figure = pa.entry_pattern(candles, direction)
    if figure is not None:
        confirm("price_action", figure[1], f"figure « {figure[0]} » dans le sens du trade")

    # --- Support / Résistance classés : rebond confirmé sur un niveau qui tient ------------------
    own_side = "support" if bull else "resistance"
    level = zones_mod.nearest_level(sr_levels, price, own_side)
    if level is not None and abs(price - level["price"]) <= 1.0 * atr and reprise:
        tf = "/".join(level["timeframes"])
        confirm("support_resistance", level["score"],
                f"rebond sur {'le support' if bull else 'la résistance'} {level['price']:.6g} "
                f"en {tf} (touché {level['touches']} fois)")

    # --- Fibonacci : correction dans la zone d'or, dans le sens du swing ------------------------
    fib = m.get("fibonacci") or {}
    if fib.get("golden_zone") and not fib.get("invalidated"):
        if (fib.get("swing") == "haussier") == bull:
            # Le milieu de la zone (50 %) est le retracement le plus fiable ; les bords le sont moins.
            depth = fib.get("retracement_pct")
            quality = 1.0 if depth is None else max(0.6, 1.0 - abs(depth - 50.0) / 30.0)
            confirm("fibonacci", quality, "correction dans la zone d'or 38,2–61,8 %")

    # --- RSI : confirmation seulement, jamais un motif d'entrée à lui seul ----------------------
    if rsi is not None and rsi_prev is not None:
        turning = (rsi > rsi_prev) if bull else (rsi < rsi_prev)
        leaving_extreme = (bull and rsi_prev < 35 and rsi >= 35) or \
                          (not bull and rsi_prev > 65 and rsi <= 65)
        div = m.get("rsi_divergence")
        want = "haussière" if bull else "baissière"
        if leaving_extreme:
            confirm("rsi", 0.8, f"le RSI sort de la zone extrême ({rsi_prev:.0f} → {rsi:.0f})")
        elif div == want:
            confirm("rsi", 0.8, f"divergence {want} en notre faveur")
        elif turning:
            confirm("rsi", 0.5, f"RSI qui se retourne dans notre sens ({rsi:.0f})")

    # --- VWAP : surtout pendant Londres et New York --------------------------------------------
    vwap_val = m.get("vwap")
    if vwap_val is not None:
        good_side = (price > vwap_val) if bull else (price < vwap_val)
        if good_side:
            prime = bool((session or {}).get("prime"))
            confirm("vwap", 1.0 if prime else 0.6,
                    f"prix du bon côté du VWAP ({vwap_val:.6g})"
                    + (" en séance de forte activité" if prime else ""))

    # --- EMA 20 / EMA 50 comme supports et résistances dynamiques -------------------------------
    ema20 = ind.ema(closes, 20)
    ema50 = ind.ema(closes, 50)
    if ema20 and ema50:
        extreme = min(c.low for c in candles[-3:]) if bull else max(c.high for c in candles[-3:])
        for name, series in (("EMA20", ema20), ("EMA50", ema50)):
            level_v = series[-1]
            if abs(extreme - level_v) <= 0.6 * atr and reprise:
                confirm("ema_dynamic", 0.7,
                        f"rebond sur la {name} ({level_v:.6g}), support dynamique de la tendance")
                break

    # --- Volume : essoufflement pendant la correction, reprise à la relance ---------------------
    rel_vol = ind.relative_volume(candles, 20)
    if rel_vol is not None:
        # Volume moyen des trois bougies qui ont précédé la reprise = celles de la correction.
        prior = ind.relative_volume(candles[:-1], 20)
        if prior is not None and prior < 0.9 and rel_vol >= 1.2:
            confirm("volume", 1.0,
                    f"volume en berne pendant la correction ({prior:.2f}×) puis en hausse à la "
                    f"reprise ({rel_vol:.2f}×) — la signature d'une vraie relance")
        elif rel_vol >= 1.2:
            confirm("volume", 0.6, f"volume supérieur à la normale ({rel_vol:.2f}×)")

    # --- Règle du minimum de confirmations ------------------------------------------------------
    score = sum(c["contribution"] for c in confirmations)
    strong = sum(1 for c in confirmations if c["strong"])
    out["confirmations"] = confirmations
    out["score"] = round(score, 3)

    if not confirmations:
        out["reason"] = "aucune confirmation d'entrée sur le 15 min"
        return out
    detail = ", ".join(f"{c['key']} {c['contribution']:.2f}" for c in confirmations)
    if len(confirmations) < MIN_CONFIRMATIONS:
        out["reason"] = (f"seulement {len(confirmations)} confirmation(s) sur "
                         f"{MIN_CONFIRMATIONS} exigées ({detail})")
        return out
    if strong < MIN_STRONG_CONFIRMATIONS:
        out["reason"] = ("aucune confirmation forte : que des indicateurs dérivés du prix "
                         f"({detail})")
        return out
    if score < min_score:
        out["reason"] = (f"confluence {score:.2f} sous le seuil de {min_score:.2f} ({detail})")
        return out

    out["fired"] = True
    out["reason"] = (f"{len(confirmations)} confirmations pondérées = {score:.2f}/{min_score:.2f} "
                     f"({detail})")
    return out
