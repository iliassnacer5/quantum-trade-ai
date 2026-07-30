"""Tests des zones Supply & Demand et des niveaux S/R classés par importance.

Ce que ces briques doivent garantir : une zone n'existe que si une impulsion l'a créée, sa force
baisse à chaque retest, et un niveau vu en 4 h pèse plus lourd qu'un micro-pivot 15 min.
"""

from __future__ import annotations

from app.domain import zones
from app.domain.indicators import Candle


def _c(o, h, low, c, v=1000.0):
    return Candle(o, h, low, c, v)


def _flat(n: int, price: float, amp: float = 0.0004) -> list[Candle]:
    """Bougies de base : petits corps, le marché piétine."""
    out = []
    for i in range(n):
        d = amp * 0.2 * (1 if i % 2 else -1)
        out.append(_c(price, price + amp, price - amp, price + d))
    return out


def _impulse(start: float, n: int, step: float, up: bool = True) -> list[Candle]:
    out, p = [], start
    for _ in range(n):
        nxt = p + step if up else p - step
        out.append(_c(p, max(p, nxt) + step * 0.1, min(p, nxt) - step * 0.1, nxt))
        p = nxt
    return out


def test_a_demand_zone_needs_an_impulse_to_exist():
    """Sans départ brutal, une phase de calme n'est qu'une pause : aucune zone."""
    assert zones.supply_demand_zones(_flat(60, 1.1000)) == []


def test_base_then_impulse_creates_a_demand_zone():
    series = _flat(40, 1.1000) + _impulse(1.1000, 12, 0.0030, up=True)
    found = zones.supply_demand_zones(series)
    assert found, "une base suivie d'une impulsion haussière doit produire une zone de demande"
    assert any(z["kind"] == "demand" for z in found)


def test_impulse_downwards_creates_a_supply_zone():
    series = _flat(40, 1.3000) + _impulse(1.3000, 12, 0.0030, up=False)
    found = zones.supply_demand_zones(series)
    assert any(z["kind"] == "supply" for z in found)


def test_a_retested_zone_is_weaker_than_a_fresh_one():
    """Chaque retour dans la zone consomme les ordres qui s'y trouvaient."""
    fresh = _flat(40, 1.1000) + _impulse(1.1000, 20, 0.0030, up=True)
    zf = [z for z in zones.supply_demand_zones(fresh) if z["kind"] == "demand"]
    assert zf and zf[0]["fresh"] is True

    # Même départ, mais le prix revient chercher la zone avant de repartir.
    retested = (_flat(40, 1.1000) + _impulse(1.1000, 8, 0.0030, up=True)
                + _impulse(1.1240, 9, 0.0030, up=False)      # retour dans la base
                + _impulse(1.0970, 20, 0.0030, up=True))
    zr = [z for z in zones.supply_demand_zones(retested) if z["kind"] == "demand"]
    if zr:
        assert zr[0]["touches"] >= 1
        assert zr[0]["strength"] <= zf[0]["strength"]


def test_zone_containing_finds_the_zone_under_the_price():
    series = _flat(40, 1.1000) + _impulse(1.1000, 12, 0.0030, up=True)
    found = zones.supply_demand_zones(series)
    demand = next(z for z in found if z["kind"] == "demand")
    inside = (demand["low"] + demand["high"]) / 2
    assert zones.zone_containing(found, inside, "demand") is not None
    assert zones.zone_containing(found, demand["high"] + 1.0, "demand") is None


def _swings(n: int, price: float, amp: float) -> list[Candle]:
    """Oscillation régulière : le même sommet et le même creux sont retouchés plusieurs fois."""
    out = []
    for i in range(n):
        up = i % 10 < 5
        p = price + (amp if up else -amp)
        out.append(_c(price, p + amp * 0.1, p - amp * 0.1, p))
    return out


def test_levels_seen_on_a_higher_timeframe_score_higher():
    """Le même prix vu en 4 h pèse plus lourd que vu seulement en 15 min."""
    m15 = _swings(120, 1.2000, 0.0050)
    h4 = _swings(120, 1.2000, 0.0050)
    only_m15 = zones.ranked_levels(m15, None, None, 1.2000)
    with_h4 = zones.ranked_levels(m15, None, h4, 1.2000)
    assert only_m15 and with_h4
    assert max(lv["score"] for lv in with_h4) > max(lv["score"] for lv in only_m15)


def test_levels_are_split_between_support_and_resistance_around_the_price():
    m15 = _swings(120, 1.2000, 0.0050)
    levels = zones.ranked_levels(m15, None, None, 1.2000)
    assert any(lv["side"] == "support" and lv["price"] < 1.2000 for lv in levels)
    assert any(lv["side"] == "resistance" and lv["price"] > 1.2000 for lv in levels)


def test_a_repeatedly_touched_level_scores_higher_than_a_lone_pivot():
    m15 = _swings(150, 1.2000, 0.0050)
    levels = zones.ranked_levels(m15, None, None, 1.2000)
    strongest = max(levels, key=lambda lv: lv["score"])
    assert strongest["touches"] >= 2
    assert 0.0 < strongest["score"] <= 1.0


def test_no_history_returns_nothing_rather_than_inventing_levels():
    assert zones.ranked_levels([], None, None, 1.1) == []
    assert zones.supply_demand_zones([]) == []
    assert zones.nearest_level([], 1.1, "support") is None
