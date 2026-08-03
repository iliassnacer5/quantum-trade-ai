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
# 1 bis. Gestion TP1 -> TP2
# ---------------------------------------------------------------------------------------
def test_tp1_reached_without_a_second_objective_closes_the_trade():
    """Sans TP2, TP1 reste une sortie sèche — le comportement historique est intact."""
    bars = _flat(3, 100.0) + [_c(100.0, 121.0, 100.0, 120.0)] + _flat(20, 120.0)
    res = pbt.replay_trade(bars, 2, "BUY", entry=100.0, stop=90.0, target=120.0,
                           max_hold=20, secure_at_r=99.0)
    assert res["outcome"] == "won" and res["r"] == 2.0
    assert res["exit_reason"] == "objectif"
    assert res["tp2_managed"] is False


def test_after_tp1_the_stop_locks_eighty_percent_and_the_trade_runs_to_tp2():
    """TP1 touché : le stop monte à 80 % du chemin et la position part chercher TP2."""
    bars = (
        _flat(3, 100.0)
        + [_c(100.0, 120.5, 100.0, 120.0)]      # TP1 (120) touché -> stop verrouillé sur 116
        + [_c(120.0, 141.0, 119.0, 140.0)]      # TP2 (140) atteint
        + _flat(20, 140.0)
    )
    res = pbt.replay_trade(bars, 2, "BUY", entry=100.0, stop=90.0, target=120.0,
                           max_hold=20, secure_at_r=99.0, target2=140.0, tp1_lock_fraction=0.8)
    assert res["outcome"] == "won"
    assert res["r"] == 4.0                      # (140-100)/10
    assert res["exit_reason"] == "second objectif"
    assert res["tp2_managed"] is True


def test_a_failed_run_to_tp2_still_exits_on_the_locked_stop():
    """La course vers TP2 échoue : on garde 80 % du chemin, pas zéro."""
    bars = (
        _flat(3, 100.0)
        + [_c(100.0, 120.5, 100.0, 120.0)]      # TP1 touché -> stop sur 116
        + [_c(120.0, 120.0, 100.0, 101.0)]      # effondrement : le stop verrouillé est touché
        + _flat(20, 101.0)
    )
    res = pbt.replay_trade(bars, 2, "BUY", entry=100.0, stop=90.0, target=120.0,
                           max_hold=20, secure_at_r=99.0, target2=140.0, tp1_lock_fraction=0.8)
    assert res["outcome"] == "won"
    assert res["r"] == 1.6                      # (116-100)/10 = 80 % des 2R acquis
    assert res["exit_reason"] == "stop verrouillé sur TP1"


def test_the_most_favourable_of_the_two_rules_applies():
    """+2R et « 80 % de TP1 » coexistent : le stop retient le meilleur des deux niveaux."""
    # Ici TP1 vaut 4R : 80 % du chemin (+3,2R) est BIEN AU-DESSUS du niveau de sécurisation +2R.
    bars = (
        _flat(3, 100.0)
        + [_c(100.0, 140.5, 100.0, 140.0)]      # +2R (120) puis TP1 (140) dans la même bougie
        + [_c(140.0, 140.0, 100.0, 101.0)]      # effondrement
        + _flat(20, 101.0)
    )
    res = pbt.replay_trade(bars, 2, "BUY", entry=100.0, stop=90.0, target=140.0,
                           max_hold=20, secure_at_r=2.0, target2=180.0, tp1_lock_fraction=0.8)
    assert res["secured"] is True and res["tp2_managed"] is True
    assert res["r"] == 3.2, "le verrou à 80 % de TP1 doit primer sur le niveau +2R, plus bas"


def test_the_stop_never_moves_backwards():
    """Un verrou moins favorable que le stop déjà en place ne doit jamais le faire reculer."""
    bars = (
        _flat(3, 100.0)
        + [_c(100.0, 141.0, 100.0, 140.0)]      # +2R (120) atteint -> stop sur 120
        + [_c(140.0, 140.0, 100.0, 101.0)]
        + _flat(20, 101.0)
    )
    # TP1 très proche de l'entrée : 80 % de son chemin (104) serait PIRE que le stop sécurisé (120).
    res = pbt.replay_trade(bars, 2, "BUY", entry=100.0, stop=90.0, target=105.0,
                           max_hold=20, secure_at_r=2.0, target2=180.0, tp1_lock_fraction=0.8)
    assert res["r"] >= 2.0, "le stop ne doit jamais redescendre sous un niveau déjà verrouillé"


def test_tp2_management_mirrors_on_a_sell():
    bars = (
        _flat(3, 100.0)
        + [_c(100.0, 100.0, 79.5, 80.0)]        # TP1 (80) touché -> stop verrouillé sur 84
        + [_c(80.0, 80.0, 59.0, 60.0)]          # TP2 (60) atteint
        + _flat(20, 60.0)
    )
    res = pbt.replay_trade(bars, 2, "SELL", entry=100.0, stop=110.0, target=80.0,
                           max_hold=20, secure_at_r=99.0, target2=60.0, tp1_lock_fraction=0.8)
    assert res["outcome"] == "won" and res["tp2_managed"] is True
    assert res["r"] == 4.0                      # (100-60)/10


def test_tp_management_ab_compares_the_same_trades():
    trades = [
        {"r": 4.0, "r_tp1_only": 2.0, "tp2_managed": True},
        {"r": 1.6, "r_tp1_only": 2.0, "tp2_managed": True},
        {"r": -1.0, "r_tp1_only": -1.0, "tp2_managed": False},
    ]
    ab = pbt.tp_management_ab(trades)
    assert ab["trades"] == 3 and ab["went_for_tp2"] == 2 and ab["changed_by_rule"] == 2
    assert ab["expectancy_with_tp2_r"] == round((4.0 + 1.6 - 1.0) / 3, 3)
    assert ab["expectancy_tp1_only_r"] == round((2.0 + 2.0 - 1.0) / 3, 3)
    assert ab["delta_r"] == round(ab["expectancy_with_tp2_r"] - ab["expectancy_tp1_only_r"], 3)


def test_tp_management_ab_says_nothing_without_data():
    assert pbt.tp_management_ab([])["trades"] == 0


# ---------------------------------------------------------------------------------------
# 1 ter. A/B stratégie complète
# ---------------------------------------------------------------------------------------
def test_the_strategy_ab_replays_both_complete_versions():
    """L'ancienne version doit être rejouable telle quelle : c'est la seule référence honnête."""
    assert set(pbt.STRATEGY_VARIANTS) == {"legacy", "refonte"}
    legacy = pbt.STRATEGY_VARIANTS["legacy"]
    assert legacy["trend_engine"] is False        # ancien calcul du biais
    assert legacy["entry_mode"] == "legacy"       # déclencheurs seuls
    assert legacy["min_target_pips"] == 200.0 and legacy["max_rr"] == 3.0
    assert pbt.STRATEGY_VARIANTS["refonte"] == {}  # les réglages de production, tels quels


def test_the_verdict_refuses_to_conclude_on_an_empty_sample():
    verdict = pbt._strategy_verdict({"trades": 0}, {"trades": 10}, 0.0)
    assert "ne conclut rien" in verdict


def test_the_verdict_calls_out_over_filtering():
    """Une espérance en hausse obtenue en ne gardant que 40 % des trades n'est pas un progrès."""
    verdict = pbt._strategy_verdict(
        {"trades": 300, "expectancy_r": 0.80}, {"trades": 100, "expectancy_r": 1.20}, 0.667)
    assert "sur-filtrage" in verdict


def test_the_verdict_does_not_dress_up_a_regression():
    verdict = pbt._strategy_verdict(
        {"trades": 300, "expectancy_r": 0.90}, {"trades": 290, "expectancy_r": 0.70}, 0.03)
    assert "n'apporte rien" in verdict


def test_the_verdict_acknowledges_a_real_gain():
    verdict = pbt._strategy_verdict(
        {"trades": 300, "expectancy_r": 0.80}, {"trades": 280, "expectancy_r": 1.10}, 0.07)
    assert "+0.30 R" in verdict and "volume comparable" in verdict


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


def test_default_universe_covers_every_market():
    """La stratégie s'applique à tous les marchés : le backtest doit donc tous les couvrir."""
    from app.data import markets

    for uni in (pbt.CORE_UNIVERSE, pbt.full_universe()):
        classes = {markets.asset_class(s) for s in uni}
        assert classes == {"forex", "commodity", "index", "stock", "crypto"}
        assert len(set(uni)) == len(uni)   # aucun doublon
    # Les indices sont bien reconnus comme tels, et non pris pour des actions.
    assert markets.asset_class("SPX500") == "index"
    assert markets.asset_class("GER40") == "index"
    # L'univers par défaut du backtest est le CATALOGUE COMPLET, pas un extrait choisi d'avance :
    # c'est la mesure qui doit dire où l'edge se trouve.
    from app.data import symbols as symbols_catalog

    assert len(pbt.full_universe()) == len(symbols_catalog.all_symbols())
    assert len(pbt.full_universe()) > len(pbt.CORE_UNIVERSE)


def test_opportunity_rate_averages_rates_not_totals():
    """Le piège de calcul à ne jamais refaire : total ÷ période la plus longue.

    Deux symboles, l'un suivi 600 jours pour 12 trades (0,02/j), l'autre 150 jours pour 15 trades
    (0,10/j). Le bon taux moyen est 0,06 — pas 27/600 = 0,045, qui rapporte tous les trades à la
    plus longue période et sous-estime la fréquence de moitié.
    """
    freq = {
        "EUR/USD": {"trades": 12, "days_evaluated": 600.0, "trades_per_day": 0.02,
                    "days_between_trades": 50.0},
        "XAU/USD": {"trades": 15, "days_evaluated": 150.0, "trades_per_day": 0.10,
                    "days_between_trades": 10.0},
    }
    out = pbt.opportunity_rate(freq)
    assert out["avg_trades_per_day_per_symbol"] == 0.06
    assert out["universe_trades_per_day"] == 0.12          # 2 symboles × 0,06
    assert out["projection"]["40"] == 2.4                  # et non 40 × 0,045 = 1,8
    assert out["universe_trades_per_week"] == 0.8


def test_volume_verdict_refuses_volume_bought_with_quality():
    """Plus de trades n'est un progrès que si le gain TOTAL monte sans dégrader PF ni drawdown."""
    ref = {"trades": 100, "expectancy_r": 0.60, "profit_factor": 2.4, "max_drawdown_r": 9.0,
           "total_r": 60.0}
    # Deux fois plus de trades mais une espérance divisée par trois : le total baisse -> à écarter.
    worse = {"trades": 200, "expectancy_r": 0.20, "profit_factor": 1.3, "max_drawdown_r": 18.0,
             "total_r": 40.0}
    assert pbt._volume_verdict(worse, ref).startswith("🔴")
    # Plus de trades, total en hausse, PF et drawdown au moins aussi bons -> adoptable.
    better = {"trades": 140, "expectancy_r": 0.62, "profit_factor": 2.5, "max_drawdown_r": 8.0,
              "total_r": 86.8}
    assert pbt._volume_verdict(better, ref).startswith("✅")
    # Total en hausse mais drawdown dégradé : c'est un arbitrage, pas une évidence.
    traded = {"trades": 200, "expectancy_r": 0.45, "profit_factor": 1.9, "max_drawdown_r": 15.0,
              "total_r": 90.0}
    assert pbt._volume_verdict(traded, ref).startswith("🟡")


def test_the_five_year_pass_is_off_because_it_was_measured_empty():
    """La passe 5 ans est désactivée par DÉCISION MESURÉE, pas par oubli.

    Cinq échelles de trade essayées (objectif 1,8 à 3,6 × ATR, stop 0,55 à 1,80 ×) : toutes rendent
    1 à 2 trades sur 5 ans et 6 symboles. Le contexte est pourtant validé 30 % du temps — c'est
    l'étape d'ENTRÉE qui ne se déclenche pas, parce que ses confirmations sont de la micro-structure
    lue sur l'unité d'entrée. À l'échelle journalière, cette information a déjà servi à établir la
    tendance. Ce test existe pour qu'on ne remette pas le réglage à True sans nouvelle mesure.
    """
    from app.core.config import Settings

    assert Settings().playbook_backtest_deep_enabled is False
    # Le code de la passe reste en place : il redeviendra utile avec un historique intraday profond.
    assert "swing" in pbt._LADDERS


def test_swing_ladder_disables_the_pip_ceiling():
    """Le garde-fou en pips est calibré pour une entrée 15 min : à l'échelle journalière il rendait
    la fourchette de stop VIDE.

    Mesuré sur XAU/USD : le stop MINIMUM exigé (0,55 × ATR hebdomadaire ≈ 33 $) dépassait le
    MAXIMUM autorisé (150 pips = 15 $). Aucun stop ne pouvait exister — la passe rendait zéro trade
    sur cinq ans, en silence. Le plafond en ATR, lui, reste actif : c'est celui qui contient
    réellement le drawdown.
    """
    assert pbt._LADDER_SCALE["intraday"] == {}          # la production n'est PAS touchée
    assert pbt._LADDER_SCALE["swing"]["max_stop_pips"] > 1e6
    # ...et le plafond en ATR n'est pas désactivé au passage.
    assert "max_stop_atr_daily" not in pbt._LADDER_SCALE["swing"]


def test_swing_ladder_uses_only_timeframes_that_reach_five_years():
    """La passe longue ne doit reposer sur AUCUNE unité de temps plafonnée à deux ans.

    C'est toute sa raison d'être : Yahoo refuse le 1 h et le 4 h au-delà de 730 jours. Si l'échelle
    swing en réclamait une, la passe « 5 ans » n'en couvrirait que deux — sans le dire.
    """
    deep = set(pbt._LADDERS["swing"].values())
    assert deep <= {"1M", "1w", "1d"}
    assert "1h" not in deep and "4h" not in deep and "15m" not in deep


# ---------------------------------------------------------------------------------------
# 6. Les 10 meilleurs de CHAQUE marché (demandé le 28/07/2026)
# ---------------------------------------------------------------------------------------
def _ranked(symbol: str, expectancy: float, n: int = 12) -> dict:
    """Une ligne de classement telle que `rank_pairs` la produit, avec l'espérance voulue."""
    wins = [_trade(expectancy + 1.0, f"2026-01-{i:02d}") for i in range(1, n + 1)]
    return {"symbol": symbol, **pbt.metrics(wins), "rank": 1,
            "expectancy_r": expectancy, "verdict": ""}


def test_market_tops_ranks_inside_each_market_not_globally():
    """Classer globalement écraserait les marchés calmes sous les marchés volatils.

    On veut lire « les 10 meilleures actions » ET « les 10 meilleures paires forex », pas un
    palmarès unique où un seul marché occupe tout le haut du tableau.
    """
    ranking = [
        _ranked("EUR/USD", 0.30), _ranked("GBP/USD", 0.50), _ranked("USD/JPY", 0.10),
        _ranked("AAPL", 0.90), _ranked("MSFT", 0.20),
        _ranked("XAU/USD", 0.70),
    ]
    tops = pbt.market_tops(ranking, top_n=10)

    assert set(tops) == {"forex", "stock", "commodity"}
    assert [r["symbol"] for r in tops["forex"]["top"]] == ["GBP/USD", "EUR/USD", "USD/JPY"]
    assert [r["symbol"] for r in tops["stock"]["top"]] == ["AAPL", "MSFT"]
    # Le rang est celui du MARCHÉ, pas du classement global.
    assert [r["market_rank"] for r in tops["forex"]["top"]] == [1, 2, 3]
    # Les marchés sont ordonnés par la qualité de leur meilleur instrument.
    assert list(tops) == ["stock", "commodity", "forex"]


def test_market_tops_caps_at_ten_and_counts_the_unmeasured():
    """Le top s'arrête à 10, et le nombre d'instruments non mesurés reste visible.

    Cacher les non-mesurés donnerait à croire que tout le marché a été jugé, alors qu'une partie
    n'avait simplement pas assez de trades.
    """
    ranking = [_ranked(f"SYM{i}", 1.0 - i / 100) for i in range(14)]
    for row in ranking:
        row["symbol"] = f"S{row['symbol']}"          # sans « / » -> classe « stock »
    ranking.append({**_ranked("PLTR", 0.0), "rank": None})   # échantillon insuffisant
    tops = pbt.market_tops(ranking, top_n=10)

    assert len(tops["stock"]["top"]) == 10
    assert tops["stock"]["rated"] == 14
    assert tops["stock"]["unrated"] == 1
    assert "1 sans échantillon exploitable" in tops["stock"]["note"]


def test_market_tops_does_not_dress_up_a_losing_market():
    """« Meilleur du marché » ne veut pas dire « rentable » : le compte des positifs le dit."""
    ranking = [_ranked("EUR/USD", -0.20), _ranked("GBP/USD", -0.50)]
    tops = pbt.market_tops(ranking, top_n=10)
    assert tops["forex"]["profitable"] == 0
    assert tops["forex"]["top"][0]["expectancy_r"] == -0.20


def test_the_conclusion_names_the_best_instruments_of_each_market():
    """Le palmarès est RÉDIGÉ, pas seulement rendu en JSON : la page doit pouvoir le lire."""
    trades = [_trade(3.0, f"2026-01-{i:02d}") for i in range(1, 7)] + \
             [_trade(-1.0, f"2026-02-{i:02d}") for i in range(1, 5)]
    ranking = pbt.rank_pairs({"EUR/USD": pbt.metrics(trades)}, 8)
    scope = {
        "overall": pbt.metrics(trades),
        "ranking": ranking,
        "market_tops": pbt.market_tops(ranking, top_n=10),
        "by_trigger": {}, "by_session": {}, "losers_profile": {},
        "min_trades": 8, "years_covered": 2.0, "pairs_tested": 12,
    }
    conclusion = pbt.write_conclusion(
        {"scope": scope, "fidelity": {"overall": pbt.metrics([]), "years_covered": 0.2}})
    assert conclusion["market_tops"]["forex"]["top"][0]["symbol"] == "EUR/USD"
    assert any(line.startswith("TOP Forex") and "EUR/USD" in line for line in conclusion["lines"])


def test_every_market_offers_enough_instruments_to_rank_ten():
    """Un « top 10 » sur 8 candidats ne classe rien, il recopie la liste.

    Le forex et les actions sont les deux marchés que l'utilisateur veut voir classés : ils doivent
    donc compter au moins dix instruments dans l'univers backtesté.
    """
    assert len(pbt.FOREX_PAIRS) >= 10
    assert len(pbt.STOCKS) >= 10
    for market, symbols in pbt.MARKET_UNIVERSES.items():
        assert symbols, market
        assert len(set(symbols)) == len(symbols), f"doublon dans {market}"
        assert set(symbols) <= set(pbt.CORE_UNIVERSE), f"{market} sort de l'univers backtesté"


# ---------------------------------------------------------------------------------------
# 7. Un seul point de vérité pour les réglages de la stratégie
# ---------------------------------------------------------------------------------------
def test_production_backtest_and_training_share_the_same_settings():
    """Le backtest doit mesurer CE QUI TRADE — donc lire ses réglages au même endroit.

    Régression déjà vécue : `training_service` passait deux mots-clés inexistants, l'AttributeError
    était avalé et le walk-forward nocturne rendait zéro trade en silence. `settings_kwargs` rend
    cette classe de bug impossible : les trois appelants passent le MÊME dictionnaire.
    """
    import inspect

    from app.core.config import get_settings
    from app.domain import playbook

    kwargs = playbook.settings_kwargs(get_settings())
    accepted = set(inspect.signature(playbook.build).parameters)
    unknown = set(kwargs) - accepted
    assert not unknown, f"mots-clés inconnus de playbook.build : {sorted(unknown)}"
    # Les réglages décidés doivent bien y figurer, sinon ils ne s'appliqueraient qu'à la production
    # et le backtest mesurerait une autre stratégie. Plancher d'objectif SUPPRIMÉ le 03/08/2026 :
    # aucune distance en pips ne définit plus le SL ni le TP.
    assert kwargs["min_target_pips"] == 0.0
    assert kwargs["min_target_atr_daily"] == 0.0
    assert kwargs["require_daily"] is False
    assert kwargs["block_stop_width"] is False
    assert kwargs["block_room"] is False
    assert kwargs["block_reach"] is False
    assert kwargs["trend_required_tfs"] == ("4h", "1h")


# ---------------------------------------------------------------------------------------
# 8. Backtest d'UN SEUL instrument (bouton « backtester ce symbole »)
# ---------------------------------------------------------------------------------------
async def test_run_symbol_returns_everything_needed_to_judge(monkeypatch):
    """Un backtest qui ne montre pas ses trades ne se vérifie pas : le journal complet est rendu."""
    trades = [_trade(3.0, f"2026-01-{i:02d}") for i in range(1, 7)] + \
             [_trade(-1.0, f"2026-02-{i:02d}") for i in range(1, 5)]

    async def fake(symbol, **kw):  # noqa: ANN001
        return {"symbol": symbol, "trades": trades, "bars": 5000, "bars_evaluated": 4000,
                "coverage": "2024-01-01 → 2026-01-01", "years": 2.0, "days_evaluated": 730.0,
                "trades_per_day": 0.014, "days_between_trades": 73.0}

    monkeypatch.setattr(pbt, "backtest_symbol", fake)
    out = await pbt.run_symbol("EUR/USD", entry_tf="1h", step=4)

    assert out["symbol"] == "EUR/USD" and out["market"] == "forex"
    assert out["market_label"] == "Forex"
    assert out["overall"]["trades"] == 10 and out["overall"]["wins"] == 6
    assert len(out["trades"]) == 10          # le journal complet, pas un résumé
    assert "exploitable" in out["verdict"]
    # Les ventilations qui permettent de dire OÙ la stratégie marche sur cet instrument.
    for key in ("by_trigger", "by_session", "by_direction", "by_outcome", "losers_profile"):
        assert key in out


async def test_run_symbol_says_when_there_is_nothing_to_measure(monkeypatch):
    """Zéro trade et données absentes sont deux choses différentes : le verdict les distingue."""
    async def no_trades(symbol, **kw):  # noqa: ANN001
        return {"symbol": symbol, "trades": [], "years": 2.0, "days_evaluated": 730.0}

    async def no_data(symbol, **kw):  # noqa: ANN001
        return {"symbol": symbol, "trades": [], "error": "historique insuffisant (12 bougies 1h)"}

    monkeypatch.setattr(pbt, "backtest_symbol", no_trades)
    empty = await pbt.run_symbol("EUR/USD")
    assert "aucun trade conforme" in empty["verdict"]
    assert "pas une panne" in empty["verdict"]

    monkeypatch.setattr(pbt, "backtest_symbol", no_data)
    broken = await pbt.run_symbol("EUR/USD")
    assert broken["verdict"].startswith("non mesurable")
    assert "historique insuffisant" in broken["verdict"]


async def test_symbol_run_state_is_released_even_when_the_backtest_raises(monkeypatch):
    """Un symbole qui plante ne doit pas rester « en cours » et bloquer toute relance."""
    async def boom(symbol, **kw):  # noqa: ANN001
        raise RuntimeError("connecteur indisponible")

    monkeypatch.setattr(pbt, "backtest_symbol", boom)
    try:
        await pbt.run_symbol_job(None, "EUR/USD", entry_tf="1h")
    except RuntimeError:
        pass
    assert pbt.symbol_run_state("EUR/USD", "1h")["running"] is False


async def test_progress_advances_symbol_by_symbol(monkeypatch):
    """L'avancement doit AVANCER — un compteur figé à 0 est indiscernable d'un plantage.

    `_set_phase` n'était appelé qu'une fois par passe, avec `done=0`, et jamais réactualisé : la
    page affichait « 0/88 » du début à la fin d'une passe de plusieurs minutes.
    """
    vus: list[int] = []

    async def _un_symbole(symbol, **kw):  # noqa: ANN001
        vus.append(pbt.run_state()["done"])
        return {"symbol": symbol, "trades": [], "coverage": "", "years": 0.0}

    monkeypatch.setattr(pbt, "backtest_symbol", _un_symbole)
    await pbt.run_pass(["EUR/USD", "GBP/USD", "USD/JPY"], entry_tf="1h", step=4, parallel=1)

    assert pbt.run_state()["done"] == 3, "chaque symbole terminé doit faire avancer le compteur"
    assert vus == [0, 1, 2], "l'avancement doit progresser pendant la passe, pas à la fin"


async def test_progress_advances_even_when_a_symbol_fails(monkeypatch):
    """Un fournisseur KO ne doit pas bloquer la barre de progression au premier symbole."""
    async def _plante(symbol, **kw):  # noqa: ANN001
        raise RuntimeError("connecteur indisponible")

    monkeypatch.setattr(pbt, "backtest_symbol", _plante)
    await pbt.run_pass(["EUR/USD", "GBP/USD"], entry_tf="1h", step=4, parallel=1)

    assert pbt.run_state()["done"] == 2, "un symbole en échec compte aussi dans l'avancement"


def test_api_exposes_the_backtestable_markets():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": "mkt@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    body = client.get("/api/backtest/playbook/markets", headers=h).json()
    by_market = {m["market"]: m for m in body["markets"]}
    assert by_market["forex"]["count"] >= 10
    assert by_market["stock"]["count"] >= 10
    assert {t["tf"] for t in body["entry_timeframes"]} == {"1h", "15m"}

    # Un symbole jamais backtesté le DIT, il ne rend pas une page vide.
    empty = client.get("/api/backtest/playbook/symbol?symbol=EUR/USD", headers=h).json()
    assert empty["available"] is False and "pas encore été backtesté" in empty["note"]
