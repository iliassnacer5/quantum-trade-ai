"""Tests des briques de structure de marché : HH/HL/LH/LL, BOS et CHOCH.

Ces trois lectures décident QUAND on entre (après la cassure de la correction, jamais pendant),
donc elles doivent être franches : une mèche qui dépasse ne vaut pas une cassure, et une cassure
vieille de vingt bougies n'est plus un déclencheur.
"""

from __future__ import annotations

from app.domain import market_structure as ms
from app.domain.indicators import Candle


def _c(o, h, low, c, v=1000.0):
    return Candle(o, h, low, c, v)


def _zigzag(n: int, start: float, step: float, up: bool = True) -> list[Candle]:
    """Tendance en escalier : 5 bougies dans le sens, 2 de repli -> vrais HH et HL."""
    out, p = [], start
    for i in range(n):
        move = step * (1.0 if i % 7 < 5 else -0.6)
        p += move if up else -move
        out.append(_c(p - step * 0.3, p + step * 0.5, p - step * 0.5, p))
    return out


def test_uptrend_is_labelled_with_higher_highs_and_higher_lows():
    res = ms.label_swings(_zigzag(80, 1.1000, 0.0020, up=True))
    assert res["state"] == "haussière"
    assert "HH" in res["sequence"] and "HL" in res["sequence"]
    assert res["last_higher_low"] is not None


def test_downtrend_is_labelled_with_lower_highs_and_lower_lows():
    res = ms.label_swings(_zigzag(80, 1.3000, 0.0020, up=False))
    assert res["state"] == "baissière"
    assert "LH" in res["sequence"] and "LL" in res["sequence"]
    assert res["last_lower_high"] is not None


def test_a_range_is_not_a_trend():
    out = []
    for i in range(80):
        d = 0.0010 * (1 if i % 4 < 2 else -1)
        p = 1.2000 + d
        out.append(_c(p, p + 0.0004, p - 0.0004, p))
    assert ms.label_swings(out)["state"] in ("range", "indéterminée")


def test_the_first_pivot_of_each_kind_stays_unlabelled():
    """Le premier sommet n'a aucun sommet auquel se comparer : il ne doit pas être classé au hasard."""
    res = ms.label_swings(_zigzag(60, 1.1000, 0.0020))
    highs = [p for p in res["points"] if p["side"] == "high"]
    lows = [p for p in res["points"] if p["side"] == "low"]
    assert highs and highs[0]["kind"] is None
    assert lows and lows[0]["kind"] is None


def _uptrend_then_correction() -> tuple[list[Candle], float]:
    """Tendance haussière, PUIS correction : le sommet de la correction est le niveau à casser.

    C'est la situation réelle d'une entrée : on attend que le repli soit cassé vers le haut pour
    rentrer dans le sens de la tendance de fond.
    """
    series = _zigzag(60, 1.1000, 0.0015, up=True)
    p = series[-1].close
    for _ in range(10):                     # le repli
        p -= 0.0008
        series.append(_c(p + 0.0003, p + 0.0006, p - 0.0006, p))
    level = ms.label_swings(series)["last_swing_high"]
    return series, level


def test_bos_needs_a_close_beyond_the_level_not_a_wick():
    """Une mèche qui dépasse puis rentre est un piège, pas une cassure de structure."""
    base, level = _uptrend_then_correction()
    # Bougie dont la MÈCHE dépasse largement le sommet mais qui clôture sous lui.
    wick_only = base + [_c(level - 0.0005, level + 0.0030, level - 0.0010, level - 0.0002)]
    assert ms.detect_bos(wick_only, 1) is None
    # Même bougie, mais qui CLÔTURE au-dessus : cette fois c'est une cassure.
    real = base + [_c(level - 0.0005, level + 0.0030, level - 0.0010, level + 0.0020)]
    bos = ms.detect_bos(real, 1)
    assert bos is not None and bos["bars_ago"] == 0


def test_a_stale_break_is_not_an_entry_trigger():
    """La cassure d'il y a vingt bougies n'est plus un déclencheur : le train est parti sans nous."""
    base, level = _uptrend_then_correction()
    series = base + [_c(level, level + 0.0030, level - 0.0005, level + 0.0020)]
    # Le prix retombe ensuite doucement : plus aucun sommet n'est franchi depuis.
    p = series[-1].close
    for _ in range(20):
        p -= 0.0002
        series.append(_c(p + 0.0001, p + 0.0003, p - 0.0003, p))
    assert ms.detect_bos(series, 1, max_bars_ago=10) is None
    # Sans la borne de fraîcheur, la même cassure est bien retrouvée — elle a juste vieilli.
    assert ms.detect_bos(series, 1, max_bars_ago=60) is not None


def test_bos_direction_matters():
    """Dans une tendance haussière saine, les creux montent : rien n'est cassé vers le bas."""
    series = _zigzag(70, 1.1000, 0.0015, up=True)
    assert ms.detect_bos(series, 1) is not None
    assert ms.detect_bos(series, -1) is None
    assert ms.detect_bos(series, 0) is None        # sans direction, pas de question posée
    assert ms.detect_bos(series, 0) is None        # sans direction, pas de question posée


def test_choch_marks_the_end_of_a_correction():
    """Une micro-baisse cassée par le haut : la correction est finie, pas la tendance de fond."""
    correction = _zigzag(60, 1.2000, 0.0015, up=False)
    level = ms.label_swings(correction)["last_swing_high"]
    series = correction + [_c(level - 0.0005, level + 0.0025, level - 0.0008, level + 0.0018)]
    res = ms.detect_choch(series)
    assert res is not None
    assert res["against"] == -1 and res["direction"] == 1


def test_choch_is_silent_without_a_readable_micro_trend():
    out = []
    for i in range(60):
        d = 0.0008 * (1 if i % 3 else -1)
        p = 1.1000 + d
        out.append(_c(p, p + 0.0003, p - 0.0003, p))
    assert ms.detect_choch(out) is None


def test_short_history_says_nothing_rather_than_guessing():
    tiny = [_c(1.1, 1.1005, 1.0995, 1.1002) for _ in range(4)]
    assert ms.label_swings(tiny)["state"] == "indéterminée"
    assert ms.detect_bos(tiny, 1) is None
    assert ms.detect_choch(tiny) is None
