"""PLAN D'IMPLÉMENTATION (globale/03) — les briques mesurables des Phases 1, 2 et 3.

Ce que ces tests garantissent, dans l'ordre du plan :

- **1.3 Secrets** : l'API refuse de DÉMARRER en production avec le secret par défaut.
- **2.1 Verdict par paire** : 🟢 exige espérance ≥ +0,4 R, n ≥ 20 ET deux passages hebdomadaires
  consécutifs ; 🔴 exige un échantillon suffisant pour condamner ; le reste est 🟡 — et une paire
  absente d'un passage perd son vert (pas d'auto-trade sur une mesure périmée).
- **2.2 Gating** : seules les paires 🟢 sont auto-tradées ; chaque refus est journalisé avec les
  niveaux du trade (le rituel hebdo pourra dire si les gates ont eu raison).
- **2.3 Matrice paire × déclencheur** : un déclencheur mesuré < +0,4 R sur n ≥ 15 pour UNE paire
  y est désactivé, sans être condamné ailleurs.
- **2.4 A/B volatilité** : la variante « stop k×ATR 4 h borné » produit bien un stop différent,
  dans les bornes de la bande de R/R.
- **2.6 Sécurisation +2R on/off** : le comparatif se calcule sur les MÊMES trades.
- **3.1 Sizing par conviction** : ×1,25 sur 🟢 solide, ×0,5 sur 🟡, plafond absolu.
- **3.2 Corrélation** : jamais plus de N positions partageant une même devise.
- **3.3 Gel des entrées** : journée à −3 % (ou semaine à −6 %) -> plus aucune nouvelle entrée.
- **3.4 Journal enrichi** : chaque ordre ouvert emporte verdict, déclencheur, session, ATR %.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import enforce_prod_secrets, get_settings
from app.core.security import hash_password
from app.repositories.store import get_store
from app.services import execution_service, risk_service, verdict_service


def _user(store, email: str, capital: float = 10_000.0):
    tenant = store.tenants.create(name=email)
    user = store.users.create(
        tenant_id=tenant.id, email=email, password_hash=hash_password("password123"),
        full_name="Plan",
    )
    user.capital = capital
    store.users.update(user)
    return user


def _backtest_payload(date: str, by_symbol: dict, by_pair_trigger: dict | None = None) -> dict:
    return {"date": date, "scope": {"by_symbol": by_symbol,
                                    "by_pair_trigger": by_pair_trigger or {}}}


def _m(expectancy: float, trades: int, win: float = 55.0, pf: float = 2.0) -> dict:
    return {"expectancy_r": expectancy, "trades": trades, "win_rate": win, "profit_factor": pf}


# ---------------------------------------------------------------------------------------
# 1.3 — Secrets : refus de boot en production avec le secret par défaut
# ---------------------------------------------------------------------------------------
def test_default_secret_key_refuses_to_boot_in_production():
    s = get_settings()
    env, key = s.environment, s.secret_key
    try:
        s.environment, s.secret_key = "production", "change-me"
        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            enforce_prod_secrets(s)
        # Un secret trop court est tout aussi forgeable qu'un secret par défaut.
        s.secret_key = "abc"
        with pytest.raises(RuntimeError):
            enforce_prod_secrets(s)
        # Un vrai secret passe.
        s.secret_key = "a" * 64
        enforce_prod_secrets(s)
        # En dev, le défaut reste toléré (avertissement au boot, pas de refus).
        s.environment, s.secret_key = "dev", "change-me"
        enforce_prod_secrets(s)
    finally:
        s.environment, s.secret_key = env, key


# ---------------------------------------------------------------------------------------
# 2.1 — Verdict par paire : deux passages consécutifs avant le vert
# ---------------------------------------------------------------------------------------
def test_green_requires_two_consecutive_weekly_passes():
    store = get_store()
    good = _m(0.9, 25)
    first = verdict_service.update_from_backtest(store, _backtest_payload("2026-07-19", {"USD/CHF": good}))
    assert first["pairs"]["USD/CHF"]["status"] == "yellow", "1er passage vert = 🟡, pas 🟢"
    assert first["pairs"]["USD/CHF"]["green_streak"] == 1

    second = verdict_service.update_from_backtest(store, _backtest_payload("2026-07-26", {"USD/CHF": good}))
    assert second["pairs"]["USD/CHF"]["status"] == "green"
    assert second["pairs"]["USD/CHF"]["green_streak"] == 2


def test_rerunning_the_same_day_does_not_fabricate_a_streak():
    """Relancer deux fois le backtest le même jour ne compte qu'UN passage."""
    store = get_store()
    good = _m(0.9, 25)
    verdict_service.update_from_backtest(store, _backtest_payload("2026-07-26", {"USD/CHF": good}))
    again = verdict_service.update_from_backtest(store, _backtest_payload("2026-07-26", {"USD/CHF": good}))
    assert again["pairs"]["USD/CHF"]["status"] == "yellow"
    assert again["pairs"]["USD/CHF"]["green_streak"] == 1


def test_red_needs_enough_trades_to_condemn_and_yellow_covers_the_rest():
    store = get_store()
    rec = verdict_service.update_from_backtest(store, _backtest_payload("2026-07-26", {
        "USD/CAD": _m(-0.4, 12),    # perd avec échantillon suffisant -> 🔴
        "EUR/GBP": _m(-0.8, 3),     # perd mais 3 trades : on ne sait pas -> 🟡
        "GBP/USD": _m(0.2, 40),     # positif mais sous le seuil -> 🟡
    }))
    assert rec["pairs"]["USD/CAD"]["status"] == "red"
    assert rec["pairs"]["EUR/GBP"]["status"] == "yellow"
    assert rec["pairs"]["GBP/USD"]["status"] == "yellow"


def test_a_pair_missing_from_the_last_pass_loses_its_green():
    """Pas d'auto-trade sur une mesure qu'on n'a pas pu refaire (données en échec)."""
    store = get_store()
    good = _m(0.9, 25)
    verdict_service.update_from_backtest(store, _backtest_payload("2026-07-12", {"USD/CHF": good}))
    verdict_service.update_from_backtest(store, _backtest_payload("2026-07-19", {"USD/CHF": good}))
    assert verdict_service.verdict_for(store, "USD/CHF")["status"] == "green"

    rec = verdict_service.update_from_backtest(store, _backtest_payload("2026-07-26", {"EUR/USD": _m(0.1, 10)}))
    row = rec["pairs"]["USD/CHF"]
    assert row["status"] == "yellow" and row["green_streak"] == 0
    assert "absente" in row["reason"]


# ---------------------------------------------------------------------------------------
# 2.3 — Matrice paire × déclencheur
# ---------------------------------------------------------------------------------------
def test_a_losing_trigger_is_disabled_per_pair_not_everywhere():
    store = get_store()
    s = get_settings()
    s.playbook_trigger_matrix_gating = True
    try:
        verdict_service.update_from_backtest(store, _backtest_payload(
            "2026-07-26", {"EUR/USD": _m(0.5, 30)},
            by_pair_trigger={
                "EUR/USD|repli": _m(0.1, 18),     # mesuré sous +0,4 R sur n ≥ 15 -> désactivé
                "EUR/USD|cassure": _m(1.1, 16),   # au-dessus du seuil -> conservé
                "GBP/JPY|repli": _m(0.1, 5),      # échantillon trop court -> on ne conclut pas
            },
        ))
        assert verdict_service.trigger_disabled(store, "EUR/USD", "repli") is not None
        assert verdict_service.trigger_disabled(store, "EUR/USD", "cassure") is None
        assert verdict_service.trigger_disabled(store, "GBP/JPY", "repli") is None
    finally:
        s.playbook_trigger_matrix_gating = True


# ---------------------------------------------------------------------------------------
# 2.2 — Gating de l'auto-entrée + journal des trades évités
# ---------------------------------------------------------------------------------------
def test_auto_entry_gating_only_trades_green_pairs_and_logs_refusals():
    store = get_store()
    s = get_settings()
    s.playbook_pair_gating = True
    try:
        good = _m(0.9, 25)
        verdict_service.update_from_backtest(store, _backtest_payload("2026-07-19", {"USD/CHF": good, "USD/CAD": _m(-0.4, 12)}))
        verdict_service.update_from_backtest(store, _backtest_payload("2026-07-26", {"USD/CHF": good, "USD/CAD": _m(-0.4, 12)}))

        ready = [
            {"symbol": "USD/CHF", "direction": "BUY", "entry": 0.88, "stop_loss": 0.873,
             "take_profit_1": 0.90, "trigger": "cassure — swing haut"},
            {"symbol": "USD/CAD", "direction": "SELL", "entry": 1.36, "stop_loss": 1.367,
             "take_profit_1": 1.34, "trigger": "repli — MA20"},
            {"symbol": "XAU/USD", "direction": "BUY", "entry": 3300.0, "stop_loss": 3270.0,
             "take_profit_1": 3400.0, "trigger": "cassure — swing haut"},  # jamais notée
        ]
        allowed, refused = verdict_service.filter_auto_ready(store, ready)
        assert [p["symbol"] for p in allowed] == ["USD/CHF"]
        assert {r["symbol"] for r in refused} == {"USD/CAD", "XAU/USD"}
        # Chaque refus est journalisé AVEC ses niveaux : rejouable au rituel hebdomadaire.
        logged = verdict_service.recent_refusals(store)
        assert {r["symbol"] for r in logged} == {"USD/CAD", "XAU/USD"}
        assert all(r["entry"] and r["stop_loss"] and r["reason"] for r in logged)
    finally:
        s.playbook_pair_gating = False


def test_gating_refuses_a_green_pair_on_a_disabled_trigger():
    store = get_store()
    s = get_settings()
    s.playbook_pair_gating = True
    try:
        good = _m(0.9, 25)
        matrix = {"USD/CHF|repli": _m(0.0, 20)}
        verdict_service.update_from_backtest(store, _backtest_payload("2026-07-19", {"USD/CHF": good}, matrix))
        verdict_service.update_from_backtest(store, _backtest_payload("2026-07-26", {"USD/CHF": good}, matrix))

        pick = {"symbol": "USD/CHF", "direction": "BUY", "entry": 0.88, "stop_loss": 0.873,
                "take_profit_1": 0.90, "trigger": "repli — MA20 + reprise"}
        allowed, refused = verdict_service.filter_auto_ready(store, [pick])
        assert allowed == [] and "repli" in refused[0]["reason"]
    finally:
        s.playbook_pair_gating = False


# ---------------------------------------------------------------------------------------
# 2.4 — Variante « stop k×ATR 4 h borné » (A/B volatilité)
# ---------------------------------------------------------------------------------------
def test_atr4h_stop_mode_moves_the_stop_within_the_rr_band():
    from tests.test_playbook import _build

    reference = _build()
    assert reference.ready, "le scénario canonique doit produire un setup prêt"
    variant = _build(stop_mode="atr4h")
    assert variant.ready
    assert "ATR 4 h" in variant.stop_basis
    # La borne de la bande de R/R reste respectée : l'objectif de 200 pips reste logeable.
    assert variant.risk_pips <= get_settings().playbook_max_stop_pips


def test_volatility_ab_variants_are_the_three_planned_answers():
    from app.backtest import playbook_backtest as pbt

    assert set(pbt.VOLATILITY_VARIANTS) == {"adapt", "refuse", "stop_atr4h"}


async def test_backtest_symbol_accepts_overriding_a_base_kwarg(monkeypatch):
    """Régression : surcharger `volatility_filter` (déjà présent dans les kwargs de base) ne doit
    pas lever « got multiple values » — c'est exactement ce que fait chaque variante de l'A/B."""
    from app.backtest import playbook_backtest as pbt
    from app.domain import playbook
    from app.domain.indicators import Candle

    def series(n: float) -> list[Candle]:
        return [Candle(1.0, 1.01, 0.99, 1.0, 10.0) for _ in range(int(n))]

    lengths = {"1h": 900, "15m": 900, "4h": 400, "1d": 300, "1M": 60}

    async def _fake_series(symbol, interval, limit, **kw):  # noqa: ANN001
        step = pbt._INTERVAL_SECONDS.get(interval, 3600)
        end = datetime.now(UTC)
        out = series(lengths.get(interval, 300))
        return [Candle(c.open, c.high, c.low, c.close, c.volume,
                       timestamp=end - timedelta(seconds=step * (len(out) - 1 - i)))
                for i, c in enumerate(out)]

    captured: dict = {}

    def _stub_build(symbol, monthly, daily, h4, m15, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return playbook.PlaybookSetup(symbol=symbol)   # jamais prêt : aucun trade rejoué

    monkeypatch.setattr(pbt, "_series", _fake_series)
    monkeypatch.setattr(playbook, "build", _stub_build)

    res = await pbt.backtest_symbol(
        "EUR/USD", entry_tf="1h", step=200,
        overrides={"volatility_filter": False, "stop_mode": "atr4h"},
    )
    assert res.get("error") is None, f"la surcharge ne doit pas faire échouer la paire : {res.get('error')}"
    assert captured["volatility_filter"] is False and captured["stop_mode"] == "atr4h"


# ---------------------------------------------------------------------------------------
# 2.6 — Comparatif sécurisation +2R on/off (mêmes trades, deux rejeux)
# ---------------------------------------------------------------------------------------
def test_secure_ab_compares_the_same_trades():
    from app.backtest import playbook_backtest as pbt

    trades = [
        {"r": 2.0, "r_no_secure": 3.0},   # sécurisé à +2R alors que le TP aurait payé 3R
        {"r": 2.0, "r_no_secure": -1.0},  # sécurisé à +2R alors que le trade serait reparti au stop
        {"r": -1.0, "r_no_secure": -1.0},  # perdant dans les deux mondes
    ]
    ab = pbt.secure_ab(trades)
    assert ab["trades"] == 3 and ab["changed_by_rule"] == 2
    assert abs(ab["expectancy_with_secure_r"] - 1.0) < 1e-3
    assert abs(ab["expectancy_without_secure_r"] - (1.0 / 3)) < 1e-3
    assert abs(ab["delta_r"] - (2.0 / 3)) < 1e-3


def test_replay_trade_without_securing_lets_the_trade_return_to_its_stop():
    """`secure_at_r=inf` désactive réellement la règle : le stop initial reste le seul stop."""
    from app.backtest.playbook_backtest import replay_trade
    from app.domain.indicators import Candle

    def c(low: float, high: float) -> Candle:
        return Candle(open=(low + high) / 2, high=high, low=low, close=(low + high) / 2, volume=1.0)

    # Entrée 100, stop 99 (risque 1), objectif 103. Le prix monte à +2,5R puis retombe sous 99.
    bars = [c(99.5, 100.5), c(100.0, 102.5), c(98.5, 101.0)]
    secured = replay_trade(bars, 0, "BUY", 100.0, 99.0, 103.0, max_hold=10, secure_at_r=2.0)
    plain = replay_trade(bars, 0, "BUY", 100.0, 99.0, 103.0, max_hold=10, secure_at_r=float("inf"))
    assert secured["r"] == 2.0 and secured["secured"] is True
    assert plain["r"] == -1.0 and plain["secured"] is False


# ---------------------------------------------------------------------------------------
# 3.1 — Sizing par conviction (via l'exécution playbook complète)
# ---------------------------------------------------------------------------------------
@pytest.fixture
def flat_market(monkeypatch):
    """Prix de marché déterministe (1,1000) pour dimensionner sans réseau."""
    from app.data import markets
    from app.domain.indicators import Candle

    candles = [Candle(1.1, 1.1, 1.1, 1.1, 10.0) for _ in range(60)]

    async def _load(symbol, interval="1h", limit=200, **kw):  # noqa: ANN001
        return candles

    monkeypatch.setattr(markets, "load_candles", _load)
    monkeypatch.setattr(markets, "is_real", lambda symbol: True)
    return candles


def _pick(symbol: str = "USD/CHF") -> dict:
    return {"symbol": symbol, "direction": "BUY", "tier": "ready",
            "entry": 1.1, "stop_loss": 1.09, "take_profit_1": 1.13,
            "trigger": "cassure — swing haut",
            "layers": {"daily": {"metrics": {"atr_pct": 1.12}}}}


async def test_conviction_sizing_multiplies_green_and_halves_yellow(flat_market):
    store = get_store()
    s = get_settings()
    s.conviction_sizing_enabled = True
    try:
        good = _m(0.9, 35)   # 🟢 avec n ≥ 30 -> ×1,25
        verdict_service.update_from_backtest(store, _backtest_payload("2026-07-19", {"USD/CHF": good, "GBP/USD": _m(0.2, 40)}))
        verdict_service.update_from_backtest(store, _backtest_payload("2026-07-26", {"USD/CHF": good, "GBP/USD": _m(0.2, 40)}))

        user = _user(store, "sizing@test.com", capital=10_000.0)   # modéré : 1 % de base
        report = await execution_service.execute_playbook_trades(
            store, user.tenant_id, count=2, picks=[_pick("USD/CHF"), _pick("GBP/USD")],
        )
        by_symbol = {o["symbol"]: o for o in report["opened"]}
        assert set(by_symbol) == {"USD/CHF", "GBP/USD"}
        # Risque au stop = capital × 1 % × multiplicateur ; distance au stop = 0,01.
        green_risk = by_symbol["USD/CHF"]["qty"] * 0.01
        yellow_risk = by_symbol["GBP/USD"]["qty"] * 0.01
        assert abs(green_risk - 10_000 * 0.0125) < 1.0, "paire 🟢 solide : 1 % × 1,25"
        assert abs(yellow_risk - 10_000 * 0.005) < 1.0, "paire 🟡 : 1 % × 0,5"
        assert by_symbol["USD/CHF"]["conviction_mult"] == 1.25
        assert by_symbol["GBP/USD"]["conviction_mult"] == 0.5
    finally:
        s.conviction_sizing_enabled = False


async def test_conviction_sizing_is_capped_in_absolute(flat_market):
    """Profil agressif (2 %) × 1,25 serait 2,5 % : le plafond absolu (1,5 %) s'impose."""
    store = get_store()
    s = get_settings()
    s.conviction_sizing_enabled = True
    try:
        good = _m(0.9, 35)
        verdict_service.update_from_backtest(store, _backtest_payload("2026-07-19", {"USD/CHF": good}))
        verdict_service.update_from_backtest(store, _backtest_payload("2026-07-26", {"USD/CHF": good}))
        user = _user(store, "cap@test.com", capital=10_000.0)
        user.risk_profile = "aggressive"
        store.users.update(user)
        report = await execution_service.execute_playbook_trades(
            store, user.tenant_id, count=1, picks=[_pick("USD/CHF")],
        )
        risk = report["opened"][0]["qty"] * 0.01
        assert abs(risk - 10_000 * 0.015) < 1.0, "2 % × 1,25 = 2,5 % -> plafonné à 1,5 %"
    finally:
        s.conviction_sizing_enabled = False


# ---------------------------------------------------------------------------------------
# 3.2 — Garde de corrélation : max 2 positions partageant une devise
# ---------------------------------------------------------------------------------------
async def test_correlation_guard_blocks_a_third_euro_position(flat_market):
    store = get_store()
    s = get_settings()
    s.correlation_guard_enabled = True
    try:
        user = _user(store, "corr@test.com")
        for i, sym in enumerate(("EUR/USD", "EUR/JPY")):
            store.records.put("order", f"open-{i}", {
                "mode": "paper", "symbol": sym, "side": "buy", "outcome": "open",
            }, tenant_id=user.tenant_id)
        report = await execution_service.execute_playbook_trades(
            store, user.tenant_id, count=1, picks=[_pick("EUR/GBP")],
        )
        assert report["opened"] == []
        assert "EUR" in report["skipped"][0]["reason"]
        # Une paire SANS devise partagée passe, elle.
        ok = await execution_service.execute_playbook_trades(
            store, user.tenant_id, count=1, picks=[_pick("AUD/CHF")],
        )
        assert len(ok["opened"]) == 1
    finally:
        s.correlation_guard_enabled = False


async def test_correlation_guard_does_not_apply_to_crypto(flat_market):
    """La garde ne vaut QUE pour le change : USDT est une unité de compte, pas un pari.

    Elle découpait tout symbole contenant un « / », donc aussi les paires crypto : BTC/USDT et
    ETH/USDT comptaient comme « deux positions exposées à USDT » et la troisième paire crypto —
    quelle qu'elle soit — était refusée. Le portefeuille crypto se trouvait ainsi plafonné à deux
    positions, tous actifs confondus, pour une corrélation qui n'existe pas par la devise de
    cotation.
    """
    store = get_store()
    s = get_settings()
    s.correlation_guard_enabled = True
    try:
        user = _user(store, "corr-crypto@test.com")
        for i, sym in enumerate(("BTC/USDT", "ETH/USDT")):
            store.records.put("order", f"crypto-{i}", {
                "mode": "paper", "symbol": sym, "side": "buy", "outcome": "open",
            }, tenant_id=user.tenant_id)
        report = await execution_service.execute_playbook_trades(
            store, user.tenant_id, count=1, picks=[_pick("SUI/USDT")],
        )
        assert len(report["opened"]) == 1, (
            "une 3e paire crypto ne doit pas être refusée pour « exposition à USDT »"
        )
    finally:
        s.correlation_guard_enabled = False


def test_currencies_are_read_only_for_forex_pairs():
    """`_currencies` ne découpe que les paires de CHANGE — c'est ce qui borne la garde."""
    assert execution_service._currencies("EUR/USD") == {"EUR", "USD"}   # noqa: SLF001
    assert execution_service._currencies("BTC/USDT") == set()           # noqa: SLF001
    assert execution_service._currencies("SUI/USDT") == set()           # noqa: SLF001
    assert execution_service._currencies("AAPL") == set()               # noqa: SLF001


# ---------------------------------------------------------------------------------------
# 3.3 — Gel des entrées : journée à −3 %, semaine à −6 %
# ---------------------------------------------------------------------------------------
def _closed_loss(store, tenant_id: str, pnl: float, *, days_ago: int = 0, key: str = "loss"):
    closed = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    store.records.put("order", f"{key}-{days_ago}-{pnl}", {
        "mode": "paper", "symbol": "EUR/USD", "side": "buy", "outcome": "lost",
        "realized_pnl": pnl, "closed_at": closed,
    }, tenant_id=tenant_id)


async def test_a_minus_three_percent_day_freezes_new_entries(flat_market):
    store = get_store()
    s = get_settings()
    s.loss_freeze_enabled = True
    try:
        user = _user(store, "freeze@test.com", capital=10_000.0)
        _closed_loss(store, user.tenant_id, -150.0, key="a")
        assert risk_service.entries_frozen(store, user.tenant_id) == (False, None)

        _closed_loss(store, user.tenant_id, -160.0, key="b")   # total −310 = −3,1 %
        frozen, reason = risk_service.entries_frozen(store, user.tenant_id)
        assert frozen and "QUOTIDIEN" in reason

        report = await execution_service.execute_playbook_trades(
            store, user.tenant_id, count=1, picks=[_pick("USD/CHF")],
        )
        assert report["opened"] == [] and report.get("frozen") is True
        assert "GEL" in report["summary"]
        # La notification part UNE fois, pas à chaque passage.
        assert await risk_service.notify_freezes(store) >= 1
        assert await risk_service.notify_freezes(store) == 0
    finally:
        s.loss_freeze_enabled = False


def test_a_minus_six_percent_week_freezes_even_without_a_bad_day():
    store = get_store()
    s = get_settings()
    s.loss_freeze_enabled = True
    try:
        user = _user(store, "wfreeze@test.com", capital=10_000.0)
        # Pertes réparties sur la semaine : aucune journée ne franchit −3 % à elle seule.
        weekday = datetime.now(UTC).weekday()
        spread = [d for d in (1, 2, 3) if d <= weekday]
        if not spread:   # lundi : tout le budget hebdo tient dans la seule journée écoulée...
            pytest.skip("un lundi, gel hebdo et gel quotidien se confondent — scénario testé les autres jours")
        per_day = -620.0 / len(spread)
        for d in spread:
            _closed_loss(store, user.tenant_id, per_day, days_ago=d, key="w")
        frozen, reason = risk_service.entries_frozen(store, user.tenant_id)
        assert frozen and "HEBDOMADAIRE" in reason
    finally:
        s.loss_freeze_enabled = False


# ---------------------------------------------------------------------------------------
# 3.4 — Journal enrichi : le contexte du trade est persisté sur l'ordre
# ---------------------------------------------------------------------------------------
async def test_opened_orders_carry_their_context(flat_market):
    store = get_store()
    user = _user(store, "journal@test.com")
    report = await execution_service.execute_playbook_trades(
        store, user.tenant_id, count=1, picks=[_pick("USD/CHF")],
    )
    assert len(report["opened"]) == 1
    order = execution_service.list_orders(store, user.tenant_id)[0]
    assert order["trigger_type"] == "cassure"
    assert order["atr_pct"] == 1.12
    assert order["session_window"], "la fenêtre de session doit être enregistrée"
    assert order["pair_verdict"] is not None
    assert order["risk_pct"] > 0 and order["conviction_mult"] > 0


async def test_opened_orders_carry_their_factor_votes(flat_market):
    """Chaque ordre playbook emporte AUSSI ce que chaque facteur a voté à l'ouverture.

    C'est la matière première qui permet, à la clôture, d'attribuer le résultat RÉEL du trade aux
    mêmes agents que l'entraînement nocturne (cf. `domain.factor_attribution`). Sans ce champ, le
    Journal ne peut apprendre que du flux « Analyser ce symbole » — jamais de l'auto-entrée.
    """
    pick = _pick("USD/CHF")
    pick["layers"] = {
        "daily": {"metrics": {"atr_pct": 1.12},
                  "factors": [{"key": "ma", "score": 0.5}, {"key": "rsi", "score": 0.3}]},
        "h4": {"factors": [{"key": "ma", "score": 0.4}]},
        "m15": {"factors": [{"key": "structure", "score": -0.2}]},
    }
    store = get_store()
    user = _user(store, "factor-votes@test.com")
    report = await execution_service.execute_playbook_trades(
        store, user.tenant_id, count=1, picks=[pick],
    )
    assert len(report["opened"]) == 1
    order = execution_service.list_orders(store, user.tenant_id)[0]
    assert order["factor_votes"] == {"ma": 2, "rsi": 1, "structure": -1}


# ---------------------------------------------------------------------------------------
# 3.5 — L'expérience VÉCUE (trades playbook clôturés) nourrit le Journal, pas seulement le
# flux « Analyser ce symbole ». Demandé le 02/08/2026 : « je veux que les agents s'entraînent
# aussi avec les positions clôturées dans le journal, plus l'historique ».
# ---------------------------------------------------------------------------------------
async def test_a_single_closed_trade_is_not_enough_to_move_a_multiplier(flat_market):
    """Un seul trade clôturé ne doit RIEN faire bouger — c'est le garde-fou contre le sur-ajustement.

    `compute_weight_multipliers` exige au moins 3 échantillons par agent avant de s'écarter de la
    neutralité (1.0) : sur-réagir à un trade unique, gagnant ou perdant, produirait un poids
    d'agent qui ne représente qu'un coup de chance ou de malchance.
    """
    from app.services import journal_service

    pick = _pick("USD/CHF")
    pick["layers"] = {"daily": {"factors": [{"key": "ma", "score": 0.5}]}}
    store = get_store()
    user = _user(store, "closed-loop-single@test.com")
    report = await execution_service.execute_playbook_trades(
        store, user.tenant_id, count=1, picks=[pick],
    )
    order = execution_service.list_orders(store, user.tenant_id)[0]

    store.records.put(execution_service.ORDER, order["id"], {
        **order, "outcome": "won", "realized_pnl": 200.0,
    }, tenant_id=user.tenant_id)

    rows = journal_service.playbook_entries(store, user.tenant_id)
    closed = next(r for r in rows if r["id"] == order["id"])
    assert closed["agent_scores"]["technical"] > 0, "le score est bien calculé…"

    after = journal_service.compute_multipliers(store, user.tenant_id)
    assert after.get("technical", 1.0) == 1.0, "…mais 1 seul échantillon reste neutre (n < 3)"


async def test_accumulated_closed_trades_do_move_the_multiplier(flat_market):
    """LA RÉPONSE À LA QUESTION POSÉE : l'ACCUMULATION de trades clôturés aide-t-elle réellement ?

    Oui, une fois le seuil de confiance franchi (n ≥ 3 échantillons pour cet agent) : trois trades
    playbook gagnés d'affilée, dont les facteurs « technical » avaient tous voté dans le bon sens,
    déplacent le multiplicateur de cet agent au-dessus de la neutralité — sans qu'aucun signal du
    flux « Analyser ce symbole » n'existe. C'est exactement le trou que cette fonctionnalité comble :
    avant, ces trades ne faisaient progresser AUCUN agent, quel que soit leur nombre.
    """
    from app.services import journal_service

    store = get_store()
    user = _user(store, "closed-loop-accum@test.com")
    # Symboles distincts : le garde anti-doublon refuserait un 2e ACHAT sur la même paire tant que
    # le premier reste visible comme « récent » — hors de propos ici, on teste l'ACCUMULATION par
    # agent, pas ce garde-fou (déjà couvert ailleurs).
    for symbol in ("USD/CHF", "AUD/CHF", "NZD/CHF"):
        pick = _pick(symbol)
        pick["layers"] = {"daily": {"factors": [{"key": "ma", "score": 0.5}]},
                          "h4": {"factors": [{"key": "rsi", "score": 0.3}]}}
        report = await execution_service.execute_playbook_trades(
            store, user.tenant_id, count=1, picks=[pick],
        )
        assert report["opened"], f"{symbol} : ouverture attendue ({report['skipped']})"
        order = report["opened"][0]
        store.records.put(execution_service.ORDER, order["order_id"], {
            **store.records.get(execution_service.ORDER, order["order_id"]),
            "outcome": "won", "realized_pnl": 100.0,
        }, tenant_id=user.tenant_id)

    after = journal_service.compute_multipliers(store, user.tenant_id)
    assert after["technical"] > 1.0, "3 gains accumulés doivent renforcer l'agent technique"


async def test_a_manual_order_never_fabricates_a_rationale(flat_market):
    """Un achat/vente MANUEL (bouton « Acheter »/« Vendre ») n'a aucune rationale stratégique : il
    ne doit jamais se voir attribuer un score d'agent inventé, gagné ou perdu."""
    from app.services import journal_service

    store = get_store()
    user = _user(store, "manual-order@test.com")
    conn_id = execution_service.ensure_paper_connection(store, user.tenant_id)
    # `flat_market` sert un prix constant de 1.1 sur tous les symboles : les niveaux doivent
    # encadrer CE prix, pas un prix arbitraire (GOOGL n'a rien de spécial dans ce fixture).
    order = await execution_service.place_order(
        store, user.tenant_id, conn_id=conn_id, symbol="GOOGL", side="buy", qty=1.0,
        stop_loss=1.05, take_profit=1.2,
    )
    store.records.put(execution_service.ORDER, order["id"], {
        **order, "outcome": "won", "realized_pnl": 50.0,
    }, tenant_id=user.tenant_id)

    rows = journal_service.playbook_entries(store, user.tenant_id)
    assert rows[0]["agent_scores"] == {}, "aucune rationale à attribuer sur un ordre manuel"


async def test_insights_endpoint_reflects_playbook_learning(flat_market):
    """Bout en bout par l'API que la page Journal appelle réellement."""
    from fastapi.testclient import TestClient

    from app.core.security import hash_password
    from app.main import app

    pick = _pick("USD/CHF")
    pick["layers"] = {
        "daily": {"factors": [{"key": "vwap", "score": 0.5}]},
        "h4": {"factors": [{"key": "vwap", "score": 0.4}]},
        "m15": {"factors": [{"key": "volume", "score": 0.3}]},
    }
    store = get_store()
    tenant = store.tenants.create(name="insights@test.com")
    user = store.users.create(
        tenant_id=tenant.id, email="insights@test.com",
        password_hash=hash_password("password123"), full_name="Insights",
    )
    await execution_service.execute_playbook_trades(
        store, user.tenant_id, count=1, picks=[pick],
    )
    order = execution_service.list_orders(store, user.tenant_id)[0]
    store.records.put(execution_service.ORDER, order["id"], {
        **order, "outcome": "won", "realized_pnl": 120.0,
    }, tenant_id=user.tenant_id)

    client = TestClient(app)
    r = client.post("/api/auth/login", json={"email": "insights@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = client.post("/api/billing/checkout/pro", headers=h)
    assert r.status_code == 200, r.text

    ins = client.get("/api/journal/insights", headers=h).json()
    assert ins["reliability_source"] == "live"
    assert ins["trades_learned"] >= 1
    volume_row = next((row for row in ins["reliability"] if row["agent"] == "volume"), None)
    assert volume_row is not None, "l'agent volume doit apparaître, nourri par ce trade playbook"


# ---------------------------------------------------------------------------------------
# API — les verdicts sont exposés à l'interface (2.7 côté backend)
# ---------------------------------------------------------------------------------------
def test_api_exposes_pair_verdicts():
    from fastapi.testclient import TestClient

    from app.main import app

    store = get_store()
    good = _m(0.9, 25)
    verdict_service.update_from_backtest(store, _backtest_payload("2026-07-19", {"USD/CHF": good}))
    verdict_service.update_from_backtest(store, _backtest_payload("2026-07-26", {"USD/CHF": good}))

    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": "verdicts@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    body = client.get("/api/backtest/playbook/verdicts", headers=h).json()
    assert body["available"] is True
    assert body["pairs"]["USD/CHF"]["status"] == "green"
    assert "criteria" in body and "refusals" in body
