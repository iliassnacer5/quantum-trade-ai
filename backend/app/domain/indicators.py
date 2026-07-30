"""Indicateurs techniques déterministes (pur Python, sans dépendance native).

Implémente RSI, EMA, MACD, Bandes de Bollinger, ATR.
Utilisés par l'Agent Technique (M2) — calculs reproductibles, pas de LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Candle:
    """Bougie OHLCV. `timestamp` optionnel (renseigné pour le backtest, None en live/synthétique)."""

    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: datetime | None = None


def ema(values: list[float], period: int) -> list[float]:
    """Moyenne mobile exponentielle. Retourne une liste de même longueur (None implicite=premières valeurs lissées)."""
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(values: list[float], period: int) -> float | None:
    """Moyenne mobile simple sur les `period` dernières valeurs."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def sma_series(values: list[float], period: int) -> list[float | None]:
    """Série complète de la moyenne mobile simple (None avant amorçage).

    Utilisée par le playbook : la stratégie raisonne sur la PENTE des MA20/MA50, pas seulement
    sur leur dernière valeur.
    """
    if len(values) < period:
        return []
    out: list[float | None] = [None] * (period - 1)
    running = sum(values[:period])
    out.append(running / period)
    for i in range(period, len(values)):
        running += values[i] - values[i - period]
        out.append(running / period)
    return out


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Relative Strength Index (0-100). Méthode de Wilder."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    # Moyenne de Wilder
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    """RSI de Wilder sur TOUTE la série (aligné sur `closes`, None avant amorçage).

    Nécessaire pour détecter les DIVERGENCES prix/RSI : il faut la valeur du RSI au moment de
    chaque sommet/creux, pas seulement la dernière.
    """
    if len(closes) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    def _val(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        return 100 - (100 / (1 + ag / al))

    out: list[float | None] = [None] * len(closes)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = _val(avg_gain, avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _val(avg_gain, avg_loss)
    return out


def macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[float, float, float] | None:
    """MACD -> (ligne MACD, ligne signal, histogramme). Renvoie None si pas assez de données."""
    if len(closes) < slow + signal:
        return None
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    hist = macd_line[-1] - signal_line[-1]
    return macd_line[-1], signal_line[-1], hist


def macd_series(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float], list[float], list[float]] | None:
    """MACD sur toute la série -> (ligne, signal, histogramme). Base de la divergence MACD."""
    if len(closes) < slow + signal:
        return None
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    line = [f - s for f, s in zip(ema_fast, ema_slow, strict=True)]
    sig = ema(line, signal)
    hist = [ln - sg for ln, sg in zip(line, sig, strict=True)]
    return line, sig, hist


def bollinger(closes: list[float], period: int = 20, mult: float = 2.0) -> tuple[float, float, float] | None:
    """Bandes de Bollinger -> (bande basse, milieu, bande haute)."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((c - mid) ** 2 for c in window) / period
    std = variance**0.5
    return mid - mult * std, mid, mid + mult * std


def atr(candles: list[Candle], period: int = 14) -> float | None:
    """Average True Range — mesure de volatilité (base du dimensionnement SL/TP)."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        c = candles[i]
        prev_close = candles[i - 1].close
        tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        trs.append(tr)
    # ATR de Wilder
    atr_val = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr_val = (atr_val * (period - 1) + trs[i]) / period
    return atr_val


def stochastic(candles: list[Candle], period: int = 14, smooth: int = 3) -> tuple[float, float] | None:
    """Oscillateur stochastique -> (%K, %D). 0-100. >80 surachat, <20 survente."""
    if len(candles) < period + smooth:
        return None
    ks: list[float] = []
    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1 : i + 1]
        hh = max(c.high for c in window)
        ll = min(c.low for c in window)
        k = 100.0 if hh == ll else (candles[i].close - ll) / (hh - ll) * 100.0
        ks.append(k)
    if len(ks) < smooth:
        return None
    d = sum(ks[-smooth:]) / smooth
    return ks[-1], d


def adx(candles: list[Candle], period: int = 14) -> float | None:
    """Average Directional Index — force de la tendance (0-100). >25 = tendance, <20 = range."""
    res = adx_di(candles, period)
    return res[0] if res else None


def adx_di(candles: list[Candle], period: int = 14) -> tuple[float, float, float] | None:
    """ADX **et** les deux directionnels : ``(adx, +DI, -DI)``.

    L'ADX seul dit qu'il y a une tendance, pas dans quel SENS. Les directionnels tranchent :
    +DI > -DI signale une pression acheteuse dominante. La détection de tendance en a besoin pour
    vérifier que la force mesurée pousse bien dans le sens du biais, et pas contre lui.
    """
    if len(candles) < 2 * period:
        return None
    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, len(candles)):
        up = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        tr = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close),
        )
        trs.append(tr)

    def _wilder(values: list[float]) -> list[float]:
        out = [sum(values[:period])]
        for i in range(period, len(values)):
            out.append(out[-1] - out[-1] / period + values[i])
        return out

    atr_s = _wilder(trs)
    pdm_s = _wilder(plus_dm)
    mdm_s = _wilder(minus_dm)
    dxs: list[float] = []
    last_pdi = last_mdi = 0.0
    for atr_v, pdm_v, mdm_v in zip(atr_s, pdm_s, mdm_s):
        if atr_v == 0:
            continue
        pdi = 100 * pdm_v / atr_v
        mdi = 100 * mdm_v / atr_v
        last_pdi, last_mdi = pdi, mdi
        denom = pdi + mdi
        dxs.append(100 * abs(pdi - mdi) / denom if denom else 0.0)
    if not dxs:
        return None
    value = sum(dxs) / len(dxs) if len(dxs) < period else sum(dxs[-period:]) / period
    return value, last_pdi, last_mdi


def support_resistance(candles: list[Candle], lookback: int = 50) -> tuple[float, float]:
    """Support (plus bas) et résistance (plus haut) sur la fenêtre récente."""
    window = candles[-lookback:] if len(candles) >= lookback else candles
    return min(c.low for c in window), max(c.high for c in window)


def pivot_points(candles: list[Candle]) -> dict | None:
    """Points pivots CLASSIQUES (référence pro universelle) sur la période précédente (~24 bougies).

    P = (H+L+C)/3 ; R1 = 2P−L ; S1 = 2P−H ; R2 = P+(H−L) ; S2 = P−(H−L)."""
    if len(candles) < 26:
        return None
    prev = candles[-25:-1]
    h = max(c.high for c in prev)
    low = min(c.low for c in prev)
    cl = prev[-1].close
    p = (h + low + cl) / 3
    return {
        "p": round(p, 6), "r1": round(2 * p - low, 6), "s1": round(2 * p - h, 6),
        "r2": round(p + (h - low), 6), "s2": round(p - (h - low), 6),
    }


def fibonacci_levels(candles: list[Candle], lookback: int = 100) -> dict | None:
    """Retracements de Fibonacci (38.2 / 50 / 61.8 %) du dernier swing visible.

    Sens du swing : position relative du plus haut vs plus bas dans la fenêtre."""
    if len(candles) < lookback // 2:
        return None
    window = candles[-lookback:]
    hi = max(c.high for c in window)
    lo = min(c.low for c in window)
    if hi <= lo:
        return None
    hi_idx = max(range(len(window)), key=lambda i: window[i].high)
    lo_idx = min(range(len(window)), key=lambda i: window[i].low)
    span = hi - lo
    up = lo_idx < hi_idx  # bas puis haut = swing haussier
    if up:
        levels = {"38.2": hi - 0.382 * span, "50": hi - 0.5 * span, "61.8": hi - 0.618 * span}
    else:
        levels = {"38.2": lo + 0.382 * span, "50": lo + 0.5 * span, "61.8": lo + 0.618 * span}
    return {"swing": "haussier" if up else "baissier", "high": round(hi, 6), "low": round(lo, 6),
            "levels": {k: round(v, 6) for k, v in levels.items()}}


def fibonacci_extension(candles: list[Candle], lookback: int = 100) -> dict | None:
    """Extensions de Fibonacci (127,2 % et 161,8 %) — des OBJECTIFS, pas des retracements.

    Là où `fibonacci_levels` mesure la profondeur d'une correction (où le prix pourrait repartir),
    l'extension projette le mouvement AU-DELÀ de l'extrême du swing : c'est là que le marché a
    l'habitude d'aller chercher ses prises de bénéfices quand la tendance reprend.

    Même swing de référence que `fibonacci_levels`, pour que retracement et objectif racontent la
    même histoire. Retourne ``{"swing", "high", "low", "levels": {"1.272", "1.618"}}``.
    """
    base = fibonacci_levels(candles, lookback)
    if not base:
        return None
    hi, lo = base["high"], base["low"]
    span = hi - lo
    if base["swing"] == "haussier":
        levels = {"1.272": hi + 0.272 * span, "1.618": hi + 0.618 * span}
    else:
        levels = {"1.272": lo - 0.272 * span, "1.618": lo - 0.618 * span}
    return {"swing": base["swing"], "high": hi, "low": lo,
            "levels": {k: round(v, 6) for k, v in levels.items()}}


def supertrend(candles: list[Candle], period: int = 10, multiplier: float = 3.0) -> dict | None:
    """SuperTrend — un suiveur de tendance qui ne change d'avis que lorsque le prix le franchit.

    Deux bandes sont posées à `multiplier` × ATR de part et d'autre du prix médian. Elles sont
    VERROUILLÉES : tant que la tendance dure, la bande ne peut que se resserrer vers le prix, jamais
    s'en éloigner. C'est ce verrou qui évite les allers-retours d'un simple canal ATR — la bascule
    ne se produit qu'à une vraie cassure.

    Retourne ``{"direction": +1|-1, "level", "flipped_ago", "series"}`` où `flipped_ago` est le
    nombre de bougies depuis la dernière bascule (0 = elle vient de se produire, donc fragile).
    ``None`` si l'historique ne suffit pas — jamais de direction inventée.
    """
    if len(candles) < period + 1:
        return None
    atrs = _atr_series(candles, period)
    if not atrs:
        return None
    directions: list[int] = []
    final_upper = final_lower = 0.0
    direction = 1
    level = candles[0].close
    for i in range(period, len(candles)):
        c = candles[i]
        atr_v = atrs[i]
        if atr_v is None:
            continue
        mid = (c.high + c.low) / 2
        basic_upper = mid + multiplier * atr_v
        basic_lower = mid - multiplier * atr_v
        prev_close = candles[i - 1].close
        # Verrou des bandes : elles ne s'écartent du prix que si la précédente a été franchie.
        final_upper = (basic_upper if not directions or basic_upper < final_upper
                       or prev_close > final_upper else final_upper)
        final_lower = (basic_lower if not directions or basic_lower > final_lower
                       or prev_close < final_lower else final_lower)
        if not directions:
            direction = 1 if c.close >= final_upper else -1
        elif direction > 0 and c.close < final_lower:
            direction = -1
        elif direction < 0 and c.close > final_upper:
            direction = 1
        level = final_lower if direction > 0 else final_upper
        directions.append(direction)
    if not directions:
        return None
    flipped_ago = 0
    for d in reversed(directions[:-1]):
        if d != directions[-1]:
            break
        flipped_ago += 1
    return {"direction": directions[-1], "level": round(level, 8),
            "flipped_ago": flipped_ago, "series": directions}


def _atr_series(candles: list[Candle], period: int = 14) -> list[float | None]:
    """ATR de Wilder sous forme de SÉRIE, alignée sur `candles` (None avant amorçage).

    `atr()` ne rend que la dernière valeur ; le SuperTrend, lui, doit rejouer toute l'histoire.
    """
    if len(candles) < period + 1:
        return []
    out: list[float | None] = [None] * len(candles)
    trs = []
    for i in range(1, len(candles)):
        trs.append(max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close),
        ))
    running = sum(trs[:period]) / period
    out[period] = running
    for i in range(period, len(trs)):
        running = (running * (period - 1) + trs[i]) / period
        out[i + 1] = running
    return out


def obv(candles: list[Candle]) -> list[float]:
    """On-Balance Volume (OBV)."""
    if not candles:
        return []
    out = [candles[0].volume]
    for i in range(1, len(candles)):
        c = candles[i]
        prev_close = candles[i - 1].close
        if c.close > prev_close:
            out.append(out[-1] + c.volume)
        elif c.close < prev_close:
            out.append(out[-1] - c.volume)
        else:
            out.append(out[-1])
    return out


def vwap(candles: list[Candle]) -> float | None:
    """Volume Weighted Average Price (VWAP) sur la période donnée."""
    if not candles:
        return None
    cum_vol = 0.0
    cum_pv = 0.0
    for c in candles:
        typ_price = (c.high + c.low + c.close) / 3
        cum_pv += typ_price * c.volume
        cum_vol += c.volume
    if cum_vol == 0:
        return None
    return cum_pv / cum_vol


def rolling_vwap(candles: list[Candle], period: int = 20) -> list[float | None]:
    """VWAP glissant sur `period` bougies (série complète) -> permet d'en lire la TENDANCE.

    Le VWAP « ancré » classique (cf. `vwap`) donne un niveau ; la stratégie demande la *tendance*
    VWAP, donc il faut la série.

    Calculé en SOMME GLISSANTE (une seule passe) et non en re-sommant la fenêtre à chaque bougie.
    L'ancienne version était quadratique : profilée le 30/07/2026 sur un backtest de 500 bougies,
    elle représentait à elle seule 1,4 s de processeur, 237 000 appels à `sum()` et 1,9 million
    d'évaluations de générateur — pour un résultat identique. Cet indicateur est sur le chemin de
    `ta.analyze`, donc de TOUTE analyse (scanner, sélection du jour, backtests, playbook) : le gain
    profite à toutes les pages, pas seulement à celle qui a révélé le problème.
    """
    out: list[float | None] = []
    cum_pv = cum_vol = 0.0
    for i, c in enumerate(candles):
        cum_pv += ((c.high + c.low + c.close) / 3) * c.volume
        cum_vol += c.volume
        # Sortie de fenêtre : on retire la bougie qui vient de quitter les `period` dernières.
        if i >= period:
            old = candles[i - period]
            cum_pv -= ((old.high + old.low + old.close) / 3) * old.volume
            cum_vol -= old.volume
        # `cum_vol` peut devenir infinitésimal (et non exactement 0) par accumulation d'erreurs de
        # virgule flottante sur des volumes nuls : on teste donc un seuil, pas l'égalité stricte.
        out.append(cum_pv / cum_vol if cum_vol > 1e-12 else None)
    return out


def slope_pct(series: list[float | None], lookback: int = 5) -> float | None:
    """Pente relative d'une série sur `lookback` points, en % de la valeur de départ.

    Sert à qualifier « MA20 montante », « VWAP orienté à la hausse »… sans dépendre de l'échelle
    du symbole (EUR/USD à 1,08 et BTC à 60 000 se comparent).
    """
    vals = [v for v in series if v is not None]
    if len(vals) < lookback + 1:
        return None
    start, end = vals[-lookback - 1], vals[-1]
    if not start:
        return None
    return (end - start) / abs(start) * 100


def swing_points(candles: list[Candle], left: int = 2, right: int = 2) -> dict:
    """Sommets et creux de swing (pivots fractals) -> structure de marché et niveaux majeurs.

    Un sommet est confirmé si son `high` domine les `left` bougies précédentes et les `right`
    suivantes (idem, inversé, pour les creux). Retourne {"highs": [(index, prix)], "lows": [...]}.
    """
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for i in range(left, len(candles) - right):
        window = candles[i - left : i + right + 1]
        if candles[i].high >= max(c.high for c in window):
            highs.append((i, candles[i].high))
        if candles[i].low <= min(c.low for c in window):
            lows.append((i, candles[i].low))
    return {"highs": highs, "lows": lows}


def divergence(
    candles: list[Candle], oscillator: list[float | None], lookback: int = 60,
    left: int = 2, right: int = 2,
) -> str | None:
    """Divergence RÉGULIÈRE entre le prix et un oscillateur (RSI ou histogramme MACD).

    - haussière : le prix fait un creux plus BAS, l'oscillateur un creux plus HAUT (vendeurs à bout).
    - baissière : le prix fait un sommet plus HAUT, l'oscillateur un sommet plus BAS (acheteurs à bout).
    Retourne la divergence la plus RÉCENTE, ou None.
    """
    if not oscillator or len(candles) < left + right + 10:
        return None
    start = max(0, len(candles) - lookback)
    sw = swing_points(candles[start:], left, right)

    def _osc(idx: int) -> float | None:
        real = idx + start
        return oscillator[real] if 0 <= real < len(oscillator) else None

    found: list[tuple[int, str]] = []
    lows = sw["lows"]
    if len(lows) >= 2:
        (i1, p1), (i2, p2) = lows[-2], lows[-1]
        o1, o2 = _osc(i1), _osc(i2)
        if o1 is not None and o2 is not None and p2 < p1 and o2 > o1:
            found.append((i2, "haussière"))
    highs = sw["highs"]
    if len(highs) >= 2:
        (i1, p1), (i2, p2) = highs[-2], highs[-1]
        o1, o2 = _osc(i1), _osc(i2)
        if o1 is not None and o2 is not None and p2 > p1 and o2 < o1:
            found.append((i2, "baissière"))
    if not found:
        return None
    return max(found, key=lambda f: f[0])[1]


def relative_volume(candles: list[Candle], period: int = 20) -> float | None:
    """Volume de la dernière bougie rapporté à la moyenne des `period` précédentes.

    > 1 = participation supérieure à la normale (mouvement soutenu) ; < 1 = mouvement mou.
    None si le marché n'a pas de volume centralisé (forex spot).
    """
    if len(candles) < period + 1:
        return None
    avg = sum(c.volume for c in candles[-period - 1 : -1]) / period
    if avg <= 0:
        return None
    return candles[-1].volume / avg


def acc_dist(candles: list[Candle]) -> list[float]:
    """Accumulation/Distribution Line."""
    if not candles:
        return []
    out = []
    ad = 0.0
    for c in candles:
        if c.high == c.low:
            clv = 0.0
        else:
            clv = ((c.close - c.low) - (c.high - c.close)) / (c.high - c.low)
        ad += clv * c.volume
        out.append(ad)
    return out
