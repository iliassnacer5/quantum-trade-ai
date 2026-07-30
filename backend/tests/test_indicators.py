"""Tests des indicateurs techniques déterministes."""

import pytest

from app.domain import indicators as ind
from app.domain.indicators import Candle


def test_ema_length_and_trend():
    vals = [float(i) for i in range(1, 51)]
    e = ind.ema(vals, 10)
    assert len(e) == len(vals)
    assert e[-1] < vals[-1]  # EMA en retard sur une série croissante


def test_rsi_strong_uptrend_high():
    closes = [float(i) for i in range(1, 60)]  # hausse monotone
    r = ind.rsi(closes, 14)
    assert r is not None and r > 70


def test_rsi_downtrend_low():
    closes = [float(i) for i in range(60, 1, -1)]
    r = ind.rsi(closes, 14)
    assert r is not None and r < 30


def test_rsi_insufficient_data():
    assert ind.rsi([1, 2, 3], 14) is None


def test_macd_returns_triplet():
    closes = [float(i % 10) + i * 0.1 for i in range(60)]
    res = ind.macd(closes)
    assert res is not None and len(res) == 3


def test_bollinger_ordering():
    closes = [100 + (i % 5) for i in range(30)]
    boll = ind.bollinger(closes, 20)
    assert boll is not None
    low, mid, high = boll
    assert low <= mid <= high


def test_atr_positive():
    candles = [Candle(10, 11, 9, 10.5, 100) for _ in range(30)]
    a = ind.atr(candles, 14)
    assert a is not None and a > 0


def _trend_candles(n=80, start=100.0, step=1.0, up=True) -> list[Candle]:
    out, p = [], start
    for _ in range(n):
        nxt = p + step if up else p - step
        out.append(Candle(p, max(p, nxt) + 0.3, min(p, nxt) - 0.3, nxt, 1000.0))
        p = nxt
    return out


def test_adx_di_agrees_with_adx_and_points_in_the_trend_direction():
    """Le refactoring ne doit rien changer à `adx`, et les directionnels doivent trancher le sens."""
    up = _trend_candles(up=True)
    res = ind.adx_di(up, 14)
    assert res is not None
    value, plus_di, minus_di = res
    assert value == ind.adx(up, 14)
    assert plus_di > minus_di          # une hausse régulière : pression acheteuse dominante

    down = _trend_candles(up=False)
    res_down = ind.adx_di(down, 14)
    assert res_down is not None and res_down[2] > res_down[1]


def test_adx_di_needs_enough_history():
    assert ind.adx_di([Candle(10, 11, 9, 10.5, 100) for _ in range(10)], 14) is None


def test_supertrend_follows_the_direction_of_the_move():
    up = ind.supertrend(_trend_candles(up=True), 10, 3.0)
    assert up is not None and up["direction"] == 1
    assert up["level"] < _trend_candles(up=True)[-1].close   # la bande suit SOUS le prix

    down = ind.supertrend(_trend_candles(up=False), 10, 3.0)
    assert down is not None and down["direction"] == -1


def test_supertrend_reports_a_fresh_flip_as_fresh():
    """Une bascule toute récente est fragile : `flipped_ago` doit le dire."""
    series = _trend_candles(50, 100.0, 1.0, up=True) + _trend_candles(20, 150.0, 3.0, up=False)
    res = ind.supertrend(series, 10, 3.0)
    assert res is not None and res["direction"] == -1
    assert res["flipped_ago"] < 20      # la bascule appartient au segment baissier ajouté


def test_supertrend_says_nothing_without_history():
    assert ind.supertrend([Candle(10, 11, 9, 10.5, 100) for _ in range(5)], 10) is None


def test_fibonacci_extension_projects_beyond_the_swing():
    """Une extension est un OBJECTIF : elle vit au-delà de l'extrême, pas à l'intérieur du swing."""
    up = _trend_candles(120, 100.0, 1.0, up=True)
    ext = ind.fibonacci_extension(up)
    base = ind.fibonacci_levels(up)
    assert ext is not None and base is not None
    assert ext["swing"] == "haussier"
    assert ext["levels"]["1.272"] > base["high"]
    assert ext["levels"]["1.618"] > ext["levels"]["1.272"]


def test_fibonacci_extension_mirrors_on_a_bearish_swing():
    down = _trend_candles(120, 200.0, 1.0, up=False)
    ext = ind.fibonacci_extension(down)
    base = ind.fibonacci_levels(down)
    assert ext is not None and base is not None
    assert ext["swing"] == "baissier"
    assert ext["levels"]["1.272"] < base["low"]
    assert ext["levels"]["1.618"] < ext["levels"]["1.272"]


def test_fibonacci_extension_without_data():
    assert ind.fibonacci_extension([]) is None


# ---------------------------------------------------------------------------------------
# PERFORMANCE : `rolling_vwap` en somme glissante (30/07/2026)
# ---------------------------------------------------------------------------------------
# L'implémentation d'origine re-sommait toute la fenêtre à chaque bougie (quadratique) : profilée
# sur un backtest de 500 bougies, elle pesait 1,4 s de processeur à elle seule, 237 000 appels à
# `sum()` et 1,9 million d'évaluations de générateur. Elle est sur le chemin de `ta.analyze`, donc de
# TOUTE analyse. Ces tests verrouillent le point qui compte : la version rapide doit rendre
# EXACTEMENT le même résultat que la version lente, sinon l'optimisation change la stratégie.
def _rolling_vwap_reference(candles, period=20):
    """L'implémentation d'ORIGINE (quadratique), gardée comme oracle de comparaison."""
    out = []
    for i in range(len(candles)):
        window = candles[max(0, i - period + 1): i + 1]
        cum_vol = sum(c.volume for c in window)
        if cum_vol <= 0:
            out.append(None)
            continue
        cum_pv = sum(((c.high + c.low + c.close) / 3) * c.volume for c in window)
        out.append(cum_pv / cum_vol)
    return out


def _varied_candles(n: int, *, zero_volume: bool = False):
    """Série déterministe à prix ET volumes variables (un volume constant masquerait une erreur
    de fenêtre : toutes les fenêtres auraient le même poids)."""
    out, price = [], 100.0
    for i in range(n):
        price *= 1.0 + (0.004 if i % 3 else -0.003)
        vol = 0.0 if zero_volume else 100.0 + (i * 37 % 900)
        out.append(ind.Candle(price, price * 1.004, price * 0.996, price, vol))
    return out


@pytest.mark.parametrize("period", [1, 5, 20, 50])
def test_rolling_vwap_matches_the_quadratic_reference(period):
    """Le résultat doit être identique à la virgule flottante près, pour toute période."""
    candles = _varied_candles(300)
    fast = ind.rolling_vwap(candles, period)
    slow = _rolling_vwap_reference(candles, period)
    assert len(fast) == len(slow)
    for got, want in zip(fast, slow):
        assert (got is None) == (want is None)
        if want is not None:
            assert got == pytest.approx(want, rel=1e-9)


def test_rolling_vwap_handles_zero_volume_and_short_series():
    """Volumes nuls -> None (pas de division par zéro), et une série plus courte que la période
    reste calculable sur la fenêtre partielle — comme avant l'optimisation."""
    assert ind.rolling_vwap([], 20) == []
    assert all(v is None for v in ind.rolling_vwap(_varied_candles(50, zero_volume=True), 20))
    short = ind.rolling_vwap(_varied_candles(7), 20)
    assert len(short) == 7 and all(v is not None for v in short)


def test_rolling_vwap_window_actually_slides():
    """Garde-fou contre une somme glissante qui n'oublierait jamais les bougies sorties de fenêtre :
    au-delà de `period`, le VWAP ne doit plus dépendre du tout des premières bougies."""
    tail = _varied_candles(60)[40:]
    with_history = ind.rolling_vwap(_varied_candles(60), 10)[-len(tail):]
    # Recalculé sur la seule queue, les 10 dernières valeurs doivent coïncider : passé la période,
    # l'historique antérieur n'influence plus rien.
    tail_only = ind.rolling_vwap(tail, 10)
    assert with_history[-1] == pytest.approx(tail_only[-1], rel=1e-9)
