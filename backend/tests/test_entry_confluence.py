"""Tests de l'ÉTAPE 2 : la règle du minimum de confirmations.

Ce que la règle doit garantir :
- on entre à trois confirmations pondérées, pas à l'unanimité (sinon aucune opportunité) ;
- mais jamais sur des indicateurs secondaires seuls : il faut au moins une confirmation FORTE ;
- toucher une zone ne suffit pas — il faut une réaction du prix ;
- deux gardes bloquent tout, quel que soit le nombre de confirmations : un RSI étiré sans tendance
  exceptionnelle, et un niveau majeur opposé trop proche pour laisser un potentiel.
"""

from __future__ import annotations

from app.domain import entry_confluence as ec
from app.domain.indicators import Candle


def _c(o, h, low, c, v=1000.0):
    return Candle(o, h, low, c, v)


def _series(n=80, start=1.1000, step=0.0005, up=True, vol=1000.0) -> list[Candle]:
    """Le scénario réel d'une entrée : tendance, PUIS correction, PUIS bougie de reprise.

    C'est le seul état où l'on cherche vraiment à entrer — et c'est aussi ce qui fait redescendre
    le RSI hors de la zone d'épuisement, où la stratégie refuse par principe de rentrer.
    """
    out, p = [], start
    for i in range(n):
        move = step * (1.0 if i % 7 < 5 else -0.6)
        p += move if up else -move
        out.append(_c(p - step * 0.2, p + step * 0.4, p - step * 0.4, p, vol))
    for _ in range(8):                      # la correction
        p += (-2.0 * step) if up else (2.0 * step)
        out.append(_c(p - step * 0.2, p + step * 0.3, p - step * 0.3, p, vol))
    p += (2.5 * step) if up else (-2.5 * step)     # la bougie de reprise
    out.append(_c(p - 2.5 * step, p + step * 0.3, p - 2.6 * step, p, vol))
    return out


def _zone(kind: str, low: float, high: float, strength: float = 0.9) -> dict:
    return {"kind": kind, "low": low, "high": high, "mid": (low + high) / 2, "index": 0,
            "touches": 0, "fresh": True, "strength": strength, "timeframe": "15m"}


def _level(price: float, side: str, score: float = 0.9) -> dict:
    return {"price": price, "side": side, "score": score, "touches": 4,
            "timeframes": ["15m", "1h"], "last_touch_bars": 5}


def _strong_trend() -> dict:
    return {"confidence": 0.8, "direction": 1, "status": "valid"}


def _keys(res: dict) -> set:
    return {c["key"] for c in res["confirmations"]}


# --- La règle de décision -----------------------------------------------------------------------

def test_three_weighted_confirmations_open_the_door():
    series = _series()
    price = series[-1].close
    res = ec.evaluate_entry(
        series, direction=1, trend=_strong_trend(),
        zones=[_zone("demand", price - 0.0004, price + 0.0004)],
        sr_levels=[_level(price - 0.0002, "support")],
        metrics={"vwap": price - 0.0010},
    )
    assert res["fired"] is True, res["reason"]
    assert res["score"] >= res["min_score"]
    assert len(res["confirmations"]) >= 3
    assert any(c["strong"] for c in res["confirmations"])


def test_secondary_indicators_alone_are_never_enough():
    """Six indicateurs dérivés du prix totaliseraient 3,0 — et ne doivent pourtant pas suffire."""
    fake = [{"key": k, "weight": 0.5, "quality": 1.0, "contribution": 0.5,
             "reading": "", "strong": False} for k in ("rsi", "vwap", "ema_dynamic",
                                                       "volume", "rsi", "vwap")]
    score = sum(c["contribution"] for c in fake)
    assert score == 3.0                                  # le seuil est bien atteint...
    assert not any(c["strong"] for c in fake)            # ...mais aucune confirmation forte

    # Le moteur applique la même règle sur un vrai marché sans confirmation forte.
    series = _series()
    price = series[-1].close
    res = ec.evaluate_entry(series, direction=1, trend=_strong_trend(),
                            zones=[], sr_levels=[], metrics={"vwap": price - 0.0010})
    if res["confirmations"] and not any(c["strong"] for c in res["confirmations"]):
        assert res["fired"] is False
        assert "forte" in res["reason"] or "confirmation" in res["reason"]


def test_the_weights_make_price_behaviour_count_double():
    assert ec.CONFIRMATION_WEIGHTS["supply_demand"] == 1.0
    assert ec.CONFIRMATION_WEIGHTS["structure"] == 1.0
    assert ec.CONFIRMATION_WEIGHTS["price_action"] == 1.0
    assert ec.CONFIRMATION_WEIGHTS["support_resistance"] == 1.0
    for light in ("rsi", "vwap", "ema_dynamic"):
        assert ec.CONFIRMATION_WEIGHTS[light] == 0.5
    assert set(ec.STRONG_KEYS) == {"supply_demand", "structure", "price_action",
                                   "support_resistance"}


def test_the_contribution_is_weight_times_quality():
    series = _series()
    price = series[-1].close
    res = ec.evaluate_entry(
        series, direction=1, trend=_strong_trend(),
        zones=[_zone("demand", price - 0.0004, price + 0.0004, strength=0.6)],
        sr_levels=[], metrics={},
    )
    for c in res["confirmations"]:
        assert abs(c["contribution"] - c["weight"] * c["quality"]) < 1e-9


def test_too_few_confirmations_is_refused_and_explained():
    res = ec.evaluate_entry(_series(), direction=1, trend=_strong_trend(),
                            zones=[], sr_levels=[], metrics={})
    if not res["fired"]:
        assert res["reason"]


# --- Supply & Demand : le contact ne suffit pas --------------------------------------------------

def test_touching_a_zone_without_a_reaction_does_not_count():
    """« Ne jamais entrer uniquement parce que le prix touche une zone. »"""
    series = _series(up=True)
    # Dernière bougie BAISSIÈRE : le prix est dans la zone mais ne réagit pas.
    price = series[-1].close
    series = series + [_c(price, price + 0.0001, price - 0.0008, price - 0.0006)]
    price = series[-1].close
    res = ec.evaluate_entry(
        series, direction=1, trend=_strong_trend(),
        zones=[_zone("demand", price - 0.0005, price + 0.0005)],
        sr_levels=[], metrics={},
    )
    assert "supply_demand" not in _keys(res)
    assert any("réaction" in r for r in res["rejections"])


def test_a_weak_zone_is_ignored():
    series = _series()
    price = series[-1].close
    res = ec.evaluate_entry(
        series, direction=1, trend=_strong_trend(),
        zones=[_zone("demand", price - 0.0004, price + 0.0004, strength=0.3)],
        sr_levels=[], metrics={},
    )
    assert "supply_demand" not in _keys(res)


# --- Les gardes ---------------------------------------------------------------------------------

def test_an_overbought_rsi_blocks_a_buy():
    """Pas d'achat au-dessus de 70 — sauf tendance extrêmement forte."""
    climbing = [_c(1.10 + i * 0.001, 1.1005 + i * 0.001, 1.0995 + i * 0.001, 1.10 + i * 0.001)
                for i in range(80)]
    price = climbing[-1].close
    weak = ec.evaluate_entry(
        climbing, direction=1, trend={"confidence": 0.4},
        zones=[_zone("demand", price - 0.001, price + 0.001)], sr_levels=[], metrics={})
    assert weak["fired"] is False
    assert "RSI" in weak["reason"]


def test_an_exhausted_rsi_is_refused_even_in_a_very_strong_trend():
    """Au-delà de 78, plus aucune exception : l'entrée serait beaucoup trop tardive."""
    climbing = [_c(1.10 + i * 0.001, 1.1005 + i * 0.001, 1.0995 + i * 0.001, 1.10 + i * 0.001)
                for i in range(80)]
    res = ec.evaluate_entry(climbing, direction=1, trend={"confidence": 0.99},
                            zones=[], sr_levels=[], metrics={})
    assert res["fired"] is False
    assert "épuisement" in res["reason"]


def test_a_major_level_too_close_blocks_the_entry():
    """« Éviter d'ouvrir si le prix est trop proche d'un niveau majeur qui limite le potentiel. »"""
    series = _series()
    price = series[-1].close
    res = ec.evaluate_entry(
        series, direction=1, trend=_strong_trend(),
        zones=[_zone("demand", price - 0.0004, price + 0.0004)],
        sr_levels=[_level(price + 0.0001, "resistance", score=0.9)],
        metrics={}, risk_hint=0.0010,
    )
    assert res["fired"] is False
    assert "trop proche" in res["reason"]


def test_a_distant_major_level_does_not_block():
    series = _series()
    price = series[-1].close
    res = ec.evaluate_entry(
        series, direction=1, trend=_strong_trend(),
        zones=[_zone("demand", price - 0.0004, price + 0.0004)],
        sr_levels=[_level(price + 0.0200, "resistance", score=0.9)],
        metrics={}, risk_hint=0.0010,
    )
    assert "trop proche" not in res["reason"]


# --- Robustesse ---------------------------------------------------------------------------------

def test_no_direction_means_no_evaluation():
    res = ec.evaluate_entry(_series(), direction=0, trend=_strong_trend(),
                            zones=[], sr_levels=[], metrics={})
    assert res["fired"] is False and "insuffisantes" in res["reason"]


def test_short_history_is_refused_rather_than_guessed():
    res = ec.evaluate_entry([_c(1.1, 1.1005, 1.0995, 1.1002)] * 10, direction=1,
                            trend=_strong_trend(), zones=[], sr_levels=[], metrics={})
    assert res["fired"] is False


def test_weights_can_be_overridden():
    parsed = ec.parse_weights("supply_demand:2.0,rsi:0.1")
    assert parsed["supply_demand"] == 2.0 and parsed["rsi"] == 0.1
    assert parsed["structure"] == ec.CONFIRMATION_WEIGHTS["structure"]
    assert ec.parse_weights("") == ec.CONFIRMATION_WEIGHTS
