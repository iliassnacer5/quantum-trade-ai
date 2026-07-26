"""Analyse technique experte consolidée — calcule TOUTES les métriques + un score directionnel.

Sortie structurée (`metrics`) exposée dans la Signal Card pour un affichage type "tableau de bord
trader" (RSI, MACD, ADX, Stochastique, EMA, Bollinger, ATR, OBV, VWAP, supports/résistances).

Le score combine plusieurs indicateurs confirmants : plus ils s'alignent, plus le score est franc
(|score| proche de 1), ce qui se traduit ensuite par une confiance élevée dans le Master.
"""

from __future__ import annotations

from app.domain import indicators as ind
from app.domain.indicators import Candle


def _state_rsi(v: float) -> str:
    return "survente" if v < 30 else "surachat" if v > 70 else "neutre"


# Seuil de score directionnel pour la « haute conviction », adapté à la classe d'actif :
# le forex et les actions bougent en % plus faibles que la crypto, donc un seuil plus bas.
HIGH_CONV_THRESHOLD = {"crypto": 0.30, "forex": 0.22, "stock": 0.25}


def is_high_conviction(score: float, adx: float | None, asset_class: str | None) -> bool:
    """Vrai si tendance forte (ADX>25) ET score directionnel au-dessus du seuil de la classe."""
    threshold = HIGH_CONV_THRESHOLD.get(asset_class or "crypto", 0.30)
    return abs(score) > threshold and (adx or 0) > 25


def fear_greed_proxy(candles: list[Candle]) -> int:
    """Indice Fear & Greed (0-100) dérivé du marché quand aucune source de news n'est disponible.

    Combine momentum (variation 14 périodes), RSI et position dans la fourchette récente — la même
    logique que les indices F&G crypto. 50 = neutre, >50 = avidité (haussier), <50 = peur.
    """
    closes = [c.close for c in candles]
    if len(closes) < 20:
        return 50
    momentum = (closes[-1] / closes[-14] - 1.0) if closes[-14] else 0.0  # ±
    rsi_val = ind.rsi(closes, 14) or 50.0
    support, resistance = ind.support_resistance(candles, 50)
    span = resistance - support
    range_pos = ((closes[-1] - support) / span) if span else 0.5  # 0..1
    raw = 50 + momentum * 400 + (rsi_val - 50) * 0.6 + (range_pos - 0.5) * 30
    return int(max(0, min(100, round(raw))))


def analyze(candles: list[Candle]) -> dict:
    """Retourne {metrics, score[-1..1], confidence[0..1], notes[list], signals_count}."""
    closes = [c.close for c in candles]
    price = closes[-1]
    metrics: dict = {"price": round(price, 8)}
    signals: list[float] = []
    notes: list[str] = []

    # --- Tendance (EMA 20/50/200) ---
    ema20 = ind.ema(closes, 20)[-1]
    ema50 = ind.ema(closes, 50)[-1] if len(closes) >= 50 else ema20
    ema200 = ind.ema(closes, 200)[-1] if len(closes) >= 200 else ema50
    if ema20 > ema50 > ema200:
        trend, ts = "haussière", 0.5
    elif ema20 < ema50 < ema200:
        trend, ts = "baissière", -0.5
    elif ema20 > ema50:
        trend, ts = "haussière (court terme)", 0.3
    elif ema20 < ema50:
        trend, ts = "baissière (court terme)", -0.3
    else:
        trend, ts = "neutre", 0.0
    signals.append(ts)
    notes.append(f"Tendance {trend} (EMA20 {ema20:.2f} / EMA50 {ema50:.2f})")
    metrics |= {"ema20": round(ema20, 4), "ema50": round(ema50, 4), "ema200": round(ema200, 4), "trend": trend}

    # --- RSI ---
    rsi_val = ind.rsi(closes, 14)
    if rsi_val is not None:
        state = _state_rsi(rsi_val)
        if state == "survente":
            signals.append(0.5)
        elif state == "surachat":
            signals.append(-0.5)
        else:
            signals.append((50 - rsi_val) / 100)
        notes.append(f"RSI {rsi_val:.0f} ({state})")
        metrics["rsi"] = round(rsi_val, 1)
        metrics["rsi_state"] = state

    # --- MACD ---
    macd_res = ind.macd(closes)
    if macd_res is not None:
        line, sig, hist = macd_res
        signals.append(0.4 if hist > 0 else -0.4)
        notes.append(f"MACD {'haussier' if hist > 0 else 'baissier'} (hist {hist:+.2f})")
        metrics["macd"] = {"line": round(line, 4), "signal": round(sig, 4), "hist": round(hist, 4),
                           "state": "haussier" if hist > 0 else "baissier"}

    # --- ADX (force de tendance) ---
    adx_val = ind.adx(candles, 14)
    if adx_val is not None:
        adx_state = "tendance forte" if adx_val > 25 else "tendance modérée" if adx_val > 20 else "range (pas de tendance)"
        notes.append(f"ADX {adx_val:.0f} ({adx_state})")
        metrics["adx"] = round(adx_val, 1)
        metrics["adx_state"] = adx_state
        # L'ADX n'a pas de direction propre : il amplifie le signal de tendance existant.
        if adx_val > 25 and ts != 0:
            signals.append(0.3 if ts > 0 else -0.3)

    # --- Stochastique ---
    stoch = ind.stochastic(candles)
    if stoch is not None:
        k, d = stoch
        if k < 20:
            signals.append(0.4)
            st = "survente"
        elif k > 80:
            signals.append(-0.4)
            st = "surachat"
        else:
            st = "neutre"
        notes.append(f"Stochastique %K {k:.0f} ({st})")
        metrics["stochastic"] = {"k": round(k, 1), "d": round(d, 1), "state": st}

    # --- Bollinger ---
    boll = ind.bollinger(closes, 20)
    if boll is not None:
        low, mid, high = boll
        if price <= low:
            signals.append(0.3)
            pos = "bande basse (survente)"
        elif price >= high:
            signals.append(-0.3)
            pos = "bande haute (surachat)"
        else:
            pos = "milieu de bande"
        notes.append(f"Bollinger : {pos}")
        metrics["bollinger"] = {"low": round(low, 4), "mid": round(mid, 4), "high": round(high, 4), "position": pos}

    # --- Volume : OBV / VWAP ---
    obv_line = ind.obv(candles)
    if len(obv_line) >= 10:
        obv_up = obv_line[-1] > obv_line[-10]
        signals.append(0.25 if obv_up else -0.25)
        notes.append(f"OBV en {'hausse (accumulation)' if obv_up else 'baisse (distribution)'}")
        metrics["obv_trend"] = "hausse" if obv_up else "baisse"
    vwap_val = ind.vwap(candles)
    if vwap_val is not None:
        signals.append(0.2 if price > vwap_val else -0.2)
        notes.append(f"Prix {'au-dessus' if price > vwap_val else 'en-dessous'} du VWAP ({vwap_val:.2f})")
        metrics["vwap"] = round(vwap_val, 4)
        metrics["vs_vwap"] = "au-dessus" if price > vwap_val else "en-dessous"

    # --- ATR / volatilité ---
    atr_val = ind.atr(candles, 14)
    if atr_val is not None:
        metrics["atr"] = round(atr_val, 4)
        metrics["atr_pct"] = round(atr_val / price * 100, 2) if price else 0.0

    # --- Supports / résistances ---
    support, resistance = ind.support_resistance(candles, 50)
    metrics["support"] = round(support, 4)
    metrics["resistance"] = round(resistance, 4)
    span = resistance - support
    if span > 0:
        pos_pct = (price - support) / span * 100
        metrics["range_position_pct"] = round(pos_pct, 1)
        # Proche du support => biais achat ; proche résistance => biais vente.
        if pos_pct < 20:
            signals.append(0.2)
            notes.append("Prix proche du support (rebond possible)")
        elif pos_pct > 80:
            signals.append(-0.2)
            notes.append("Prix proche de la résistance (rejet possible)")

    # --- Points pivots (référence pro : niveaux surveillés par tout le marché) ---
    piv = ind.pivot_points(candles)
    if piv:
        metrics["pivots"] = piv
        if price > piv["r1"]:
            signals.append(0.2)
            notes.append(f"Prix au-dessus du pivot R1 ({piv['r1']:.4g}) : momentum haussier")
        elif price < piv["s1"]:
            signals.append(-0.2)
            notes.append(f"Prix sous le pivot S1 ({piv['s1']:.4g}) : momentum baissier")
        elif abs(price - piv["p"]) / price < 0.002:
            notes.append(f"Prix sur le pivot central ({piv['p']:.4g}) : zone de décision")

    # --- Retracements de Fibonacci (zones de rebond institutionnelles) ---
    fib = ind.fibonacci_levels(candles)
    if fib:
        metrics["fibonacci"] = fib
        lv = fib["levels"]
        # Prix dans la zone d'or (entre 50% et 61.8%) d'un swing = zone de reprise classique.
        golden_lo, golden_hi = sorted([lv["50"], lv["61.8"]])
        if golden_lo <= price <= golden_hi:
            bias = 0.25 if fib["swing"] == "haussier" else -0.25
            signals.append(bias)
            notes.append(f"Prix dans la zone d'or Fibonacci (50-61.8%) du swing {fib['swing']}")

    # --- FACTEURS DE LA STRATÉGIE DU DESK (playbook) ---------------------------------------
    # MA20 / MA50 SIMPLES, divergences RSI & MACD, tendance VWAP, volume relatif : la stratégie
    # les exige à chaque unité de temps. Comme tous les agents experts passent par `ta.analyze`,
    # ils héritent automatiquement de ces facteurs.
    ma20 = ind.sma(closes, 20)
    ma50 = ind.sma(closes, 50)
    if ma20 is not None:
        metrics["ma20"] = round(ma20, 6)
    if ma20 is not None and ma50 is not None:
        metrics["ma50"] = round(ma50, 6)
        if price > ma20 > ma50:
            signals.append(0.35)
            notes.append(f"Prix > MA20 ({ma20:.4g}) > MA50 ({ma50:.4g}) : empilement haussier")
        elif price < ma20 < ma50:
            signals.append(-0.35)
            notes.append(f"Prix < MA20 ({ma20:.4g}) < MA50 ({ma50:.4g}) : empilement baissier")
        else:
            notes.append(f"MA20 ({ma20:.4g}) et MA50 ({ma50:.4g}) entremêlées : pas d'empilement net")

    rsi_series = ind.rsi_series(closes, 14)
    macd_all = ind.macd_series(closes)
    rsi_div = ind.divergence(candles, rsi_series) if rsi_series else None
    macd_div = ind.divergence(candles, macd_all[2]) if macd_all else None
    metrics["rsi_divergence"] = rsi_div
    metrics["macd_divergence"] = macd_div
    for label, div in (("RSI", rsi_div), ("MACD", macd_div)):
        if div:
            signals.append(0.3 if div == "haussière" else -0.3)
            notes.append(f"Divergence {div} {label} (essoufflement du mouvement en cours)")

    vwap_slope = ind.slope_pct(ind.rolling_vwap(candles, 20), 5)
    if vwap_slope is not None:
        metrics["vwap_slope_pct"] = round(vwap_slope, 3)
        if vwap_slope > 0.01:
            signals.append(0.2)
            notes.append("Tendance VWAP haussière")
        elif vwap_slope < -0.01:
            signals.append(-0.2)
            notes.append("Tendance VWAP baissière")
        else:
            notes.append("Tendance VWAP plate")

    rel_vol = ind.relative_volume(candles, 20)
    if rel_vol is not None:
        metrics["relative_volume"] = round(rel_vol, 2)
        notes.append(
            f"Volume {rel_vol:.1f}× la moyenne 20 "
            f"({'participation soutenue' if rel_vol >= 1.2 else 'participation faible' if rel_vol < 0.8 else 'normale'})"
        )

    # --- Position vs plus haut / plus bas 52 périodes (contexte long terme) ---
    if len(candles) >= 52:
        w52 = candles[-52:]
        hi52, lo52 = max(c.high for c in w52), min(c.low for c in w52)
        if hi52 > lo52:
            pos52 = (price - lo52) / (hi52 - lo52) * 100
            metrics["pos_52"] = round(pos52, 1)
            if pos52 > 95:
                notes.append("Prix au plus haut 52 périodes (force, mais étendu)")
            elif pos52 < 5:
                notes.append("Prix au plus bas 52 périodes (faiblesse, mais possible capitulation)")

    # --- Agrégation ---
    n = max(len(signals), 1)
    raw = sum(signals) / n
    # Score plus franc quand les indicateurs s'alignent (moins de dilution par la moyenne).
    aligned = sum(1 for s in signals if (s > 0) == (raw > 0) and s != 0)
    agreement = aligned / n
    score = max(-1.0, min(1.0, raw * (0.6 + 0.8 * agreement)))

    # --- Amplification de tendance (ADX) ---
    # L'ADX mesure la FORCE de la tendance : quand elle est forte (>25) et confirmée par l'EMA, on
    # tire le score vers la direction de la tendance (principe : en tendance forte, on suit la
    # tendance et on ignore les oscillateurs de retour à la moyenne type RSI neutre). Cela évite que
    # des marchés en tendance claire (ex. EUR/USD ADX 39 baissier) restent à faible conviction.
    adx_val = metrics.get("adx")
    if adx_val and adx_val > 25 and ts != 0:
        trend_dir = 1.0 if ts > 0 else -1.0
        # On n'amplifie que si le score n'est pas franchement contraire à la tendance.
        if score * trend_dir >= -0.1:
            boost = min(0.4, (adx_val - 25) / 45.0)
            score = max(-1.0, min(1.0, (1 - boost) * score + boost * trend_dir))

    # Confiance plus haute quand la tendance est forte et confirmée.
    confidence = min(1.0, 0.45 + 0.08 * len(signals) + (0.1 if (adx_val or 0) > 25 else 0.0))
    return {
        "metrics": metrics,
        "score": round(score, 3),
        "confidence": round(confidence, 3),
        "notes": notes,
        "signals_count": len(signals),
        "agreement": round(agreement, 2),
    }
