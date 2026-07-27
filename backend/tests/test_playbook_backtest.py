"""Tests du BACKTEST de la stratégie du desk (forex + or, plusieurs paires, plusieurs années).

Ce qui est vérifié ici, dans l'ordre de ce qu'on demande à un backtest :

1. **Le rejeu d'un trade est honnête** — stop prioritaire quand stop et objectif tombent dans la
   même bougie, sécurisation à +2R effective, expiration du scénario au bout du temps imparti.
2. **L'arithmétique des métriques se reconstitue à la main** (réussite, espérance en R, profit
   factor, drawdown).
3. **Le classement des paires est ordonné et honnête** — une paire sans échantillon suffisant n'est
   pas notée, elle est déclarée non classée.
4. **Le profil des perdants** met en évidence un vrai écart quand il existe, et se tait sinon.
5. **La conclusion ne brode jamais** : sans trade, elle le dit.
"""

from __future__ import annotations

from app.backtest import playbook_backtest as pbt
from app.domain.indicators import Candle


def _c(o: float, h: float, low: float, c: float) -> Candle:
    return Candle(o, h, low, c, 1000.0)


def _flat(n: int, price: float) -> list[Candle]:
    return [_c(price, price + 0.0001, price - 0.0001, price) for _ in range(n)]


# ---------------------------------------------------------------------------------------
# 1. Rejeu d'un trade
# ---------------------------------------------------------------------------------------
def test_replay_stops_out_at_the_initial_stop():
    """Le prix descend jusqu'au stop : perte de exactement 1 R."""
    bars = _flat(3, 100.0) + [_c(100.0, 100.0, 89.0, 90.0)] + _flat(20, 90.0)
    res = pbt.replay_trade(bars, 2, "BUY", entry=100.0, stop=90.0, target=130.0,
                           max_hold=20, secure_at_r=2.0)
    assert res["outcome"] == "lost"
    assert res["r"] == -1.0
    assert res["secured"] is False
    assert res["exit_reason"] == "stop initial"


def test_replay_reaches_the_target():
    """Le prix atteint l'objectif : gain égal au R/R planifié."""
    bars = _flat(3, 100.0) + [_c(100.0, 131.0, 100.0, 130.0)] + _flat(20, 130.0)
    res = pbt.replay_trade(bars, 2, "BUY", entry=100.0, stop=90.0, target=130.0,
                           max_hold=20, secure_at_r=2.0)
    assert res["outcome"] == "won"
    assert res["r"] == 3.0                      # (130-100)/10
    assert res["exit_reason"] == "objectif"


def test_replay_secures_the_gain_at_two_r():
    """+2R touché puis retournement : la position sort GAGNANTE à +2R, pas perdante.

    C'est exactement la règle demandée : une fois +2R atteint, le stop y est remonté et le trade ne
    peut plus redevenir perdant.
    """
    bars = (
        _flat(3, 100.0)
        + [_c(100.0, 121.0, 100.0, 120.0)]      # touche +2R (stop remonté sur 120)
        + [_c(120.0, 120.0, 85.0, 88.0)]        # effondrement : le stop sécurisé est touché
        + _flat(20, 88.0)
    )
    res = pbt.replay_trade(bars, 2, "BUY", entry=100.0, stop=90.0, target=130.0,
                           max_hold=20, secure_at_r=2.0)
    assert res["outcome"] == "won", "le trade doit sortir gagnant grâce à la sécurisation"
    assert res["r"] == 2.0
    assert res["secured"] is True
    assert res["exit_reason"] == "stop sécurisé"


def test_replay_stop_wins_when_both_hit_in_the_same_candle():
    """Stop ET objectif dans la même bougie -> on suppose le stop touché (hypothèse prudente)."""
    bars = _flat(3, 100.0) + [_c(100.0, 131.0, 89.0, 100.0)] + _flat(20, 100.0)
    res = pbt.replay_trade(bars, 2, "BUY", entry=100.0, stop=90.0, target=130.0,
                           max_hold=20, secure_at_r=2.0)
    assert res["outcome"] == "lost", "un backtest ne doit jamais s'accorder le bénéfice du doute"


def test_replay_expires_when_nothing_is_reached():
    """Ni stop ni objectif dans le temps imparti : le scénario expire, sans mentir sur le résultat."""
    bars = _flat(30, 100.0)
    res = pbt.replay_trade(bars, 2, "BUY", entry=100.0, stop=90.0, target=130.0,
                           max_hold=10, secure_at_r=2.0)
    assert res["outcome"] == "expired"
    assert res["exit_reason"] == "expiration du scénario"


def test_replay_works_on_the_sell_side():
    """Symétrie complète côté vente, sécurisation comprise."""
    bars = _flat(3, 100.0) + [_c(100.0, 100.0, 79.0, 80.0)] + [_c(80.0, 115.0, 80.0, 112.0)] + _flat(20, 112.0)
    res = pbt.replay_trade(bars, 2, "SELL", entry=100.0, stop=110.0, target=70.0,
                           max_hold=20, secure_at_r=2.0)
    assert res["secured"] is True and res["outcome"] == "won" and res["r"] == 2.0


# ---------------------------------------------------------------------------------------
# 2. Métriques
# ---------------------------------------------------------------------------------------
def _trade(r: float, at: str, **kw) -> dict:
    base = {"r": r, "at": at, "outcome": "won" if r > 0 else "lost", "planned_rr": 2.5,
            "bars_held": 10, "secured": r >= 2, "reward_pips": 200.0, "risk_pips": 80.0,
            "trigger": "repli", "session": "overlap", "symbol": "EUR/USD"}
    base.update(kw)
    return base


def test_metrics_reconstruct_by_hand():
    trades = [_trade(3.0, "2026-01-01"), _trade(-1.0, "2026-01-02"),
              _trade(2.0, "2026-01-03"), _trade(-1.0, "2026-01-04")]
    m = pbt.metrics(trades)
    assert m["trades"] == 4 and m["wins"] == 2 and m["losses"] == 2
    assert m["win_rate"] == 50.0
    assert abs(m["expectancy_r"] - (3.0 - 1.0 + 2.0 - 1.0) / 4) < 1e-9
    assert abs(m["profit_factor"] - 5.0 / 2.0) < 0.01
    assert m["avg_win_r"] == 2.5 and m["avg_loss_r"] == -1.0
    assert m["total_r"] == 3.0


def test_metrics_max_drawdown_follows_the_equity_curve():
    """Gain puis deux pertes : le drawdown vaut la perte cumulée depuis le sommet."""
    trades = [_trade(3.0, "2026-01-01"), _trade(-1.0, "2026-01-02"), _trade(-1.0, "2026-01-03")]
    assert pbt.metrics(trades)["max_drawdown_r"] == 2.0


def test_metrics_on_empty_input_are_zero_not_none():
    m = pbt.metrics([])
    assert m["trades"] == 0 and m["win_rate"] == 0.0 and m["profit_factor"] is None


# ---------------------------------------------------------------------------------------
# 3. Classement des paires
# ---------------------------------------------------------------------------------------
def test_ranking_orders_by_expectancy_and_flags_small_samples():
    by_symbol = {
        "EUR/USD": pbt.metrics([_trade(3.0, f"2026-01-{i:02d}") for i in range(1, 11)]),
        "GBP/USD": pbt.metrics([_trade(-1.0, f"2026-01-{i:02d}") for i in range(1, 11)]),
        "XAU/USD": pbt.metrics([_trade(3.0, "2026-01-01"), _trade(-1.0, "2026-01-02")]),
    }
    ranking = pbt.rank_pairs(by_symbol, min_trades=8)
    rated = [r for r in ranking if r["rank"]]
    assert [r["symbol"] for r in rated] == ["EUR/USD", "GBP/USD"]
    assert rated[0]["rank"] == 1 and "exploitable" in rated[0]["verdict"]
    assert "non exploitable" in rated[1]["verdict"]
    # Échantillon insuffisant -> non classé, et on le DIT plutôt que de deviner.
    small = next(r for r in ranking if r["symbol"] == "XAU/USD")
    assert small["rank"] is None and "non classé" in small["verdict"]


# ---------------------------------------------------------------------------------------
# 4. Profil des perdants
# ---------------------------------------------------------------------------------------
def test_losers_profile_detects_a_real_difference():
    """Les perdants ont un ADX nettement plus faible -> le profil doit le dire."""
    winners = [_trade(3.0, f"2026-01-{i:02d}", adx=32, alignment=5, confidence=0.8,
                      atr_pct=0.5, stop_atr_daily=1.0) for i in range(1, 9)]
    losers = [_trade(-1.0, f"2026-02-{i:02d}", adx=14, alignment=3, confidence=0.5,
                     atr_pct=0.5, stop_atr_daily=1.0) for i in range(1, 9)]
    profile = pbt.losers_profile(winners + losers)
    assert profile["sample"] == 8
    assert any("tendance" in f for f in profile["findings"])
    assert profile["comparisons"]["adx"]["perdants"] == 14


def test_losers_profile_stays_silent_without_a_real_pattern():
    """Perdants et gagnants identiques : on ne fabrique pas d'explication."""
    rows = ([_trade(3.0, f"2026-01-{i:02d}", adx=25, alignment=4, confidence=0.7,
                    atr_pct=0.5, stop_atr_daily=1.0) for i in range(1, 9)]
            + [_trade(-1.0, f"2026-02-{i:02d}", adx=25, alignment=4, confidence=0.7,
                      atr_pct=0.5, stop_atr_daily=1.0) for i in range(1, 9)])
    profile = pbt.losers_profile(rows)
    assert len(profile["findings"]) == 1
    assert "au hasard" in profile["findings"][0]


def test_losers_profile_refuses_a_tiny_sample():
    profile = pbt.losers_profile([_trade(-1.0, "2026-01-01")], min_trades=3)
    assert "trop peu" in profile["note"]


# ---------------------------------------------------------------------------------------
# 5. Conclusion
# ---------------------------------------------------------------------------------------
def test_conclusion_says_so_when_there_is_no_trade():
    payload = {
        "scope": {"overall": pbt.metrics([]), "ranking": [], "by_trigger": {}, "by_session": {},
                  "losers_profile": {}, "min_trades": 8, "years_covered": 2.0, "pairs_tested": 12},
        "fidelity": {"overall": pbt.metrics([]), "years_covered": 0.2},
    }
    conclusion = pbt.write_conclusion(payload)
    assert conclusion["verdict"] == "non mesurable"
    assert "Aucun trade" in conclusion["headline"]


def test_conclusion_reports_the_measured_numbers():
    trades = [_trade(3.0, f"2026-01-{i:02d}") for i in range(1, 7)] + \
             [_trade(-1.0, f"2026-02-{i:02d}") for i in range(1, 5)]
    scope = {
        "overall": pbt.metrics(trades),
        "ranking": pbt.rank_pairs({"EUR/USD": pbt.metrics(trades)}, 8),
        "by_trigger": {"repli": pbt.metrics(trades)},
        "by_session": {"overlap": pbt.metrics(trades)},
        "losers_profile": pbt.losers_profile(trades),
        "min_trades": 8, "years_covered": 2.0, "pairs_tested": 12,
    }
    payload = {"scope": scope, "fidelity": {"overall": pbt.metrics([]), "years_covered": 0.2}}
    conclusion = pbt.write_conclusion(payload)
    assert "10 trades" in conclusion["headline"]
    assert "6 gagnants" in conclusion["headline"] and "4 perdants" in conclusion["headline"]
    assert conclusion["verdict"] == "exploitable"
    assert any("R/R moyen PLANIFIÉ" in line for line in conclusion["lines"])
    # L'absence de contrôle 15 min est signalée, pas passée sous silence.
    assert any("aucun trade conforme" in line for line in conclusion["lines"])


def test_conclusion_refuses_to_compare_on_a_tiny_15m_sample():
    """1 trade en 15 min ne peut PAS « confirmer » ni « améliorer » 200 trades en 1 h.

    Régression : la conclusion annonçait « le passage au 15 min améliore le résultat » sur la base
    d'un seul trade. Comparer deux espérances sur n=1 n'a aucun sens statistique.
    """
    scope_trades = [_trade(3.0, f"2026-01-{i:02d}") for i in range(1, 7)] + \
                   [_trade(-1.0, f"2026-02-{i:02d}") for i in range(1, 5)]
    scope = {
        "overall": pbt.metrics(scope_trades),
        "ranking": pbt.rank_pairs({"EUR/USD": pbt.metrics(scope_trades)}, 8),
        "by_trigger": {}, "by_session": {}, "losers_profile": {},
        "min_trades": 8, "years_covered": 1.9, "pairs_tested": 12,
    }
    fidelity = {"overall": pbt.metrics([_trade(2.0, "2026-03-01")]), "years_covered": 0.16}
    lines = pbt.write_conclusion({"scope": scope, "fidelity": fidelity})["lines"]
    control = next(line for line in lines if "Contrôle 15 min" in line)
    assert "ne confirme NI n'infirme" in control
    assert not any("améliore le résultat" in line for line in lines)


def test_default_universe_is_forex_and_metals():
    """Le desk travaille le forex et l'or : c'est l'univers par défaut du backtest."""
    assert "XAU/USD" in pbt.DEFAULT_UNIVERSE and "XAG/USD" in pbt.DEFAULT_UNIVERSE
    assert "EUR/USD" in pbt.DEFAULT_UNIVERSE and "GBP/JPY" in pbt.DEFAULT_UNIVERSE
    assert all("/" in s for s in pbt.DEFAULT_UNIVERSE)
    # Aucune crypto ni action : la stratégie est calibrée en pips sur devises et métaux.
    assert not any(s.endswith("/USDT") for s in pbt.DEFAULT_UNIVERSE)
