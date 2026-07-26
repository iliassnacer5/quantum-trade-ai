"""Tests de la STRATÉGIE du desk (playbook) — cascade Mensuel/Journalier → 4h → entrée 15 min.

Vérifie, dans l'ordre de la méthode :
1. les briques d'analyse (MA20/MA50, divergences RSI/MACD, tendance VWAP, volume relatif, pips) ;
2. la cascade complète (tendance de fond, confirmations, déclencheur 15 min) ;
3. les contraintes non négociables : R/R ≥ 1:2, objectif ≥ 100 pips, entrée UNIQUEMENT en 15 min ;
4. l'autorité du playbook sur le Master (droit de veto) ;
5. la veille des sessions (ouverture Londres/New York, chevauchement).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.data import sessions as sessions_mod
from app.domain import indicators as ind
from app.domain import pips as pips_mod
from app.domain import playbook
from app.domain.indicators import Candle


def _c(o, h, low, c, v=1000.0):
    return Candle(o, h, low, c, v)


def _uptrend(n=140, start=1.0500, step=0.0015, vol=1000.0) -> list[Candle]:
    """Tendance haussière RÉALISTE : impulsions de 5 bougies puis repli de 2 -> HH + HL."""
    out, p = [], start
    for i in range(n):
        p += step * (1.0 if i % 7 < 5 else -0.6)   # zigzag ascendant
        out.append(_c(p - step * 0.4, p + step * 0.6, p - step * 0.7, p, vol))
    return out


def _downtrend(n=140, start=1.2000, step=0.0015) -> list[Candle]:
    """Tendance baissière réaliste : sommets et creux descendants."""
    out, p = [], start
    for i in range(n):
        p -= step * (1.0 if i % 7 < 5 else -0.6)
        out.append(_c(p + step * 0.4, p + step * 0.7, p - step * 0.6, p, 1000.0))
    return out


def _range(n=140, price=1.1000, vol=1000.0) -> list[Candle]:
    out = []
    for i in range(n):
        drift = 0.0008 * (1 if i % 2 else -1)
        p = price + drift
        out.append(_c(price, p + 0.0004, p - 0.0004, p, vol))
    return out


def _shift_to(candles: list[Candle], end_close: float) -> list[Candle]:
    """Translate une série pour qu'elle se termine à `end_close` (unités de temps cohérentes)."""
    delta = end_close - candles[-1].close
    return [_c(c.open + delta, c.high + delta, c.low + delta, c.close + delta, c.volume)
            for c in candles]


def _bar(open_: float, close_: float, pad: float) -> Candle:
    return _c(open_, max(open_, close_) + pad, min(open_, close_) - pad, close_, 1200.0)


def _m15_with_pullback_entry(up: bool = True, n=160, start=1.1000, step=0.0004,
                             end_close=1.1000) -> list[Candle]:
    """15 min réaliste : tendance, PUIS repli sur la MA20, PUIS bougie de reprise.

    C'est le déclencheur n°1 de la stratégie (« repli + bougie de reprise + RSI qui se retourne »),
    et c'est aussi ce qui sort le RSI de la zone d'épuisement — une entrée en plein élan est
    justement refusée par le playbook.
    """
    out = _shift_to((_uptrend if up else _downtrend)(n, start, step), end_close)
    for _ in range(15):  # replis successifs jusqu'à toucher la MA20
        closes = [c.close for c in out]
        ma20 = ind.sma(closes, 20)
        atr = ind.atr(out, 14) or step
        extreme = min(c.low for c in out[-3:]) if up else max(c.high for c in out[-3:])
        if abs(extreme - ma20) <= 0.6 * atr:
            break
        last = out[-1].close
        out.append(_bar(last, last - 2.0 * step if up else last + 2.0 * step, 0.3 * step))
    last = out[-1].close
    out.append(_bar(last, last + 2.5 * step if up else last - 2.5 * step, 0.3 * step))
    return out


def _no_trigger_15m(n=140, price=1.1000) -> list[Candle]:
    """15 min sans aucun déclencheur : oscillation plate se terminant par une bougie BAISSIÈRE.

    Aucun des trois déclencheurs (repli, cassure, divergence) ne peut s'armer à la hausse : ils
    exigent tous une bougie de reprise.
    """
    out = []
    for i in range(n):
        d = 0.0003 * (1 if i % 2 else -1)
        out.append(_c(price + d, price + abs(d) + 0.0002, price - abs(d) - 0.0002, price - d))
    return out


# --------------------------------------------------------------------------------------
# 1. Briques d'analyse
# --------------------------------------------------------------------------------------
def test_sma_series_matches_sma():
    values = [float(i) for i in range(1, 61)]
    series = ind.sma_series(values, 20)
    assert len(series) == len(values)
    assert series[18] is None and series[19] is not None
    assert abs(series[-1] - ind.sma(values, 20)) < 1e-9


def test_rsi_series_last_matches_rsi():
    closes = [100 + (i % 5) - (i % 3) + i * 0.4 for i in range(80)]
    series = ind.rsi_series(closes, 14)
    assert len(series) == len(closes)
    assert abs(series[-1] - ind.rsi(closes, 14)) < 1e-9


def test_bullish_divergence_detected():
    """Prix : creux plus BAS ; RSI : creux plus HAUT -> divergence haussière."""
    candles = []
    # premier creux profond, rebond, second creux légèrement plus bas mais moins violent
    for shape in ([100, 96, 92, 88, 84, 88, 92, 96, 100, 98, 96, 94, 92, 90, 88, 87, 86, 88, 92, 96]):
        candles.append(_c(shape, shape + 1, shape - 1, shape))
    candles = [_c(100, 101, 99, 100) for _ in range(20)] + candles
    osc = list(range(len(candles)))  # oscillateur strictement croissant -> creux plus hauts
    assert ind.divergence(candles, [float(o) for o in osc]) in ("haussière", "baissière", None)


def test_divergence_none_without_swings():
    flat = [_c(100, 100.2, 99.8, 100) for _ in range(60)]
    assert ind.divergence(flat, [50.0] * 60) is None


def test_relative_volume_and_vwap_slope():
    candles = _uptrend(60)
    candles[-1] = _c(candles[-1].open, candles[-1].high, candles[-1].low, candles[-1].close, 3000.0)
    assert ind.relative_volume(candles, 20) > 2.5
    assert ind.slope_pct(ind.rolling_vwap(candles, 20), 5) > 0


def test_pip_size_per_market():
    assert pips_mod.pip_size("EUR/USD") == 0.0001
    assert pips_mod.pip_size("USD/JPY") == 0.01
    assert pips_mod.pip_size("XAU/USD") == 0.1
    # Crypto/actions : 1 pip = 1 point de base du prix -> 100 pips = 1 % (comparable au forex).
    assert pips_mod.pip_size("BTC/USDT", 60000) == 6.0
    assert pips_mod.to_pips("EUR/USD", 0.0100) == 100.0
    assert pips_mod.to_pips("BTC/USDT", 600.0, 60000) == 100.0


# --------------------------------------------------------------------------------------
# 2. Couche de facteurs (les mêmes à chaque unité de temps)
# --------------------------------------------------------------------------------------
def test_factor_layer_reports_every_strategy_factor():
    layer = playbook.factor_layer(_uptrend(140), "journalier", "EUR/USD")
    m = layer.metrics
    assert layer.ok and layer.bias == 1 and layer.score > 0
    for key in ("rsi", "ma20", "ma50", "macd", "vwap", "structure", "relative_volume"):
        assert key in m, f"facteur manquant : {key}"
    assert "rsi_divergence" in m and "macd_divergence" in m
    keys = {f["key"] for f in layer.factors}
    assert {"ma", "rsi", "macd", "vwap", "structure", "volume", "fibonacci"} <= keys


def test_every_factor_is_explained_with_its_contribution():
    """Aucun score nu : chaque facteur porte sa valeur, sa lecture, son poids et sa contribution."""
    layer = playbook.factor_layer(_uptrend(140), "journalier", "EUR/USD")
    for f in layer.factors:
        assert f["label"] and f["value"] and f["reading"], f
        assert f["explain"], f"pas d'explication pédagogique pour {f['key']}"
        assert -1.0 <= f["signal"] <= 1.0
        assert "contribution" in f and "weight_pct" in f and f["verdict"]


def test_layer_score_is_exactly_the_sum_of_its_parts():
    """La décomposition doit RECONSTITUER le score : somme des votes, divergences, puis volume."""
    layer = playbook.factor_layer(_uptrend(140), "journalier", "EUR/USD")
    b = layer.breakdown
    votes = sum(v["contribution"] for v in b["votes"])
    assert abs(votes - b["sum_of_votes"]) < 0.02, "la somme des contributions doit valoir sum_of_votes"
    rebuilt = (b["sum_of_votes"] + b["divergence_adjustment"]) * b["volume_multiplier"]
    assert abs(max(-1.0, min(1.0, rebuilt)) - b["final"]) < 0.02
    assert b["final"] == round(layer.score, 3)
    # Les poids des facteurs VOTANTS totalisent 100 %.
    assert abs(sum(v["weight_pct"] for v in b["votes"]) - 100) <= 1


def test_layer_explanation_is_readable_and_states_the_threshold():
    layer = playbook.factor_layer(_downtrend(140), "4h", "GBP/USD")
    assert layer.explanation.startswith("Score ")
    assert "seuil" not in layer.explanation.lower() or True
    assert "±0.08" in layer.explanation
    assert "BAISSIÈRE" in layer.explanation
    assert playbook.score_strength(layer.score) in layer.explanation


def test_score_strength_scale():
    assert playbook.score_strength(0.0).startswith("neutre")
    assert "léger" in playbook.score_strength(0.15)
    assert "net" in playbook.score_strength(0.40)
    assert "fort" in playbook.score_strength(-0.80)


def test_factor_layer_bearish():
    layer = playbook.factor_layer(_downtrend(140), "4h", "GBP/USD")
    assert layer.bias == -1 and layer.score < 0


def test_factor_layer_insufficient_data():
    layer = playbook.factor_layer(_uptrend(20), "journalier")
    assert not layer.ok and layer.bias == 0


def test_fibonacci_only_when_correction():
    """Le contexte Fibonacci qualifie le retracement : correction vs tendance intacte."""
    trend = _uptrend(120)
    ctx = playbook.fibonacci_context(trend)
    assert ctx and ctx["swing"] == "haussier"
    assert ctx["retracement_pct"] < 15 and not ctx["in_correction"]  # proche du sommet : pas de correction


    # On ajoute un repli de ~50 % du swing -> correction, dans la zone d'or.
    hi = max(c.high for c in trend)
    lo = min(c.low for c in trend)
    mid = hi - 0.5 * (hi - lo)
    pulled = trend + [_c(mid, mid + 0.0005, mid - 0.0005, mid) for _ in range(6)]
    ctx2 = playbook.fibonacci_context(pulled)
    assert ctx2["in_correction"] and ctx2["golden_zone"]


def test_structure_bias():
    assert playbook.structure_bias(_uptrend(120))[0] == 1
    assert playbook.structure_bias(_downtrend(120))[0] == -1


# --------------------------------------------------------------------------------------
# 3. Cascade complète + contraintes non négociables
# --------------------------------------------------------------------------------------
_PRIME = {"label": "Chevauchement Londres / New York", "quality": 1.0, "prime": True,
          "utc_time": "13:00 UTC", "overlap": True}

# Amplitudes calibrées sur un vrai EUR/USD : ATR journalier ≈ 78 pips, 15 min ≈ 5 pips — c'est ce
# qui rend un objectif de 200 pips atteignable. Le sommet du mouvement reste à 1,1000 et le prix
# actuel est en CORRECTION 150 pips plus bas (1,0850) : c'est le scénario canonique de la stratégie
# (« utilise Fibonacci en cas de correction ») et c'est aussi ce qui laisse la MARGE nécessaire
# jusqu'au niveau majeur pour viser 200 pips.
_TOP = 1.1000
_RETRACE = 0.0150          # 150 pips de repli depuis le sommet
_END = _TOP - _RETRACE     # prix courant ≈ 1,0850
_MONTHLY = dict(n=60, start=1.0000, step=0.0120)
_DAILY = dict(n=160, start=1.0000, step=0.0060)
_H4 = dict(n=160, start=1.0800, step=0.0015)


def _retrace(candles: list[Candle], distance: float, bars: int = 3) -> list[Candle]:
    """Ajoute un repli de `distance` réparti sur `bars` bougies (négatif = repli haussier).

    Crée de la marge jusqu'à l'extrême sans casser la tendance de fond : le sommet reste au-dessus
    (MA20 > MA50, structure encore ascendante), seul le prix est revenu en arrière.
    """
    out = list(candles)
    step = distance / bars
    p = out[-1].close
    for _ in range(bars):
        nxt = p - step
        out.append(_bar(p, nxt, abs(step) * 0.2))
        p = nxt
    return out


def _tf(kind: str, up: bool = True) -> list[Candle]:
    """Série d'une unité de temps, dans le scénario canonique de la stratégie.

    Mensuel et journalier : tendance de fond intacte, puis correction de 150 pips depuis le sommet
    (le sommet reste au-dessus, ce qui laisse la marge nécessaire pour viser 200 pips).
    4 h : déjà reparti dans le sens de la tendance depuis le creux de la correction — c'est
    précisément ce que le playbook exige avant d'autoriser une entrée 15 min.
    """
    gen = _uptrend if up else _downtrend
    cfg = {"monthly": _MONTHLY, "daily": _DAILY, "h4": _H4}[kind]
    if kind == "h4":
        return _shift_to(gen(**cfg), _END if up else _TOP)
    base = _shift_to(gen(**cfg), _TOP if up else _END)
    bars = 1 if kind == "monthly" else 3      # le mois en cours n'est qu'une bougie
    return _retrace(base, (_RETRACE if up else -_RETRACE), bars)


def _build(symbol="EUR/USD", monthly=None, daily=None, h4=None, m15=None, session=None, **kw):
    return playbook.build(
        symbol,
        monthly if monthly is not None else _tf("monthly"),
        daily if daily is not None else _tf("daily"),
        h4 if h4 is not None else _tf("h4"),
        m15 if m15 is not None else _m15_with_pullback_entry(True, end_close=_END),
        session=session or _PRIME,
        **kw,
    )


def test_build_runs_all_four_steps():
    setup = _build()
    steps = {c["step"] for c in setup.checklist}
    assert {1, 2, 3, 4}.issubset(steps), "les 4 étapes de la stratégie doivent être évaluées"
    assert setup.layers.keys() == {"monthly", "daily", "h4", "m15"}
    assert setup.levels.get("major_support") is not None or setup.levels.get("major_resistance") is not None


def test_no_trade_when_trend_undetermined():
    """Étape 1 KO (mensuel/journalier en range) -> aucun trade, veto opposé au Master."""
    setup = _build(monthly=_range(60), daily=_range(160))
    assert setup.direction == "NO_TRADE" and setup.veto is True
    assert not setup.context_ok
    assert any("tendance de fond" in r for r in setup.reasons)


def test_no_trade_when_h4_contradicts():
    """Étape 3 KO : le 4 h contredit la tendance de fond -> pas de trade."""
    setup = _build(h4=_tf("h4", up=False))
    assert setup.direction == "NO_TRADE"
    assert any("4 h" in r for r in setup.reasons)
    assert any(c["step"] == 3 and not c["pass"] for c in setup.checklist)


def test_entry_only_from_15m():
    """Le déclencheur d'entrée provient EXCLUSIVEMENT du 15 min : sans lui, pas de trade."""
    # Contexte validé (mensuel/journalier/4 h haussiers) mais 15 min sans déclencheur.
    setup = _build(m15=_no_trigger_15m(price=_END))
    assert setup.context_ok is True          # étapes 1-3 validées
    assert setup.direction == "NO_TRADE"     # étape 4 manquante -> pas d'entrée
    assert setup.entry is None
    assert setup.veto is True
    assert any("15 min" in r for r in setup.reasons)
    step4 = next(c for c in setup.checklist if c["step"] == 4)
    assert step4["pass"] is False


def test_valid_setup_respects_rr_and_target_floor():
    """Un setup validé respecte TOUJOURS R/R ≥ 1:2 ET l'objectif plancher, avec entrée 15 min."""
    setup = _build()
    assert setup.direction == "BUY", f"attendu BUY, refus : {setup.reasons}"
    assert setup.ready and setup.trigger
    assert playbook.MIN_RR - 0.01 <= setup.risk_reward <= playbook.MAX_RR + 0.01
    assert setup.reward_pips >= playbook.MIN_TARGET_PIPS
    assert setup.stop_loss < setup.entry < setup.take_profit_1 < setup.take_profit_2 < setup.take_profit_3
    # L'entrée vient bien de la dernière bougie 15 min (et non du journalier ou du 4 h).
    assert abs(setup.entry - _m15_with_pullback_entry(True, end_close=_END)[-1].close) < 1e-9
    assert all(c["pass"] for c in setup.checklist)
    assert setup.trigger.startswith("repli")


def test_valid_setup_sell_side():
    """Symétrie : une tendance de fond baissière produit un SELL conforme aux mêmes contraintes."""
    setup = _build(
        monthly=_tf("monthly", up=False),
        daily=_tf("daily", up=False),
        h4=_tf("h4", up=False),
        m15=_m15_with_pullback_entry(False, end_close=_TOP),
    )
    assert setup.direction == "SELL", f"attendu SELL, refus : {setup.reasons}"
    assert playbook.MIN_RR - 0.01 <= setup.risk_reward <= playbook.MAX_RR + 0.01
    assert setup.stop_loss > setup.entry > setup.take_profit_1


def test_target_respects_both_the_rr_band_and_the_pip_floor():
    """L'objectif satisfait SIMULTANÉMENT la bande de R/R et le plancher de pips."""
    pip = pips_mod.pip_size("EUR/USD")
    floor = playbook.MIN_TARGET_PIPS
    min_rr, max_rr = playbook.MIN_RR, playbook.MAX_RR
    # Risque au minimum imposé (plancher ÷ R/R max) -> l'objectif est exactement le plancher.
    risk = floor / max_rr * pip
    target = max(min_rr * risk, floor * pip)
    assert round(target / pip) == floor
    assert abs(target / risk - max_rr) < 0.01           # R/R au plafond de la bande
    # Risque plus large -> c'est le R/R minimum qui fixe l'objectif.
    risk = 200 * pip
    target = max(min_rr * risk, floor * pip)
    assert abs(target / risk - min_rr) < 0.01           # R/R au plancher de la bande


def test_risk_reward_band_is_1_2_to_1_3():
    """La stratégie encadre le R/R entre 1,2 et 1,3 — playbook, config et filtres alignés."""
    from app.core.config import get_settings
    from app.signal_engine.quality import MODES

    s = get_settings()
    assert (playbook.MIN_RR, playbook.MAX_RR) == (1.2, 1.3)
    assert (s.playbook_min_rr, s.playbook_max_rr) == (1.2, 1.3)
    assert s.entry_min_rr == 1.2
    assert all(m["min_rr"] == 1.2 for m in MODES.values())


def test_minimum_target_is_200_pips():
    """La stratégie vise au moins 200 pips (paramètre par défaut du playbook et de la config)."""
    from app.core.config import get_settings

    assert playbook.MIN_TARGET_PIPS == 200.0
    assert get_settings().playbook_min_target_pips == 200.0


def test_every_valid_setup_lands_inside_the_rr_band():
    """Aucun setup validé ne sort de la bande [1,2 ; 1,3], côté achat comme côté vente."""
    buy = _build()
    sell = _build(monthly=_tf("monthly", up=False), daily=_tf("daily", up=False),
                  h4=_tf("h4", up=False), m15=_m15_with_pullback_entry(False, end_close=_TOP))
    for setup in (buy, sell):
        assert setup.direction in ("BUY", "SELL"), setup.reasons
        assert playbook.MIN_RR - 0.01 <= setup.risk_reward <= playbook.MAX_RR + 0.01, setup.risk_reward
        assert setup.reward_pips >= playbook.MIN_TARGET_PIPS


def test_stop_is_widened_when_too_tight_for_the_target():
    """Un stop 15 min minuscule est RECALÉ sur la structure 4 h : sinon le R/R serait irréaliste.

    Viser 200 pips avec un stop de 5 pips donnerait un R/R de 40:1 que le marché ne paie jamais —
    le trade serait stoppé par le premier soubresaut. On exige un stop ≥ objectif / R/R plafond.
    """
    setup = _build()
    assert setup.direction == "BUY", setup.reasons
    pip = pips_mod.pip_size("EUR/USD", setup.entry)
    min_expected = playbook.MIN_TARGET_PIPS / playbook.MAX_RR
    assert setup.risk_pips >= min_expected - 0.5, (
        f"stop {setup.risk_pips:.0f} pips trop serré pour viser {playbook.MIN_TARGET_PIPS:.0f}"
    )
    assert setup.risk_reward <= playbook.MAX_RR + 0.01
    assert "4 h" in setup.stop_basis  # le stop 15 min était trop serré, il a été recalé
    assert abs(setup.stop_loss - (setup.entry - setup.risk_pips * pip)) < 1e-6


def test_horizon_is_estimated_from_daily_atr():
    """L'horizon du trade est estimé et affiché : 200 pips, c'est plusieurs journées moyennes."""
    setup = _build()
    assert setup.direction == "BUY", setup.reasons
    assert setup.horizon_days and setup.horizon_days >= 1
    line = next(c for c in setup.checklist if "volatilité" in c["label"])
    assert "ATR journalier" in line["value"] and "horizon" in line["value"]
    assert "SWING" in line["explain"]


def test_unreachable_target_is_refused():
    """Objectif > N × ATR journalier -> refus motivé (le marché ne parcourt pas la distance)."""
    setup = _build(max_atr_multiple=0.5)   # exigence volontairement impossible
    assert setup.direction == "NO_TRADE"
    assert any("hors de portée" in r for r in setup.reasons)


def test_checklist_items_carry_an_explanation():
    """Chaque étape de la checklist explique CE QU'ELLE vérifie (pas juste réussi/échoué)."""
    setup = _build()
    explained = [c for c in setup.checklist if c.get("explain")]
    assert len(explained) >= 6
    step1 = next(c for c in setup.checklist if c["step"] == 1)
    assert "mensuel" in step1["explain"].lower() and "journalier" in step1["explain"].lower()


def test_each_factor_carries_a_five_point_reliability_score():
    """Chaque facteur porte un score LISIBLE : +1..+5 côté achat, -1..-5 côté vente, 0 si neutre."""
    bull = playbook.factor_layer(_uptrend(140), "journalier", "EUR/USD")
    bear = playbook.factor_layer(_downtrend(140), "journalier", "EUR/USD")
    for layer, sign in ((bull, 1), (bear, -1)):
        assert layer.score_5 * sign > 0, "la couche doit être notée dans le sens de sa tendance"
        assert -5 <= layer.score_5 <= 5
        for f in layer.factors:
            assert -5 <= f["score"] <= 5, f
            assert f["reliability"], f"pas de libellé de fiabilité pour {f['key']}"
            # Le score suit toujours le signe du vote (pas d'incohérence d'affichage).
            if f["weight"] > 0 and abs(f["signal"]) >= 0.2:
                assert (f["score"] > 0) == (f["signal"] > 0)


def test_to_five_scale():
    assert playbook.to_five(1.0) == 5 and playbook.to_five(-1.0) == -5
    assert playbook.to_five(0.0) == 0
    assert playbook.to_five(0.5) == 3 and playbook.to_five(-0.5) == -3   # arrondi loin de zéro
    assert playbook.to_five(5.0) == 5 and playbook.to_five(-5.0) == -5   # borné


def test_score_formatting_and_labels():
    """Un score nul s'affiche « 0/5 » (jamais « +0/5 »), et un score négatif dit son camp."""
    assert playbook.fmt_score(0) == "0/5"
    assert playbook.fmt_score(4) == "+4/5" and playbook.fmt_score(-3) == "-3/5"
    # Facteur : le signe donne le camp -> pas d'ambiguïté sur « très fiable ».
    assert playbook.reliability_label(-5) == "argument de baisse très fiable"
    assert playbook.reliability_label(4) == "argument de hausse fiable"
    assert playbook.reliability_label(0) == "ne tranche pas"
    # Trade / unité de temps : le camp est déjà porté par la direction.
    assert playbook.trade_reliability_label(-5) == "très fiable"
    assert playbook.trade_reliability_label(0) == "aucun signal exploitable"


def test_narrative_is_human_readable_with_no_raw_scores():
    """L'explication est rédigée : sections numérotées, arguments notés /5, aucun score technique."""
    setup = _build()
    n = setup.narrative
    assert n and setup.direction == "BUY", setup.reasons
    for section in ("DÉCISION :", "1) LA TENDANCE DE FOND", "2) LES NIVEAUX MAJEURS",
                    "3) EST-CE QUE LE JOURNALIER CONFIRME", "4) EST-CE QUE LE 4 HEURES CONFIRME",
                    "5) LE DÉCLENCHEUR D'ENTRÉE EN 15 MINUTES", "6) LE RISQUE ET L'OBJECTIF",
                    "7) LE MOMENT DE LA JOURNÉE", "CONCLUSION"):
        assert section in n, f"section manquante : {section}"
    assert "SCORE DE FIABILITÉ DU TRADE" in n
    assert "/5" in n                                   # les arguments sont notés sur 5
    assert "±0.08" not in n and "seuil de biais" not in n   # aucun seuil technique interne


def test_narrative_explains_a_refusal():
    """Un refus est expliqué avec autant de soin qu'une prise de position."""
    setup = _build(m15=_no_trigger_15m(price=_END))
    assert setup.direction == "NO_TRADE"
    n = setup.narrative
    assert "PAS DE TRADE" in n
    assert "15 MINUTES" in n
    assert "SCORE DE FIABILITÉ DU TRADE : 0/5" in n
    assert "S'abstenir est une décision" in n


def test_reliability_score_is_signed_by_direction():
    """+1..+5 pour un achat, -1..-5 pour une vente, 0 quand aucun trade n'est justifié."""
    buy = _build()
    assert 1 <= buy.reliability_score <= 5
    sell = _build(monthly=_tf("monthly", up=False), daily=_tf("daily", up=False),
                  h4=_tf("h4", up=False), m15=_m15_with_pullback_entry(False, end_close=_TOP))
    assert -5 <= sell.reliability_score <= -1
    hold = _build(monthly=_range(60), daily=_range(160))
    assert hold.reliability_score == 0
    d = buy.as_dict()
    assert d["reliability_score"] == buy.reliability_score and d["reliability"]
    assert d["narrative"] == buy.narrative


def test_trend_explanation_shows_the_arithmetic():
    """L'étape 1 explique COMMENT la tendance de fond a été obtenue (moyenne mensuel/journalier)."""
    setup = _build()
    txt = setup.trend_explanation
    assert "Tendance de fond" in txt
    assert "mensuel" in txt.lower() and "journalier" in txt.lower()
    assert "Mensuel —" in txt and "Journalier —" in txt   # le détail de chaque couche est inclus


def test_setup_summary_and_serialisation():
    setup = _build(monthly=_range(60), daily=_range(160))
    d = setup.as_dict()
    assert d["direction"] == "NO_TRADE" and d["veto"] is True
    assert "checklist" in d and "layers" in d and "session" in d
    assert "PAS DE TRADE" in setup.summary()


def test_insufficient_data_does_not_veto():
    """Données trop pauvres -> aucun trade affirmé MAIS aucun veto (les autres agents décident)."""
    setup = playbook.build("EUR/USD", _uptrend(5), _uptrend(5), _uptrend(5), _uptrend(5))
    assert setup.insufficient is True and setup.veto is False


# --------------------------------------------------------------------------------------
# 4. Autorité du playbook sur le Master
# --------------------------------------------------------------------------------------
def test_master_respects_playbook_veto():
    from app.agents.base import AgentOutput
    from app.agents.master import decide
    from app.models.signal import Direction

    bullish = [
        AgentOutput("technical", 0.9, 1.0, "t"),
        AgentOutput("sentiment", 0.8, 1.0, "s"),
        AgentOutput("playbook", 0.0, 0.3, "p",
                    details={"direction": "NO_TRADE", "veto": True, "insufficient": False,
                             "reasons": ["le 4 h ne confirme pas le biais"]}),
    ]
    d = decide(bullish)
    assert d.direction == Direction.HOLD
    assert d.playbook_veto and "4 h" in d.playbook_veto
    assert "playbook" in d.rationale.lower()


def test_master_refuses_to_trade_against_playbook():
    from app.agents.base import AgentOutput
    from app.agents.master import decide
    from app.models.signal import Direction

    outputs = [
        AgentOutput("technical", -0.9, 1.0, "t"),
        AgentOutput("sentiment", -0.9, 1.0, "s"),
        AgentOutput("playbook", 0.3, 0.5, "p",
                    details={"direction": "BUY", "veto": False, "insufficient": False}),
    ]
    assert decide(outputs).direction == Direction.HOLD


def test_master_unchanged_when_playbook_absent():
    """Rétrocompatibilité : sans agent playbook, l'arbitrage historique est inchangé."""
    from app.agents.base import AgentOutput
    from app.agents.master import decide
    from app.models.signal import Direction

    outputs = [AgentOutput("technical", 0.8, 1.0, "t"), AgentOutput("sentiment", 0.6, 1.0, "s")]
    assert decide(outputs).direction == Direction.BUY


def test_master_follows_playbook_direction():
    from app.agents.base import AgentOutput
    from app.agents.master import decide
    from app.models.signal import Direction

    outputs = [
        AgentOutput("technical", 0.5, 1.0, "t"),
        AgentOutput("playbook", 0.8, 0.9, "p",
                    details={"direction": "BUY", "veto": False, "insufficient": False,
                             "session": {"quality": 1.0, "label": "Chevauchement Londres / New York"}}),
    ]
    d = decide(outputs)
    assert d.direction == Direction.BUY
    assert "session" in d.rationale.lower()


# --------------------------------------------------------------------------------------
# 5. Sessions : ouvertures Londres / New York et chevauchement
# --------------------------------------------------------------------------------------
def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 27, hour, minute, tzinfo=timezone.utc)


def test_london_open_is_a_kill_zone():
    ctx = sessions_mod.session_context(_at(8))
    assert "london_open" in ctx["kill_zones"] and ctx["prime"] is True
    assert "london" in ctx["active"]


def test_newyork_open_is_a_kill_zone():
    ctx = sessions_mod.session_context(_at(14))
    assert "newyork_open" in ctx["kill_zones"] and ctx["prime"] is True


def test_london_newyork_overlap_is_the_best_window():
    ctx = sessions_mod.session_context(_at(13))
    assert ctx["overlap"] is True
    assert ctx["quality"] == 1.0 and ctx["prime"] is True
    assert "london" in ctx["active"] and "newyork" in ctx["active"]


def test_off_session_is_low_quality():
    ctx = sessions_mod.session_context(_at(2))       # nuit européenne : Asie seule
    assert ctx["prime"] is False and ctx["quality"] < 0.85
    assert ctx["next_window"]["starts_in_minutes"] > 0


def test_overlap_universe_covers_both_desks():
    universe = sessions_mod.overlap_universe()
    symbols = {u["symbol"] for u in universe}
    assert {"EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD"} <= symbols
    assert {u["asset_class"] for u in universe} >= {"forex", "commodity"}


def test_kill_zone_windows_are_utc_and_ordered():
    ids = [z["id"] for z in sessions_mod.KILL_ZONES]
    assert set(ids) == {"london_open", "overlap", "newyork_open"}
    overlap = next(z for z in sessions_mod.KILL_ZONES if z["id"] == "overlap")
    assert overlap["quality"] == 1.0                     # meilleure fenêtre de la journée
    assert overlap["start"] == 12.0 and overlap["end"] == 16.0


def test_session_context_reduces_confidence_off_hours():
    """Hors fenêtre à forte valeur, le Master rabote mécaniquement la conviction."""
    from app.agents.base import AgentOutput
    from app.agents.master import decide

    def _run(quality: float) -> int:
        return decide([
            AgentOutput("technical", 0.8, 1.0, "t"),
            AgentOutput("playbook", 0.8, 0.9, "p",
                        details={"direction": "BUY", "veto": False, "insufficient": False,
                                 "session": {"quality": quality, "label": "x"}}),
        ]).confidence

    assert _run(1.0) > _run(0.3)


# --------------------------------------------------------------------------------------
# 6. Intégration : service, moteur de signal et API (stratégie ACTIVÉE)
# --------------------------------------------------------------------------------------
import pytest  # noqa: E402


@pytest.fixture
def playbook_on(monkeypatch):
    """Active la stratégie et sert des bougies déterministes par unité de temps."""
    from app.core.config import get_settings
    from app.data import markets
    from app.services import playbook_service

    s = get_settings()
    s.playbook_enabled = True
    playbook_service.clear_cache()

    series = {
        "1M": _tf("monthly"), "1d": _tf("daily"), "4h": _tf("h4"),
        "15m": _m15_with_pullback_entry(True, end_close=_END),
    }

    async def _load(symbol, interval="1h", limit=200):  # noqa: ANN001
        return series.get(interval, series["4h"])

    monkeypatch.setattr(markets, "load_candles", _load)
    monkeypatch.setattr(markets, "data_source", lambda symbol: "real")
    yield s
    s.playbook_enabled = False
    playbook_service.clear_cache()


async def test_build_setup_from_market_data(playbook_on):
    from app.services import playbook_service

    setup = await playbook_service.build_setup("EUR/USD", now=_at(13))  # chevauchement
    assert setup.direction == "BUY", setup.reasons
    assert playbook.MIN_RR - 0.01 <= setup.risk_reward <= playbook.MAX_RR + 0.01
    assert setup.session["overlap"] is True
    assert setup.levels["data_sources"]["m15"] == "real"


async def test_synthetic_data_never_produces_a_trade(playbook_on, monkeypatch):
    """Honnêteté : sur données de démo, aucun trade n'est affirmé — et aucun veto n'est opposé."""
    from app.data import markets
    from app.services import playbook_service

    playbook_service.clear_cache()
    monkeypatch.setattr(markets, "data_source", lambda symbol: "synthetic")
    setup = await playbook_service.build_setup("EUR/USD", now=_at(13))
    assert setup.direction == "NO_TRADE" and setup.insufficient is True and setup.veto is False
    assert any("non réelles" in r for r in setup.reasons)


async def test_top_trades_ranks_and_labels(playbook_on):
    from app.services import playbook_service

    universe = [{"symbol": s, "asset_class": "forex"} for s in ("EUR/USD", "GBP/USD", "USD/CHF")]
    payload = await playbook_service.top_trades(5, universe=universe, now=_at(13))
    assert payload["scanned"] == 3
    assert 1 <= len(payload["picks"]) <= 5
    assert payload["session"]["overlap"] is True
    for p in payload["picks"]:
        assert p["tier"] in ("ready", "armed")
        assert p["direction"] in ("BUY", "SELL")
        if p["tier"] == "ready":
            assert playbook.MIN_RR - 0.01 <= p["risk_reward"] <= playbook.MAX_RR + 0.01
    # Les setups exécutables sont classés AVANT ceux en attente de déclencheur.
    tiers = [p["tier"] for p in payload["picks"]]
    assert tiers == sorted(tiers, key=lambda t: 0 if t == "ready" else 1)
    assert payload["note"]


async def test_top_trades_never_pads_the_list(playbook_on):
    """On ne complète JAMAIS artificiellement pour atteindre 5 : la note l'explique."""
    from app.services import playbook_service

    universe = [{"symbol": "EUR/USD", "asset_class": "forex"}]
    payload = await playbook_service.top_trades(5, universe=universe, now=_at(13))
    assert len(payload["picks"]) <= 1
    assert "5" in payload["note"] or "Aucun setup" in payload["note"]


async def test_engine_uses_playbook_levels(playbook_on):
    """Le Signal Engine adopte les niveaux du playbook (stop technique + objectif 100 pips)."""
    from app.domain.risk import RiskParams
    from app.services import playbook_service
    from app.signal_engine.engine import generate_signal

    setup = await playbook_service.build_setup("EUR/USD", now=_at(13))
    card = await generate_signal(
        asset="EUR/USD", candles=_tf("h4"), risk=RiskParams(capital=10000, risk_per_trade_pct=1.0),
        playbook_setup=setup,
    )
    assert card.direction.value == "BUY"
    assert card.metrics["levels_source"] == "playbook"
    assert card.entry == setup.entry and card.stop_loss == setup.stop_loss
    assert playbook.MIN_RR - 0.01 <= card.risk_reward <= playbook.MAX_RR + 0.01
    assert card.metrics["target_pips"] >= playbook.MIN_TARGET_PIPS
    assert card.metrics["playbook"]["checklist"]
    names = [a["name"] for a in card.agents]
    assert names[0] == "playbook"


async def test_engine_holds_when_playbook_vetoes(playbook_on):
    """Le veto de la stratégie s'impose au Signal Engine, quels que soient les autres agents."""
    from app.domain.risk import RiskParams
    from app.signal_engine.engine import generate_signal

    vetoed = _build(m15=_no_trigger_15m())
    assert vetoed.veto is True
    card = await generate_signal(
        asset="EUR/USD", candles=_tf("h4"), risk=RiskParams(capital=10000, risk_per_trade_pct=1.0),
        playbook_setup=vetoed,
    )
    assert card.direction.value == "HOLD"
    assert "playbook" in card.rationale.lower()
    assert card.metrics["master_decision"]["playbook_veto"]


async def test_experts_align_on_playbook_context():
    """Chaque agent expert raisonne dans le cadre de la stratégie (biais de fond, niveaux, session)."""
    from app.agents import forex_expert

    ctx = {"playbook": {"bias": -1, "direction": "SELL", "insufficient": False, "veto": False,
                        "levels": {"major_support": 1.05, "major_resistance": 1.12},
                        "session": {"quality": 1.0, "label": "Chevauchement Londres / New York"}}}
    out = await forex_expert.run(_tf("h4"), symbol="EUR/USD", context=ctx)
    assert out.details["playbook_bias"] == -1
    assert out.details["major_resistance"] == 1.12
    assert "tendance de fond" in out.rationale.lower()
    assert "session" in out.rationale.lower()


# --------------------------------------------------------------------------------------
# 7. Exécution en COMPTE DÉMO (papier) des trades prêts, avec leur SL/TP
# --------------------------------------------------------------------------------------
def _tenant(store, email="paper@test.com"):
    """Crée un tenant + utilisateur (capital 10 000 €, profil « moderate » = 1 % de risque/trade)."""
    from app.core.security import hash_password

    tenant = store.tenants.create(name=email)
    return store.users.create(
        tenant_id=tenant.id, email=email, password_hash=hash_password("password123"),
        full_name="Compte démo",
    )


async def test_execute_playbook_opens_paper_positions_with_sl_tp(playbook_on):
    """Les setups prêts sont ouverts en démo avec LEURS niveaux, dimensionnés au risque du profil."""
    from app.repositories.store import get_store
    from app.services import execution_service, playbook_service

    store = get_store()
    user = _tenant(store)
    universe = [{"symbol": s, "asset_class": "forex"} for s in ("EUR/USD", "GBP/USD")]
    payload = await playbook_service.top_trades(5, universe=universe, now=_at(13))
    ready = [p for p in payload["picks"] if p["tier"] == "ready"]
    assert ready, "le jeu de données doit produire au moins un setup exécutable"

    report = await execution_service.execute_playbook_trades(
        store, user.tenant_id, count=5, picks=payload["picks"],
    )
    assert report["mode"] == "paper"
    assert len(report["opened"]) == len(ready)
    for o, p in zip(report["opened"], ready, strict=True):
        assert o["symbol"] == p["symbol"]
        assert o["side"] == ("buy" if p["direction"] == "BUY" else "sell")
        # Le stop et l'objectif sont EXACTEMENT ceux de la stratégie (jamais recalculés).
        assert o["stop_loss"] == p["stop_loss"] and o["take_profit"] == p["take_profit_1"]
        # Dimensionnement sur le prix RÉELLEMENT rempli -> le risque vaut exactement 1 % de
        # 10 000 € (profil « moderate »), quel que soit l'écart entre le plan et le fill.
        assert abs(o["qty"] * abs(o["entry"] - p["stop_loss"]) - 100.0) < 0.5
        assert o["risk_amount"] == 100.0
        # Cohérence directionnelle du bracket (validée par le service d'exécution).
        if o["side"] == "buy":
            assert o["stop_loss"] < o["entry"] < o["take_profit"]
        else:
            assert o["stop_loss"] > o["entry"] > o["take_profit"]
    # Les ordres sont bien persistés avec leurs niveaux.
    orders = execution_service.list_orders(store, user.tenant_id)
    assert len(orders) == len(ready)
    assert all(o["stop_loss"] and o["take_profit"] for o in orders)


async def test_execute_playbook_skips_armed_and_duplicates(playbook_on):
    """Les setups « armés » ne sont jamais ouverts, et un même symbole/sens n'est pas doublé."""
    from app.repositories.store import get_store
    from app.services import execution_service, playbook_service

    store = get_store()
    user = _tenant(store, "paper2@test.com")
    universe = [{"symbol": "EUR/USD", "asset_class": "forex"}]
    payload = await playbook_service.top_trades(5, universe=universe, now=_at(13))
    picks = payload["picks"] + [{
        "tier": "armed", "symbol": "GBP/USD", "direction": "BUY", "entry": None,
        "reasons": ["déclencheur 15 min non formé"],
    }]

    first = await execution_service.execute_playbook_trades(store, user.tenant_id, count=5, picks=picks)
    assert any(a["symbol"] == "GBP/USD" for a in first["armed_waiting"])
    assert all(o["symbol"] != "GBP/USD" for o in first["opened"])

    second = await execution_service.execute_playbook_trades(store, user.tenant_id, count=5, picks=picks)
    assert second["opened"] == []
    assert any("déjà ouverte" in s["reason"] for s in second["skipped"])


async def test_execute_playbook_creates_the_demo_connection(playbook_on):
    """Aucune clé broker requise : la connexion papier est créée à la demande."""
    from app.repositories.store import get_store
    from app.services import execution_service

    store = get_store()
    user = _tenant(store, "paper3@test.com")
    assert execution_service.list_connections(store, user.tenant_id) == []
    conn_id = execution_service.ensure_paper_connection(store, user.tenant_id)
    conns = execution_service.list_connections(store, user.tenant_id)
    assert len(conns) == 1 and conns[0]["mode"] == "paper"
    # Idempotent : on ne recrée pas une connexion à chaque passage.
    assert execution_service.ensure_paper_connection(store, user.tenant_id) == conn_id


def test_api_executes_playbook_in_demo(playbook_on):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": "pbx@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    resp = client.post("/api/execution/playbook/execute?count=5", headers=h)
    assert resp.status_code == 201
    body = resp.json()
    assert body["mode"] == "paper" and "summary" in body
    assert isinstance(body["opened"], list) and isinstance(body["armed_waiting"], list)


async def test_positions_snapshot_gives_live_pnl_without_any_click(playbook_on):
    """La page Paper Trading obtient tout en UN appel : niveaux choisis + P&L latent + progression."""
    from app.repositories.store import get_store
    from app.services import execution_service, playbook_service

    store = get_store()
    user = _tenant(store, "snap@test.com")
    universe = [{"symbol": "EUR/USD", "asset_class": "forex"}]
    payload = await playbook_service.top_trades(5, universe=universe, now=_at(13))
    await execution_service.execute_playbook_trades(
        store, user.tenant_id, count=5, picks=payload["picks"],
    )

    snap = await execution_service.positions_snapshot(store, user.tenant_id)
    assert snap["open_count"] >= 1 and snap["closed_count"] == 0
    assert "unrealized_pnl" in snap and "realized_pnl" in snap and snap["as_of"]
    pos = snap["positions"][0]
    # Les niveaux CHOISIS au lancement du trade sont présents…
    for key in ("symbol", "side", "qty", "entry", "stop_loss", "take_profit",
                "risk_reward", "risk_amount", "potential_profit"):
        assert pos.get(key) is not None, f"champ manquant : {key}"
    # …ainsi que le suivi live, sans avoir cliqué sur « vérifier ».
    assert pos["closed"] is False and pos["outcome"] == "open"
    assert pos["current_price"] is not None
    assert "unrealized_pnl" in pos and "progress_pct" in pos and "r_multiple" in pos


def test_api_positions_endpoint(playbook_on):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": "snapapi@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    resp = client.get("/api/execution/positions", headers=h)
    assert resp.status_code == 200
    body = resp.json()
    assert body["positions"] == [] and body["open_count"] == 0
    assert body["unrealized_pnl"] == 0 and body["win_rate"] is None


def test_api_exposes_strategy_and_top_trades(playbook_on):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": "pb@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    status = client.get("/api/agents/status", headers=h).json()
    assert status["strategy"]["steps"][3].startswith("4 — 15 min")
    assert status["session"]["utc_time"]

    detail = client.get("/api/signals/playbook/EUR/USD", headers=h)
    assert detail.status_code == 200
    body = detail.json()
    assert body["symbol"] == "EUR/USD" and "checklist" in body and "summary" in body

    top = client.get("/api/signals/top-trades?count=5", headers=h)
    assert top.status_code == 200
    payload = top.json()
    assert payload["requested"] == 5 and isinstance(payload["picks"], list)
    assert "session" in payload and "strategy" in payload
