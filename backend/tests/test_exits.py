"""Tests de l'ÉTAPE 3 : où placer le stop et les objectifs, avant d'envoyer l'ordre.

Ce que le moteur doit garantir :
- le stop est posé sur un NIVEAU qui invalide le scénario, et il le dit explicitement ;
- l'objectif est posé devant un obstacle réel, jamais au-delà ;
- un R/R hors de la bande configurée est une condition BLOQUANTE, pas un ajustement ;
- les deux règles de sécurisation (+2R et 80 % de TP1) coexistent sans jamais faire reculer le stop.
"""

from __future__ import annotations

from app.domain import exits
from app.domain.indicators import Candle


def _c(o, h, low, c, v=1000.0):
    return Candle(o, h, low, c, v)


def _zone(kind: str, low: float, high: float, strength: float = 0.8) -> dict:
    return {"kind": kind, "low": low, "high": high, "mid": (low + high) / 2, "index": 0,
            "touches": 0, "fresh": True, "strength": strength, "timeframe": "15m"}


def _level(price: float, side: str, score: float = 0.8, touches: int = 3) -> dict:
    return {"price": price, "side": side, "score": score, "touches": touches,
            "timeframes": ["15m", "1h"], "last_touch_bars": 10}


# --- Le stop ------------------------------------------------------------------------------------

def test_the_stop_goes_below_the_demand_zone_that_justified_the_entry():
    """Si le prix traverse la zone, les ordres attendus n'étaient pas là : l'idée est morte."""
    stop, why = exits.plan_stop(
        entry=100.0, direction=1, h4=[], zones=[_zone("demand", 97.0, 98.0)],
        sr_levels=[], structure={}, atr15=0.4, min_distance=1.0, max_distance=6.0,
    )
    assert stop < 97.0, "le stop doit être SOUS la zone, pas dedans"
    assert "zone de demande" in why and "invalide" in why


def test_the_stop_falls_back_on_the_structure_pivot():
    """Sans zone exploitable, c'est le dernier creux plus haut qui porte le stop."""
    stop, why = exits.plan_stop(
        entry=100.0, direction=1, h4=[], zones=[],
        sr_levels=[], structure={"last_higher_low": 96.5}, atr15=0.4,
        min_distance=1.0, max_distance=6.0,
    )
    assert stop < 96.5
    assert "creux plus haut" in why and "structure" in why


def test_a_level_too_weak_to_be_defended_is_not_used_for_the_stop():
    """Un micro-pivot que le marché ne défend pas ne peut pas porter un stop."""
    fallback = (90.0, "structure 4 h")
    stop, why = exits.plan_stop(
        entry=100.0, direction=1, h4=[], zones=[],
        sr_levels=[_level(97.0, "support", score=0.2)], structure={}, atr15=0.4,
        min_distance=1.0, max_distance=6.0, fallback=fallback,
    )
    assert (stop, why) == fallback


def test_the_closest_valid_level_wins():
    """À niveaux également valables, le plus serré donne le meilleur rapport."""
    stop, why = exits.plan_stop(
        entry=100.0, direction=1, h4=[], zones=[],
        sr_levels=[_level(90.0, "support"), _level(97.0, "support")],
        structure={}, atr15=0.4, min_distance=1.0, max_distance=6.0,
    )
    assert 96.0 < stop < 97.0
    assert "97" in why


def test_the_stop_mirrors_on_a_sell():
    stop, why = exits.plan_stop(
        entry=100.0, direction=-1, h4=[], zones=[_zone("supply", 102.0, 103.0)],
        sr_levels=[], structure={}, atr15=0.4, min_distance=1.0, max_distance=6.0,
    )
    assert stop > 103.0
    assert "offre" in why


def test_without_any_usable_level_the_stop_says_so():
    stop, why = exits.plan_stop(
        entry=100.0, direction=1, h4=[], zones=[], sr_levels=[], structure={},
        atr15=0.4, min_distance=2.0, max_distance=6.0,
    )
    assert stop == 98.0
    assert "aucun niveau exploitable" in why


# --- Les objectifs ------------------------------------------------------------------------------

def test_tp1_is_the_first_level_that_pays_the_risk():
    """Risque de 2 : TP1 doit valoir au moins 4 (1:2) et au plus 8 (1:4)."""
    plan = exits.plan_targets(
        entry=100.0, stop=98.0, direction=1, zones=[],
        sr_levels=[_level(101.0, "resistance"),      # trop proche, on le franchira
                   _level(105.0, "resistance"),      # premier utile
                   _level(107.0, "resistance")],
        structure={}, fib_ext=None, barrier=None, min_rr=2.0, max_rr=4.0, atr15=0.4,
    )
    assert 104.0 < plan["tp1"] < 105.0, "l'objectif se pose DEVANT la résistance"
    assert plan["rr_ok"] is True and 2.0 <= plan["rr"] <= 4.0
    assert "résistance 105" in plan["target_basis"]


def test_tp2_is_the_next_level_still_inside_the_band():
    plan = exits.plan_targets(
        entry=100.0, stop=98.0, direction=1, zones=[],
        sr_levels=[_level(105.0, "resistance"), _level(107.0, "resistance")],
        structure={}, fib_ext=None, barrier=None, min_rr=2.0, max_rr=4.0, atr15=0.4,
    )
    assert plan["tp2"] is not None and plan["tp2"] > plan["tp1"]
    assert "107" in plan["tp2_basis"]


def test_a_fibonacci_extension_can_carry_the_objective():
    plan = exits.plan_targets(
        entry=100.0, stop=98.0, direction=1, zones=[], sr_levels=[],
        structure={}, fib_ext={"levels": {"1.272": 105.0, "1.618": 107.5}},
        barrier=None, min_rr=2.0, max_rr=4.0, atr15=0.4,
    )
    assert plan["tp1"] == 105.0
    assert "extension de Fibonacci 1.272" in plan["target_basis"]


def test_without_any_level_the_objective_is_arithmetic_and_says_so():
    """On ne prétend pas avoir trouvé un niveau quand il n'y en a pas."""
    plan = exits.plan_targets(
        entry=100.0, stop=98.0, direction=1, zones=[], sr_levels=[],
        structure={}, fib_ext=None, barrier=None, min_rr=2.0, max_rr=4.0, atr15=0.4,
    )
    assert plan["tp1"] == 104.0                     # exactement 1:2
    assert "arithmétique" in plan["target_basis"]
    assert plan["rr_ok"] is True


def test_a_configured_pip_floor_raises_the_objective_instead_of_refusing_it():
    """Un plancher configuré doit faire VISER plus loin, pas faire rejeter le trade en aval."""
    plan = exits.plan_targets(
        entry=100.0, stop=98.0, direction=1, zones=[], sr_levels=[],
        structure={}, fib_ext=None, barrier=None, min_rr=2.0, max_rr=4.0, atr15=0.4,
        floor_distance=7.0,          # bien au-delà du 1:2 (soit 4,0)
    )
    assert plan["tp1"] == 107.0
    assert plan["rr"] == 3.5 and plan["rr_ok"] is True
    assert "plancher" in plan["target_basis"]


def test_the_major_barrier_caps_the_second_objective():
    plan = exits.plan_targets(
        entry=100.0, stop=98.0, direction=1, zones=[], sr_levels=[],
        structure={}, fib_ext=None, barrier=106.0, min_rr=2.0, max_rr=4.0, atr15=0.4,
    )
    assert plan["tp2"] == 106.0
    assert "niveau majeur" in plan["tp2_basis"]


def test_no_second_objective_is_invented_when_it_would_duplicate_the_first():
    plan = exits.plan_targets(
        entry=100.0, stop=98.0, direction=1, zones=[], sr_levels=[],
        structure={}, fib_ext=None, barrier=104.0, min_rr=2.0, max_rr=4.0, atr15=0.4,
    )
    assert plan["tp1"] == 104.0
    assert plan["tp2"] is None


def test_targets_mirror_on_a_sell():
    plan = exits.plan_targets(
        entry=100.0, stop=102.0, direction=-1, zones=[],
        sr_levels=[_level(95.0, "support")],
        structure={}, fib_ext=None, barrier=None, min_rr=2.0, max_rr=4.0, atr15=0.4,
    )
    assert plan["tp1"] > 95.0 and plan["tp1"] < 100.0
    assert plan["rr_ok"] is True


def test_a_null_risk_produces_no_plan_rather_than_a_division_by_zero():
    plan = exits.plan_targets(
        entry=100.0, stop=100.0, direction=1, zones=[], sr_levels=[],
        structure={}, fib_ext=None, barrier=None, min_rr=2.0, max_rr=4.0, atr15=0.4,
    )
    assert plan["tp1"] is None and plan["rr_ok"] is False


# --- Sécurisation TP1 -> TP2 --------------------------------------------------------------------

def test_the_tp1_lock_secures_eighty_percent_of_the_way():
    assert exits.tp1_lock_stop(100.0, 110.0, 1, fraction=0.8) == 108.0
    assert exits.tp1_lock_stop(100.0, 90.0, -1, fraction=0.8) == 92.0


def test_momentum_confirms_a_healthy_continuation():
    series = []
    p = 100.0
    for i in range(80):
        p += 0.5 if i % 7 < 5 else -0.3
        series.append(_c(p - 0.2, p + 0.3, p - 0.3, p))
    res = exits.momentum_still_supports(series, 1)
    assert res["rsi"] is not None and res["macd_hist"] is not None


def test_an_exhausted_rsi_stops_the_continuation():
    series = [_c(100.0 + i, 100.5 + i, 99.5 + i, 100.0 + i) for i in range(80)]
    res = exits.momentum_still_supports(series, 1)
    assert res["ok"] is False
    assert any("RSI" in r for r in res["reasons"])


def test_without_history_the_gain_is_taken_rather_than_risked():
    """Sans information sur le momentum, on ne prolonge pas le risque."""
    res = exits.momentum_still_supports([_c(100, 101, 99, 100)] * 10, 1)
    assert res["ok"] is False
    assert "insuffisant" in res["reasons"][0]
