"""Figures de chandeliers et de structure — la lecture « price action » du graphique.

Ce module vit dans le DOMAINE et non dans l'agent chartiste, parce que deux consommateurs en ont
besoin sans devoir se connaître : l'agent `pattern` (qui vote dans l'arbitrage du Master) et la
confluence d'entrée de la stratégie (qui exige au moins une figure de retournement avant de laisser
passer un ordre). Une seule définition d'« avalée haussière » pour tout le projet.

Le biais retourné avec chaque figure va de -1 (baissier franc) à +1 (haussier franc) ; 0 signifie
que la figure informe sans trancher — une inside bar annonce une cassure, pas son sens.
"""

from __future__ import annotations

from app.domain.indicators import Candle

# Figures acceptées comme CONFIRMATION D'ENTRÉE par la stratégie. La liste est volontairement plus
# courte que tout ce que `detect_patterns` sait voir : un doji ou une « structure haussière » sur
# trois bougies ne suffit pas à engager du capital, alors qu'une avalée ou une étoile du matin
# marquent un vrai basculement du rapport de force.
ENTRY_PATTERNS = {
    "avalée haussière", "avalée baissière",
    "marteau", "étoile filante",                   # les deux formes de pin bar
    "inside bar (compression)",
    "étoile du matin", "étoile du soir",
}


def _body(c: Candle) -> float:
    return abs(c.close - c.open)


def _range(c: Candle) -> float:
    return max(c.high - c.low, 1e-9)


def _bull(c: Candle) -> bool:
    return c.close > c.open


def _bear(c: Candle) -> bool:
    return c.close < c.open


def detect_patterns(candles: list[Candle]) -> list[tuple[str, float]]:
    """Détection déterministe des figures qu'un trader PRO surveille (chandeliers + structure).

    Retourne une liste de (figure, biais[-1..1]) détectées sur les dernières bougies."""
    if len(candles) < 3:
        return []
    out: list[tuple[str, float]] = []
    c3, prev, last = candles[-3], candles[-2], candles[-1]
    body = _body(last)
    upper = last.high - max(last.close, last.open)
    lower = min(last.close, last.open) - last.low

    # --- Retournements 2 bougies ---
    # Avalée (engulfing)
    if _bull(last) and _bear(prev) and last.close >= prev.open and last.open <= prev.close:
        out.append(("avalée haussière", 0.6))
    if _bear(last) and _bull(prev) and last.open >= prev.close and last.close <= prev.open:
        out.append(("avalée baissière", -0.6))
    # Piercing line / Dark cloud cover (pénétration >50% du corps précédent)
    mid_prev = (prev.open + prev.close) / 2
    if _bear(prev) and _bull(last) and last.open < prev.close and last.close > mid_prev and last.close < prev.open:
        out.append(("piercing line (retournement haussier)", 0.5))
    if _bull(prev) and _bear(last) and last.open > prev.close and last.close < mid_prev and last.close > prev.open:
        out.append(("dark cloud cover (retournement baissier)", -0.5))
    # Pinces (tweezers) : mêmes extrêmes = rejet du niveau
    rng = _range(last)
    if abs(last.low - prev.low) < 0.1 * rng and _bear(prev) and _bull(last):
        out.append(("pince basse (tweezer bottom)", 0.4))
    if abs(last.high - prev.high) < 0.1 * rng and _bull(prev) and _bear(last):
        out.append(("pince haute (tweezer top)", -0.4))
    # Inside bar (compression -> cassure à surveiller)
    if last.high < prev.high and last.low > prev.low:
        out.append(("inside bar (compression)", 0.0))

    # --- Retournements 3 bougies ---
    # Étoile du matin / du soir
    small_mid = _body(prev) < 0.4 * _body(c3) if _body(c3) > 0 else False
    if _bear(c3) and small_mid and _bull(last) and last.close > (c3.open + c3.close) / 2:
        out.append(("étoile du matin", 0.7))
    if _bull(c3) and small_mid and _bear(last) and last.close < (c3.open + c3.close) / 2:
        out.append(("étoile du soir", -0.7))
    # Trois soldats blancs / trois corbeaux noirs (momentum fort)
    if all(_bull(c) for c in (c3, prev, last)) and c3.close < prev.close < last.close and \
       all(_body(c) > 0.5 * _range(c) for c in (c3, prev, last)):
        out.append(("trois soldats blancs", 0.6))
    if all(_bear(c) for c in (c3, prev, last)) and c3.close > prev.close > last.close and \
       all(_body(c) > 0.5 * _range(c) for c in (c3, prev, last)):
        out.append(("trois corbeaux noirs", -0.6))

    # --- Bougie isolée ---
    if lower > 2 * body and upper < body:
        out.append(("marteau", 0.4))
    if upper > 2 * body and lower < body:
        out.append(("étoile filante", -0.4))
    if body < 0.1 * _range(last):
        out.append(("doji", 0.0))

    # --- Structure ---
    highs = [c.high for c in candles[-3:]]
    lows = [c.low for c in candles[-3:]]
    if highs[0] < highs[1] < highs[2] and lows[0] < lows[1] < lows[2]:
        out.append(("structure haussière", 0.3))
    if highs[0] > highs[1] > highs[2] and lows[0] > lows[1] > lows[2]:
        out.append(("structure baissière", -0.3))
    # Double sommet / double creux (deux extrêmes similaires séparés, prix qui rejette)
    if len(candles) >= 30:
        w = candles[-30:]
        hi = max(c.high for c in w)
        lo = min(c.low for c in w)
        span = max(hi - lo, 1e-9)
        peaks = [i for i, c in enumerate(w) if c.high > hi - 0.02 * span]
        troughs = [i for i, c in enumerate(w) if c.low < lo + 0.02 * span]
        if len(peaks) >= 2 and (peaks[-1] - peaks[0]) >= 5 and last.close < hi - 0.03 * span:
            out.append(("double sommet (résistance confirmée)", -0.5))
        if len(troughs) >= 2 and (troughs[-1] - troughs[0]) >= 5 and last.close > lo + 0.03 * span:
            out.append(("double creux (support confirmé)", 0.5))
    return out


def entry_pattern(candles: list[Candle], direction: int) -> tuple[str, float] | None:
    """Meilleure figure de `ENTRY_PATTERNS` allant dans le sens de `direction`.

    Une figure compte comme confirmation si son biais pousse dans notre sens ; l'inside bar, dont
    le biais est nul, est acceptée parce qu'elle signale une compression sur le point de casser —
    utile quand elle se forme sur une zone que la tendance vient défendre.

    La QUALITÉ retournée est la valeur absolue du biais (0,7 pour une étoile du matin, 0,6 pour une
    avalée…), sauf pour l'inside bar à qui on accorde 0,3 : elle informe, sans prouver le sens.
    Retourne la figure la plus forte, ou ``None``.
    """
    if direction == 0:
        return None
    found: list[tuple[str, float]] = []
    for name, bias in detect_patterns(candles):
        if name not in ENTRY_PATTERNS:
            continue
        if bias == 0.0:
            found.append((name, 0.3))          # inside bar : compression, sens encore ouvert
        elif bias * direction > 0:
            found.append((name, abs(bias)))
    if not found:
        return None
    return max(found, key=lambda f: f[1])
