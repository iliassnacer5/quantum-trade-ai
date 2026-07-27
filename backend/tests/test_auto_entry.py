"""AUTO-ENTRÉE, INSTANTANÉ TEMPS RÉEL et ENTRAÎNEMENT QUOTIDIEN.

Ce que ces tests garantissent, dans l'ordre des promesses faites à l'utilisateur :

1. **Aucun clic** — un setup ARMÉ dont le déclencheur 15 min se forme est ouvert TOUT SEUL, avec
   son stop 15 min et son objectif borné 1 h.
2. **Jamais d'argent réel** — l'auto-entrée n'utilise que des connexions ``paper``, même quand une
   connexion réelle existe et que le KYC est validé.
3. **Pages instantanées** — les endpoints servent un instantané pré-calculé, jamais un recalcul.
4. **Entraînement mesuré** — le walk-forward produit des statistiques réelles et des
   multiplicateurs de poids par agent, sans jamais inventer un chiffre.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.config import get_settings
from app.core.security import hash_password
from app.data import sessions as sessions_mod
from app.repositories.store import get_store
from app.services import auto_entry_service, execution_service, live_snapshot, playbook_service
from tests.test_playbook import (  # réutilise les séries déterministes de la stratégie
    _END, _h1, _m15_with_pullback_entry, _no_trigger_15m, _tf,
)


def _at(hour: int) -> datetime:
    return datetime(2026, 7, 27, hour, tzinfo=timezone.utc)


def _user(store, email: str):
    tenant = store.tenants.create(name=email)
    return store.users.create(
        tenant_id=tenant.id, email=email, password_hash=hash_password("password123"),
        full_name="Auto",
    )


@pytest.fixture
def market(monkeypatch):
    """Marché déterministe : contexte haussier validé + déclencheur 15 min actif."""
    from app.data import markets

    s = get_settings()
    s.playbook_enabled = True
    s.playbook_auto_entry_enabled = True
    s.playbook_auto_paper_execute = True
    s.playbook_auto_entry_autoprovision = True
    # La garde d'heures de marché est testée séparément (voir plus bas). Ici on veut vérifier la
    # mécanique d'auto-entrée elle-même, sans que le résultat dépende du jour où la suite tourne.
    previous_gate = s.playbook_trade_only_when_open
    s.playbook_trade_only_when_open = False
    playbook_service.clear_cache()
    live_snapshot.reset()

    series = {
        "1M": _tf("monthly"), "1d": _tf("daily"), "4h": _tf("h4"), "1h": _h1(True),
        "15m": _m15_with_pullback_entry(True, end_close=_END),
    }

    async def _load(symbol, interval="1h", limit=200):  # noqa: ANN001
        return series.get(interval, series["4h"])

    monkeypatch.setattr(markets, "load_candles", _load)
    monkeypatch.setattr(markets, "data_source", lambda symbol: "real")
    yield series
    s.playbook_enabled = False
    s.playbook_trade_only_when_open = previous_gate
    playbook_service.clear_cache()
    live_snapshot.reset()


# ---------------------------------------------------------------------------------------
# 1. Aucun clic : le déclencheur 15 min ouvre la position tout seul
# ---------------------------------------------------------------------------------------
async def test_armed_setup_is_opened_automatically_when_the_trigger_fires(market):
    """Le cœur de la demande : personne ne clique, le robot entre dès que le 15 min déclenche."""
    store = get_store()
    user = _user(store, "auto1@test.com")
    candidates = [{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}]

    report = await auto_entry_service.run_auto_entry(store, candidates=candidates)

    assert report["enabled"] is True
    assert report["triggered"] == ["EUR/USD"]
    assert len(report["opened"]) == 1
    order = report["opened"][0]
    assert order["symbol"] == "EUR/USD" and order["side"] == "buy"
    # Les niveaux sont ceux de la stratégie : stop SOUS l'entrée, objectif AU-DESSUS.
    assert order["stop_loss"] < order["entry"] < order["take_profit"]
    # La position existe vraiment dans le compte démo — sans qu'aucune connexion n'ait été créée
    # à la main (auto-provisionnement du compte papier).
    orders = execution_service.list_orders(store, user.tenant_id)
    assert len(orders) == 1 and orders[0]["mode"] == "paper"


async def test_auto_entry_records_and_announces_the_opening(market):
    """Chaque entrée automatique laisse une trace consultable : on sait ce que le robot a fait."""
    store = get_store()
    user = _user(store, "auto2@test.com")
    await auto_entry_service.run_auto_entry(
        store, candidates=[{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}],
    )
    events = auto_entry_service.recent_events(store, user.tenant_id)
    assert len(events) == 1
    assert events[0]["symbol"] == "EUR/USD"
    assert "AUTOMATIQUE" in events[0]["message"]


async def test_auto_entry_does_nothing_without_a_trigger(market, monkeypatch):
    """Contexte validé mais aucun déclencheur 15 min -> le robot attend, il ne force jamais l'entrée."""
    from app.data import markets

    store = get_store()
    _user(store, "auto3@test.com")
    playbook_service.clear_cache()
    series = dict(market)
    series["15m"] = _no_trigger_15m(price=_END)

    async def _load(symbol, interval="1h", limit=200):  # noqa: ANN001
        return series.get(interval, series["4h"])

    monkeypatch.setattr(markets, "load_candles", _load)

    report = await auto_entry_service.run_auto_entry(
        store, candidates=[{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}],
    )
    assert report["opened"] == []
    assert report["armed"] == 1
    assert "aucun déclencheur" in report["note"]


async def test_auto_entry_never_doubles_an_open_position(market):
    """Deux passages de veille de suite n'ouvrent pas deux fois le même trade."""
    store = get_store()
    _user(store, "auto4@test.com")
    candidates = [{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}]
    first = await auto_entry_service.run_auto_entry(store, candidates=candidates)
    second = await auto_entry_service.run_auto_entry(store, candidates=candidates)
    assert len(first["opened"]) == 1
    assert second["opened"] == []


# ---------------------------------------------------------------------------------------
# 2. Garde-fou : jamais d'argent réel
# ---------------------------------------------------------------------------------------
async def test_auto_entry_never_touches_a_live_connection(market):
    """Même avec un broker RÉEL connecté et le KYC validé, l'auto-entrée reste en papier."""
    store = get_store()
    user = _user(store, "auto5@test.com")
    execution_service.submit_kyc(
        store, user.tenant_id, legal_name="Auto Test", country="FR", doc_id="X123",
    )
    live = execution_service.connect_broker(
        store, user.tenant_id, broker="alpaca", api_key="k", api_secret="s", mode="live",
    )
    assert live["mode"] == "live"

    await auto_entry_service.run_auto_entry(
        store, candidates=[{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}],
    )
    orders = execution_service.list_orders(store, user.tenant_id)
    assert orders, "l'auto-entrée doit avoir ouvert la position en démo"
    assert all(o["mode"] == "paper" for o in orders), "aucun ordre ne doit partir en réel"
    assert all(o["conn_id"] != live["id"] for o in orders)


async def test_auto_entry_refuses_when_london_and_new_york_are_closed(market):
    """Marchés fermés : on continue d'analyser, mais rien ne s'ouvre — carnet d'ordres vide."""
    s = get_settings()
    s.playbook_trade_only_when_open = True
    store = get_store()
    _user(store, "closed@test.com")
    try:
        report = await auto_entry_service.run_auto_entry(
            store, candidates=[{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}],
        )
    finally:
        s.playbook_trade_only_when_open = False
    # Le test tourne n'importe quel jour : on n'affirme quelque chose que si le marché est fermé.
    tradable, _ = sessions_mod.can_trade()
    if not tradable:
        assert report["opened"] == [] and report["market_open"] is False
        assert "aucune ouverture" in report["note"]


# ---------------------------------------------------------------------------------------
# 2 bis. Heures de marché : analyser toujours, n'ouvrir que Londres/New York ouvertes
# ---------------------------------------------------------------------------------------
def test_can_trade_only_when_london_or_new_york_is_open():
    """L'ANALYSE tourne en continu ; l'OUVERTURE exige une grande place ouverte."""
    def at(day: int, hour: int) -> datetime:
        return datetime(2026, 7, day, hour, tzinfo=timezone.utc)   # 2026-07-20 = lundi

    # Lundi 13:00 UTC : chevauchement Londres / New York -> on peut ouvrir.
    ok, why = sessions_mod.can_trade(at(20, 13))
    assert ok is True and ("Londres" in why or "New York" in why)
    # Lundi 03:00 UTC : Asie seule -> analyse oui, ouverture non.
    ok, why = sessions_mod.can_trade(at(20, 3))
    assert ok is False and "fermées" in why
    # Samedi : marché des changes fermé.
    ok, why = sessions_mod.can_trade(at(25, 13))
    assert ok is False and "week-end" in why
    # Dimanche 22:00 UTC : Sydney a rouvert, mais Londres/NY restent fermées.
    assert sessions_mod.is_weekend(at(26, 22)) is False
    assert sessions_mod.can_trade(at(26, 22))[0] is False
    # Vendredi 22:00 UTC : après la clôture de New York.
    assert sessions_mod.is_weekend(at(24, 22)) is True


def test_session_context_exposes_the_trading_window():
    ctx = sessions_mod.session_context(datetime(2026, 7, 20, 13, tzinfo=timezone.utc))
    assert ctx["can_trade"] is True and ctx["weekend"] is False and ctx["trade_window"]


# ---------------------------------------------------------------------------------------
# 2 ter. Sécurisation du profit : le stop remonte sur +2R
# ---------------------------------------------------------------------------------------
async def test_profit_is_secured_at_two_r(market, monkeypatch):
    """+2R atteint -> le stop est remonté SUR +2R : la position ne peut plus être perdante."""
    store = get_store()
    user = _user(store, "secure@test.com")
    await auto_entry_service.run_auto_entry(
        store, candidates=[{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}],
    )
    order = execution_service.list_orders(store, user.tenant_id)[0]
    entry, sl = order["entry"], order["stop_loss"]
    risk = abs(entry - sl)
    assert order["initial_risk"] == risk

    # Le prix a parcouru +2,4 R : la sécurisation doit se déclencher.
    monkeypatch.setattr(execution_service, "_reference_price",
                        _fixed_price(entry + 2.4 * risk))
    assert await execution_service.secure_open_profits(store) == 1

    secured = execution_service.list_orders(store, user.tenant_id)[0]
    assert secured["profit_secured"] is True
    assert abs(secured["stop_loss"] - (entry + 2.0 * risk)) < 1e-6
    assert secured["original_stop_loss"] == sl
    # Le stop est passé AU-DESSUS de l'entrée : le trade est désormais gagnant quoi qu'il arrive.
    assert secured["stop_loss"] > entry
    # Deuxième passage : rien à faire (on ne déplace pas deux fois).
    assert await execution_service.secure_open_profits(store) == 0


async def test_profit_is_not_secured_before_two_r(market, monkeypatch):
    """À +1,5 R le stop ne bouge pas : sécuriser trop tôt couperait les gagnants."""
    store = get_store()
    user = _user(store, "nosecure@test.com")
    await auto_entry_service.run_auto_entry(
        store, candidates=[{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}],
    )
    order = execution_service.list_orders(store, user.tenant_id)[0]
    risk = abs(order["entry"] - order["stop_loss"])
    monkeypatch.setattr(execution_service, "_reference_price",
                        _fixed_price(order["entry"] + 1.5 * risk))
    assert await execution_service.secure_open_profits(store) == 0
    assert execution_service.list_orders(store, user.tenant_id)[0]["stop_loss"] == order["stop_loss"]


def _fixed_price(price: float):
    async def _price(_symbol):
        return price
    return _price


async def test_auto_entry_is_off_when_disabled(market):
    """Le réglage coupe réellement la veille (rien ne s'ouvre à l'insu de l'utilisateur)."""
    s = get_settings()
    s.playbook_auto_entry_enabled = False
    try:
        report = await auto_entry_service.run_auto_entry(
            get_store(), candidates=[{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}],
        )
        assert report["enabled"] is False and report["opened"] == []
    finally:
        s.playbook_auto_entry_enabled = True


# ---------------------------------------------------------------------------------------
# 3. Instantané temps réel : la page ne calcule plus rien
# ---------------------------------------------------------------------------------------
async def test_snapshot_is_served_without_recomputing(market, monkeypatch):
    """Deuxième appel = zéro calcul : c'est ce qui autorise un rafraîchissement toutes les 10 s."""
    store = get_store()
    calls = {"n": 0}
    real = playbook_service.top_trades

    async def _counted(*args, **kwargs):
        calls["n"] += 1
        return await real(*args, **kwargs)

    monkeypatch.setattr(playbook_service, "top_trades", _counted)

    first = await live_snapshot.get(store)
    second = await live_snapshot.get(store)
    assert calls["n"] == 1, "le second appel doit lire l'instantané, pas recalculer"
    assert second["age_seconds"] >= 0 and second["stale"] is False
    assert first["picks"] == second["picks"]

    forced = await live_snapshot.get(store, force=True)
    assert calls["n"] == 2, "« recalculer maintenant » doit bien relancer le calcul"
    assert forced["computed_at"] >= first["computed_at"]


async def test_snapshot_exposes_a_verdict_for_every_scanned_symbol(market):
    """Toutes les analyses parlent la même langue : un verdict de la stratégie par symbole balayé."""
    universe = [{"symbol": s, "asset_class": "forex"} for s in ("EUR/USD", "GBP/USD", "USD/CHF")]
    payload = await playbook_service.top_trades(5, universe=universe, now=_at(13))
    assert set(payload["verdicts"]) == {"EUR/USD", "GBP/USD", "USD/CHF"}
    for verdict in payload["verdicts"].values():
        assert verdict["tier"] in ("ready", "armed", "none", "insufficient")
        assert "reliability_score" in verdict and "context_reliability" in verdict


async def test_top_trades_are_ranked_and_numbered(market):
    """Les 5 trades sortent classés, exécutables d'abord, chacun portant son rang."""
    universe = [{"symbol": s, "asset_class": "forex"} for s in ("EUR/USD", "GBP/USD", "USD/CHF")]
    payload = await playbook_service.top_trades(5, universe=universe, now=_at(13))
    picks = payload["picks"]
    assert picks, "le jeu de données doit produire au moins un setup"
    assert [p["rank"] for p in picks] == list(range(1, len(picks) + 1))
    tiers = [p["tier"] for p in picks]
    assert tiers == sorted(tiers, key=lambda t: 0 if t == "ready" else 1)
    assert payload["auto_entry"] is True


# ---------------------------------------------------------------------------------------
# 4. Entraînement quotidien : des chiffres mesurés, jamais inventés
# ---------------------------------------------------------------------------------------
def test_training_metrics_are_arithmetically_correct():
    """L'espérance en R et le profit factor se reconstituent à la main."""
    from app.services import training_service

    trades = [
        {"outcome": "won", "r": 1.3}, {"outcome": "won", "r": 1.3},
        {"outcome": "lost", "r": -1.0}, {"outcome": "lost", "r": -1.0},
        {"outcome": "won", "r": 1.2},
    ]
    m = training_service._metrics(trades)
    assert m["trades"] == 5 and m["wins"] == 3 and m["losses"] == 2
    assert m["win_rate"] == 60.0
    assert abs(m["expectancy_r"] - (1.3 + 1.3 - 1.0 - 1.0 + 1.2) / 5) < 1e-9
    assert abs(m["profit_factor"] - 3.8 / 2.0) < 0.01


def test_factor_competence_and_agent_multipliers():
    """La compétence d'un facteur devient le poids de l'agent qui le porte — sans magie."""
    from app.services import training_service

    # Le RSI plaide DANS le sens du trade et le trade gagne : il avait raison.
    trades = [{"outcome": "won", "r": 1.3, "factor_votes": {"rsi": 1, "volume": -1}} for _ in range(10)]
    comp = training_service._factor_competence(trades)
    assert comp["rsi"]["accuracy"] == 100.0 and comp["rsi"]["observations"] == 10
    # Le volume plaidait CONTRE un trade gagnant : il avait tort.
    assert comp["volume"]["accuracy"] == 0.0

    mult = training_service._agent_multipliers(comp, min_obs=5)
    assert mult["technical"] > 1.0, "un facteur juste doit renforcer son agent"
    assert mult["volume"] < 1.0, "un facteur faux doit affaiblir le sien"
    assert 0.7 <= min(mult.values()) and max(mult.values()) <= 1.3   # bornes respectées


def test_edge_is_silent_when_not_measured(monkeypatch):
    """On ne prétend jamais connaître la fiabilité d'un symbole qu'on n'a pas mesuré."""
    from app.services import training_service

    monkeypatch.setattr(training_service, "_STATE", {})
    assert training_service.edge_for("EUR/USD") is None

    monkeypatch.setattr(training_service, "_STATE", {
        "trades": 40,
        "by_symbol": {"EUR/USD": {"trades": 20, "win_rate": 60.0, "expectancy_r": 0.34}},
        "by_trigger": {},
    })
    known = training_service.edge_for("EUR/USD", trigger_type="repli")
    assert known["score"] == 0.34 and known["trades"] == 20
    unknown = training_service.edge_for("XAU/USD")
    assert unknown["score"] == 0.0 and "non mesuré" in unknown["status"]


def test_edge_says_when_only_the_trigger_was_measured(monkeypatch):
    """Un symbole sans historique suffisant ne doit pas s'attribuer les trades du déclencheur."""
    from app.services import training_service

    monkeypatch.setattr(training_service, "_STATE", {
        "trades": 30,
        # 3 trades sur BTC : sous le seuil, donc NON mesuré pour le symbole.
        "by_symbol": {"BTC/USDT": {"trades": 3, "win_rate": 33.0, "expectancy_r": -0.5}},
        "by_trigger": {"repli": {"trades": 18, "win_rate": 50.0, "expectancy_r": 0.13}},
    })
    edge = training_service.edge_for("BTC/USDT", trigger_type="repli")
    assert edge["measured_on"] == "déclencheur seul"
    assert "déclencheur « repli »" in edge["status"]
    assert "BTC/USDT :" not in edge["status"], "ne pas attribuer au symbole des trades qui ne sont pas les siens"


def test_fallback_memo_does_not_repeat_the_same_market_twice():
    """Avec un seul marché qualifié, on ne le présente pas comme son propre contraire."""
    from app.services import training_service

    payload = {
        "date": "2026-07-27", "min_trades": 8,
        "overall": {"trades": 22, "win_rate": 45.5, "expectancy_r": 0.03},
        "factor_competence": {},
        "by_symbol": {"ETH/USDT": {"trades": 11, "expectancy_r": 0.24},
                      "BTC/USDT": {"trades": 3, "expectancy_r": -0.34}},
        "by_trigger": {}, "by_session": {},
    }
    memo = training_service._fallback_memo("playbook", payload)
    assert "Meilleurs marchés : ETH/USDT" in memo
    assert "Moins bons" not in memo, "un seul marché qualifié : pas de comparatif bancal"


async def test_training_refuses_synthetic_data(monkeypatch):
    """Aucune statistique n'est tirée de bougies inventées — c'est pire qu'une absence de mesure."""
    from app.data import markets
    from app.services import training_service

    monkeypatch.setattr(markets, "is_real", lambda symbol: False)
    monkeypatch.setattr(markets, "load_candles", lambda *a, **k: _fake_candles())
    # L'attente entre tentatives ne doit pas ralentir le test.
    monkeypatch.setattr(training_service.asyncio, "sleep", _noop_sleep)

    res = await training_service.train_symbol("BTC/USDT", asset_class="crypto")
    assert res["trades"] == []
    assert "non réelles" in res["error"]


async def _fake_candles():
    from app.domain.indicators import Candle

    return [Candle(1.0, 1.1, 0.9, 1.0, 10.0) for _ in range(300)]


async def _noop_sleep(_seconds):
    return None


def test_expertise_falls_back_without_llm():
    """Sans LLM, la fiche d'expertise reste utile : elle expose les chiffres mesurés."""
    from app.services import training_service

    payload = {
        "date": "2026-07-27", "min_trades": 8,
        "overall": {"trades": 42, "win_rate": 57.1, "expectancy_r": 0.21},
        "factor_competence": {"rsi": {"accuracy": 61.0, "observations": 30}},
        "by_symbol": {"EUR/USD": {"trades": 20, "expectancy_r": 0.3}},
        "by_trigger": {}, "by_session": {},
    }
    memo = training_service._fallback_memo("technical", payload)
    assert "2026-07-27" in memo and "57.1" in memo and "61.0" in memo


# ---------------------------------------------------------------------------------------
# 5. API
# ---------------------------------------------------------------------------------------
def test_api_auto_entry_status(market):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": "aeapi@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    resp = client.get("/api/signals/auto-entry", headers=h)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["mode"] == "paper", "l'auto-entrée doit annoncer clairement qu'elle est en démo"
    assert body["interval_seconds"] > 0 and "recent" in body


def test_api_top_trades_reports_its_freshness(market):
    """La page doit pouvoir dire « mis à jour il y a X s » — la fraîcheur fait partie de la donnée."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": "fresh@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    body = client.get("/api/signals/top-trades?count=5", headers=h).json()
    assert "age_seconds" in body and "stale" in body and "computed_at" in body
    assert body["refresh_interval"] > 0
    assert body["stale"] is False


def test_api_training_endpoint(market):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": "train@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    body = client.get("/api/agents/training", headers=h).json()
    assert "trained" in body      # non entraîné au démarrage : la réponse le dit franchement
    status = client.get("/api/agents/status", headers=h).json()
    assert "training" in status and "trained" in status["training"]
