"""Tests des figures de price action (domaine) et de leur usage comme confirmation d'entrée.

Le déplacement depuis `agents/pattern.py` ne doit RIEN changer pour l'agent chartiste : c'est
vérifié ici explicitement, parce qu'une régression y serait silencieuse.
"""

from __future__ import annotations

from app.domain import price_action as pa
from app.domain.indicators import Candle


def _c(o, h, low, c, v=1000.0):
    return Candle(o, h, low, c, v)


def _filler(n: int, price: float) -> list[Candle]:
    return [_c(price, price + 0.0005, price - 0.0005, price) for _ in range(n)]


def test_the_agent_still_exposes_the_same_detection():
    """`agents.pattern.detect_patterns` doit rester le MÊME objet que celui du domaine."""
    from app.agents import pattern as pattern_agent

    assert pattern_agent.detect_patterns is pa.detect_patterns


def test_bullish_engulfing_is_detected():
    series = _filler(5, 1.1000) + [
        _c(1.1010, 1.1012, 1.0990, 1.0992),          # bougie baissière
        _c(1.0990, 1.1030, 1.0988, 1.1015),          # avalée haussière
    ]
    names = [n for n, _ in pa.detect_patterns(series)]
    assert "avalée haussière" in names


def test_entry_pattern_only_returns_figures_aligned_with_the_direction():
    series = _filler(5, 1.1000) + [
        _c(1.1010, 1.1012, 1.0990, 1.0992),
        _c(1.0990, 1.1030, 1.0988, 1.1015),          # avalée haussière
    ]
    found = pa.entry_pattern(series, 1)
    assert found is not None and found[0] == "avalée haussière"
    assert pa.entry_pattern(series, -1) is None      # rien de baissier ici


def test_entry_pattern_ignores_figures_outside_the_accepted_list():
    """Une « structure haussière » sur trois bougies n'engage pas de capital."""
    series = _filler(5, 1.1000) + [
        _c(1.1000, 1.1010, 1.0995, 1.1005),
        _c(1.1005, 1.1015, 1.1000, 1.1010),
        _c(1.1010, 1.1020, 1.1005, 1.1015),          # trois bougies en escalier, rien de plus
    ]
    names = [n for n, _ in pa.detect_patterns(series)]
    assert "structure haussière" in names
    assert not (set(names) & pa.ENTRY_PATTERNS)
    assert pa.entry_pattern(series, 1) is None


def test_inside_bar_counts_but_with_a_modest_quality():
    """Elle annonce une cassure sans en donner le sens : elle compte peu."""
    series = _filler(5, 1.1000) + [
        _c(1.1000, 1.1040, 1.0960, 1.1030),          # grande bougie
        _c(1.1010, 1.1020, 1.0990, 1.1005),          # inside bar
    ]
    found = pa.entry_pattern(series, 1)
    assert found is not None and found[0] == "inside bar (compression)"
    assert found[1] == 0.3


def test_the_strongest_figure_wins():
    """Étoile du matin (0,7) devant une avalée (0,6) quand les deux se forment."""
    series = _filler(5, 1.1000) + [
        _c(1.1030, 1.1032, 1.0980, 1.0985),          # forte bougie baissière
        _c(1.0985, 1.0990, 1.0975, 1.0982),          # petit corps
        _c(1.0982, 1.1035, 1.0980, 1.1025),          # reprise franche
    ]
    names = [n for n, _ in pa.detect_patterns(series)]
    assert "étoile du matin" in names
    found = pa.entry_pattern(series, 1)
    assert found is not None and found[1] >= 0.6


def test_no_direction_means_no_confirmation():
    series = _filler(5, 1.1000) + [
        _c(1.1010, 1.1012, 1.0990, 1.0992),
        _c(1.0990, 1.1030, 1.0988, 1.1015),
    ]
    assert pa.entry_pattern(series, 0) is None


def test_too_few_candles_detects_nothing():
    assert pa.detect_patterns([_c(1.1, 1.1005, 1.0995, 1.1002)]) == []
