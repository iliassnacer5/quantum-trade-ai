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

    async def _load(symbol, interval="1h", limit=200, **kw):  # noqa: ANN001
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


async def test_an_opened_position_carries_the_justification_of_its_levels(market):
    """Chaque position doit porter POURQUOI son stop et son objectif sont là.

    La stratégie ne place pas le stop à une distance calculée : elle le pose sur ce qui rendrait le
    scénario FAUX, et l'objectif devant le premier obstacle réel. Elle l'explique déjà en clair
    (`stop_basis`, `target_basis`) — mais ces explications n'étaient pas conservées sur l'ordre. La
    carte affichait donc un stop à 72.7987919 sans que rien ne dise pourquoi ce nombre-là, et un
    niveau qu'on ne sait pas justifier ne peut être ni discuté, ni appris.
    """
    store = get_store()
    user = _user(store, "justif@test.com")
    await auto_entry_service.run_auto_entry(
        store, candidates=[{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}],
    )
    ordre = execution_service.list_orders(store, user.tenant_id)[0]

    assert ordre.get("stop_basis"), "le stop doit dire ce qui invaliderait le scénario"
    assert ordre.get("target_basis"), "l'objectif doit dire ce qui le borne"
    # Les distances en pips accompagnent les niveaux : c'est la lecture indépendante de la taille.
    assert ordre.get("risk_pips") is not None and ordre.get("reward_pips") is not None
    assert ordre.get("pips_label")
    # Et la justification de l'ENTRÉE reste distincte de celle des NIVEAUX.
    assert ordre.get("trigger") and ordre["trigger"] != ordre["stop_basis"]


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

    async def _load(symbol, interval="1h", limit=200, **kw):  # noqa: ANN001
        return series.get(interval, series["4h"])

    monkeypatch.setattr(markets, "load_candles", _load)

    report = await auto_entry_service.run_auto_entry(
        store, candidates=[{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}],
    )
    assert report["opened"] == []
    assert report["armed"] == 1
    assert "aucun déclencheur" in report["note"]


async def test_auto_entry_refreshes_an_empty_pool_instead_of_concluding(market, monkeypatch):
    """Un vivier VIDE ne prouve pas qu'il n'y a pas de trade — il faut le recalculer, pas conclure.

    `armed_and_ready()` lit l'instantané sans contrôle de fraîcheur. Boucle de fond tombée, jamais
    passée, ou remise à zéro -> vivier vide, et la veille répondait « aucun setup armé » à chaque
    passage, indéfiniment. Le cache décidait donc qu'il n'y avait pas de trade : c'est le symptôme
    « les agents ouvrent beaucoup moins de positions ».
    """
    store = get_store()
    _user(store, "auto-pool@test.com")
    live_snapshot.reset()          # simule une boucle de fond tombée / un redémarrage

    refreshes = {"n": 0}
    real_refresh = live_snapshot.refresh

    async def _counted(*args, **kwargs):
        refreshes["n"] += 1
        return await real_refresh(*args, **kwargs)

    monkeypatch.setattr(live_snapshot, "refresh", _counted)

    await auto_entry_service.run_auto_entry(store)
    assert refreshes["n"] == 1, "un vivier absent doit déclencher un recalcul, pas une conclusion"


async def test_auto_entry_does_not_refresh_a_fresh_pool(market, monkeypatch):
    """Le recalcul de secours ne doit PAS se déclencher quand l'instantané est à jour.

    Sinon la veille (toutes les 60 s) relancerait le balayage complet de l'univers en boucle, ce que
    l'architecture d'instantané existe précisément pour éviter.
    """
    from datetime import UTC

    store = get_store()
    _user(store, "auto-pool2@test.com")
    live_snapshot.reset()
    live_snapshot._snapshot = {                       # noqa: SLF001 — instantané frais déjà publié
        "picks": [{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}],
        "ready": 0, "armed": 1, "scanned": 1, "conform": 1,
        "session": {}, "strategy": "s", "verdicts": {}, "requested": "tous",
        "computed_at": datetime.now(UTC).isoformat(),
    }

    refreshes = {"n": 0}
    real_refresh = live_snapshot.refresh

    async def _counted(*args, **kwargs):
        refreshes["n"] += 1
        return await real_refresh(*args, **kwargs)

    monkeypatch.setattr(live_snapshot, "refresh", _counted)

    await auto_entry_service.run_auto_entry(store)
    assert refreshes["n"] == 0, "un instantané frais ne doit jamais être recalculé par la veille"
    live_snapshot.reset()


async def test_a_pass_with_nothing_to_do_still_refreshes_its_report(market, monkeypatch):
    """« Au dernier passage » doit désigner LE dernier passage — pas le dernier qui avait à dire.

    Le rapport n'était persisté qu'au bout de la fonction, donc uniquement quand des setups étaient
    prêts. Dès qu'un passage ne trouvait rien (le cas le plus fréquent), le rapport PRÉCÉDENT
    restait affiché indéfiniment : un refus « déjà 2 positions ouvertes exposées à USDT » survivait
    à la remise à zéro du portefeuille et contredisait Paper Trading, qui affichait zéro position.
    """
    store = get_store()
    user = _user(store, "stale-report@test.com")

    # Un passage précédent a refusé un setup, pour une raison désormais périmée.
    store.records.put(auto_entry_service.LAST_RUN, "latest", {
        "enabled": True, "opened": [], "armed": 1,
        "skipped": [{"symbol": "AAVE/USDT", "reason": "déjà 2 position(s) ouverte(s)",
                     "tenant_id": user.tenant_id}],
        "at": "2026-07-31T10:00:00+00:00",
    })
    assert auto_entry_service.blocked_for(store, user.tenant_id), "état de départ du test"

    # Passage suivant : plus aucun setup armé à surveiller.
    live_snapshot.reset()
    live_snapshot._snapshot = {                     # noqa: SLF001 — instantané frais, mais vide
        "picks": [], "ready": 0, "armed": 0, "scanned": 0, "conform": 0,
        "session": {}, "strategy": "s", "verdicts": {}, "requested": "tous",
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    await auto_entry_service.run_auto_entry(store)

    assert auto_entry_service.blocked_for(store, user.tenant_id) == [], (
        "un passage sans rien à faire doit EFFACER les refus périmés, pas les laisser à l'écran"
    )
    assert auto_entry_service.last_run_at(store) != "2026-07-31T10:00:00+00:00", (
        "le rapport doit être réhorodaté à chaque passage"
    )
    live_snapshot.reset()


async def test_resetting_clears_the_stale_refusals(market):
    """La remise à zéro efface le rapport : ses refus invoquent des positions qui n'existent plus."""
    store = get_store()
    user = _user(store, "reset-report@test.com")
    store.records.put(auto_entry_service.LAST_RUN, "latest", {
        "enabled": True, "opened": [], "armed": 0,
        "skipped": [{"symbol": "AAVE/USDT", "reason": "déjà 2 position(s) ouverte(s)",
                     "tenant_id": user.tenant_id}],
        "at": datetime.now(timezone.utc).isoformat(),
    })

    auto_entry_service.reset(store, user.tenant_id)

    assert auto_entry_service.blocked_for(store, user.tenant_id) == [], (
        "après une remise à zéro, aucun refus ne peut encore invoquer les positions supprimées"
    )


async def test_a_closed_market_is_never_traded(market, monkeypatch):
    """MARCHÉ FERMÉ = AUCUNE OUVERTURE. Un samedi, GER40 se négocie sur la bougie de vendredi.

    Les fournisseurs continuent de servir la dernière bougie connue quand une place est fermée :
    elle est réelle, mais plus d'actualité. Ouvrir dessus poserait entrée, stop et objectif sur un
    marché à l'arrêt, et l'anti-doublon de 45 min laisserait le même setup figé se reprendre en
    boucle.
    """
    store = get_store()
    _user(store, "closed-market@test.com")
    playbook_service.clear_cache()          # le setup doit être RECALCULÉ, donc réellement évalué
    s = get_settings()
    precedent = s.playbook_trade_only_when_open
    s.playbook_trade_only_when_open = True
    monkeypatch.setattr(sessions_mod, "can_trade",
                        lambda now=None: (False, "week-end : marché des changes fermé"))
    try:
        report = await auto_entry_service.run_auto_entry(
            store, candidates=[{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}],
        )
        # LE RÉSULTAT QUI COMPTE : rien ne s'ouvre sur un marché fermé.
        assert report["opened"] == [], "aucune position ne doit s'ouvrir marché fermé"
        # Et le refus est motivé, à l'une ou l'autre des deux couches : le playbook refuse déjà de
        # déclarer le setup exécutable (`can_trade` par symbole), et `_open_market_only` reste un
        # second verrou si un déclencheur arrivait malgré tout jusqu'à l'exécution.
        motif = " ".join([report.get("note") or ""]
                         + [m["reason"] for m in (report.get("market_closed") or [])])
        assert "marché fermé" in motif or "déclencheur" in motif, motif
    finally:
        s.playbook_trade_only_when_open = precedent


async def test_crypto_still_trades_when_the_exchanges_are_closed(market, monkeypatch):
    """La CRYPTO cote 24 h/24 : lui appliquer les horaires de Londres refuserait un marché ouvert.

    C'est la nuance qui distingue « ne trade pas sur des données figées » de « ne trade pas le
    week-end » : les bougies crypto du samedi sont réelles ET fraîches.
    """
    from app.data import markets

    store = get_store()
    _user(store, "crypto-weekend@test.com")
    s = get_settings()
    precedent = s.playbook_trade_only_when_open
    s.playbook_trade_only_when_open = True
    monkeypatch.setattr(sessions_mod, "can_trade",
                        lambda now=None: (False, "week-end : marché des changes fermé"))
    try:
        ouverts, fermes = auto_entry_service._open_market_only([   # noqa: SLF001
            {"symbol": "BTC/USDT"}, {"symbol": "EUR/USD"}, {"symbol": "GER40"}, {"symbol": "AAPL"},
        ])
        assert [p["symbol"] for p in ouverts] == ["BTC/USDT"], "seule la crypto reste tradable"
        assert {f["symbol"] for f in fermes} == {"EUR/USD", "GER40", "AAPL"}
        assert markets.asset_class("BTC/USDT") == "crypto"
    finally:
        s.playbook_trade_only_when_open = precedent


def test_no_session_is_open_during_the_weekend():
    """Le week-end, AUCUNE place n'est ouverte — pas même à l'heure du chevauchement.

    `current_sessions` ne regardait que l'HEURE : un samedi à 13 h, elle annonçait Londres et
    New York ouvertes et le chevauchement actif. La bannière affichait donc des places ouvertes un
    jour de fermeture, les ordres étaient horodatés `session_window = overlap`, et l'étape 7 de la
    stratégie accordait un bonus de conviction pour une fenêtre inexistante.
    """
    samedi_13h = datetime(2026, 8, 1, 13, tzinfo=timezone.utc)      # samedi, plein « overlap »
    assert sessions_mod.is_weekend(samedi_13h)
    assert sessions_mod.current_sessions(samedi_13h) == []
    assert sessions_mod.active_kill_zones(samedi_13h) == []
    assert sessions_mod.is_overlap(samedi_13h) is False
    ctx = sessions_mod.session_context(samedi_13h)
    assert ctx.get("weekend") is True
    assert not ctx.get("prime"), "aucune fenêtre à forte valeur quand tout est fermé"

    # ...et la semaine, à la même heure, le chevauchement est bien détecté.
    mercredi_13h = datetime(2026, 7, 29, 13, tzinfo=timezone.utc)
    assert sessions_mod.current_sessions(mercredi_13h), "la semaine reste inchangée"
    assert sessions_mod.is_overlap(mercredi_13h) is True


async def test_a_closed_market_setup_is_flagged_analysis_only(market, monkeypatch):
    """ANALYSER TOUJOURS, N'OUVRIR QUE SI LA PLACE EST OUVERTE — et le DIRE.

    Un setup armé sur une action affichait « la position s'ouvrira toute seule dès que le
    déclencheur se formera ». Un samedi, c'est faux : le garde-fou d'heures de marché l'en
    empêchera. La carte promettait une exécution impossible. On expose donc l'état réel du marché
    de CHAQUE symbole, pour que l'interface distingue « je surveille et j'ouvrirai » de
    « j'analyse seulement ».
    """
    s = get_settings()
    precedent = s.playbook_trade_only_when_open
    s.playbook_trade_only_when_open = True
    monkeypatch.setattr(sessions_mod, "can_trade",
                        lambda now=None: (False, "week-end : marché des changes fermé"))
    playbook_service.clear_cache()
    try:
        univers = [{"symbol": "EUR/USD", "asset_class": "forex"},
                   {"symbol": "BTC/USDT", "asset_class": "crypto"}]
        payload = await playbook_service.top_trades(5, universe=univers, now=_at(13))
        par_symbole = {p["symbol"]: p for p in payload["picks"]}

        if "EUR/USD" in par_symbole:
            fx = par_symbole["EUR/USD"]
            assert fx["tradable_now"] is False, "le forex est fermé le week-end"
            assert "fermé" in fx["market_status"]
        if "BTC/USDT" in par_symbole:
            assert par_symbole["BTC/USDT"]["tradable_now"] is True, "la crypto cote 24 h/24"

        # Le compteur d'en-tête doit distinguer les armés réellement exécutables.
        assert "armed_market_closed" in payload
    finally:
        s.playbook_trade_only_when_open = precedent
        playbook_service.clear_cache()


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
    # Le refus est désormais PAR SYMBOLE (`market_closed`) et non plus un blocage global : la
    # crypto continue de coter quand les places boursières sont fermées.
    tradable, _ = sessions_mod.can_trade()
    if not tradable:
        assert report["opened"] == [], "aucune ouverture forex marché fermé"
        refuses = {m["symbol"] for m in (report.get("market_closed") or [])}
        # EUR/USD est soit écarté pour marché fermé, soit jamais arrivé jusque-là (pas de
        # déclencheur formé sur ce passage) — les deux sont des refus légitimes.
        assert "EUR/USD" in refuses or not report.get("triggered")


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
def _paper_order(store, tenant_id, *, entry=1.1000, risk=0.0050, rr=3.0, side="buy", oid="sec-1"):
    """Position papier ouverte, dont on maîtrise exactement le R/R (donc la place de l'objectif)."""
    sign = 1 if side == "buy" else -1
    return store.records.put(execution_service.ORDER, oid, {
        "id": oid, "mode": "paper", "symbol": "EUR/USD", "side": side, "qty": 1000.0,
        "entry": entry, "filled_price": entry,
        "stop_loss": entry - sign * risk,
        "take_profit": entry + sign * rr * risk,
        "initial_risk": risk, "outcome": None, "tenant_id": tenant_id,
    }, tenant_id=tenant_id)


async def test_profit_is_secured_at_two_r(market, monkeypatch):
    """+2R atteint ET objectif au-delà -> le stop est remonté SUR +2R : le gain est verrouillé.

    R/R 1:3 : l'objectif est à +3R, donc au-delà du niveau sécurisé. C'est exactement le cas où la
    règle du desk a un sens — verrouiller +2R puis laisser courir vers le R/R maximum.
    """
    store = get_store()
    user = _user(store, "secure@test.com")
    entry, risk = 1.1000, 0.0050
    order = _paper_order(store, user.tenant_id, entry=entry, risk=risk, rr=3.0)
    sl = order["stop_loss"]

    monkeypatch.setattr(execution_service, "_reference_price",
                        _fixed_price(entry + 2.4 * risk))
    await execution_service.secure_open_profits(store)

    secured = store.records.get(execution_service.ORDER, order["id"])
    assert secured["profit_secured"] is True
    assert abs(secured["stop_loss"] - (entry + 2.0 * risk)) < 1e-6
    assert secured["original_stop_loss"] == sl
    # Le stop est passé AU-DESSUS de l'entrée : le trade est désormais gagnant quoi qu'il arrive.
    assert secured["stop_loss"] > entry
    # ...et il reste SOUS l'objectif : la position peut encore aller le chercher.
    assert secured["stop_loss"] < secured["take_profit"]
    # Deuxième passage : rien à faire (on ne déplace pas deux fois).
    before = dict(secured)
    await execution_service.secure_open_profits(store)
    assert store.records.get(execution_service.ORDER, order["id"])["stop_loss"] == before["stop_loss"]


async def test_the_secured_stop_never_reaches_the_objective(market, monkeypatch):
    """R/R 1:2 -> +2R EST l'objectif : il n'y a rien à protéger, le stop ne bouge pas.

    Mesuré sur CVX le 30/07/2026 : le stop était déplacé sur 193.45714798 pour un objectif à
    193.45714797. Trois conséquences — le rejeu clôturait sur le STOP au lieu de l'objectif et la
    carte annonçait « stop de sécurisation touché » sur un trade qui avait atteint sa cible ; stop
    et objectif s'affichaient au même nombre de pips ; et pour un achat le stop passait AU-DESSUS
    de l'objectif, un panier d'ordres qu'un vrai broker refuserait.

    Aucun gain n'est perdu : l'objectif clôture au même prix, ou mieux.
    """
    store = get_store()
    user = _user(store, "secure-rr2@test.com")
    entry, risk = 1.1000, 0.0050
    order = _paper_order(store, user.tenant_id, entry=entry, risk=risk, rr=2.0)

    monkeypatch.setattr(execution_service, "_reference_price",
                        _fixed_price(entry + 2.4 * risk))
    # On juge CET ordre, pas le compteur global : `secure_open_profits` balaie tous les tenants.
    await execution_service.secure_open_profits(store)

    unchanged = store.records.get(execution_service.ORDER, order["id"])
    assert unchanged["stop_loss"] == order["stop_loss"], "le stop ne doit pas atteindre l'objectif"
    assert not unchanged.get("profit_secured")


async def test_the_secured_stop_never_reaches_the_objective_on_sell(market, monkeypatch):
    """Le même garde-fou, côté vente (les niveaux sont inversés)."""
    store = get_store()
    user = _user(store, "secure-sell@test.com")
    entry, risk = 1.1000, 0.0050
    order = _paper_order(store, user.tenant_id, entry=entry, risk=risk, rr=2.0,
                         side="sell", oid="sec-sell")

    monkeypatch.setattr(execution_service, "_reference_price",
                        _fixed_price(entry - 2.4 * risk))
    await execution_service.secure_open_profits(store)
    unchanged = store.records.get(execution_service.ORDER, order["id"])
    assert unchanged["stop_loss"] == order["stop_loss"], "le stop ne doit pas atteindre l'objectif"
    assert not unchanged.get("profit_secured")


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
    async def _price(_symbol, *, fresh: bool = False):  # `fresh` : cf. _reference_price
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


# ---------------------------------------------------------------------------------------
# 5. Anti-doublon et remise à zéro (demandés le 28/07/2026)
# ---------------------------------------------------------------------------------------
async def test_the_same_trigger_is_not_taken_twice_in_a_row(market):
    """Quatre entrées ETH/USDT en quatre minutes : c'est ce que le délai anti-doublon empêche.

    Le déclencheur 15 min reste actif plusieurs passages de veille d'affilée. `_already_open` ne
    couvre que les positions ENCORE ouvertes : dès que la précédente se referme, la suivante
    repartait aussitôt, au même prix. Quatre fois le même pari, donc quatre fois le risque prévu
    pour un seul.
    """
    store = get_store()
    user = _user(store, "cooldown@test.com")
    s = get_settings()
    s.playbook_auto_entry_cooldown_min = 45
    candidates = [{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}]

    first = await auto_entry_service.run_auto_entry(store, candidates=candidates)
    assert len(first["opened"]) == 1

    # La position se referme (gagnante) : sans le délai, le passage suivant rouvrirait aussitôt.
    order = execution_service.list_orders(store, user.tenant_id)[0]
    store.records.put(execution_service.ORDER, order["id"],
                      {**order, "outcome": "won"}, tenant_id=user.tenant_id)

    second = await auto_entry_service.run_auto_entry(store, candidates=candidates)
    assert second["opened"] == []
    assert any("anti-doublon" in sk["reason"] for sk in second["skipped"]), second["skipped"]
    # Le rapport DIT pourquoi rien n'est parti — un refus silencieux passe pour une panne.
    assert "anti-doublon" in second["note"]


async def test_the_cooldown_can_be_switched_off(market):
    """`playbook_auto_entry_cooldown_min = 0` désactive le garde : c'est l'ancien comportement."""
    store = get_store()
    user = _user(store, "nocooldown@test.com")
    s = get_settings()
    s.playbook_auto_entry_cooldown_min = 0
    candidates = [{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}]

    await auto_entry_service.run_auto_entry(store, candidates=candidates)
    order = execution_service.list_orders(store, user.tenant_id)[0]
    store.records.put(execution_service.ORDER, order["id"],
                      {**order, "outcome": "won"}, tenant_id=user.tenant_id)

    second = await auto_entry_service.run_auto_entry(store, candidates=candidates)
    assert len(second["opened"]) == 1
    s.playbook_auto_entry_cooldown_min = 45


async def test_reset_neutralises_open_demo_positions_without_inventing_a_result(market):
    """La remise à zéro ne clôture pas « gagnant » ni « perdant » : elle NEUTRALISE.

    Une position rouverte n'a pas été jouée jusqu'au bout. Lui donner une issue fabriquerait un
    résultat que le marché n'a pas produit — exactement le défaut qui avait produit 13 000 € de
    profit fictif. Elle sort donc des statistiques, et le P&L n'en garde rien.
    """
    store = get_store()
    user = _user(store, "reset@test.com")
    s = get_settings()
    s.playbook_auto_entry_cooldown_min = 45
    candidates = [{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}]
    await auto_entry_service.run_auto_entry(store, candidates=candidates)
    assert len(execution_service.list_orders(store, user.tenant_id)) == 1

    out = auto_entry_service.reset(store, user.tenant_id)

    assert len(out["closed"]) == 1 and out["events_cleared"] == 1
    order = execution_service.list_orders(store, user.tenant_id)[0]
    assert order["outcome"] == "reset" and order["pnl"] == 0.0
    # Ni gagnante ni perdante : elle ne compte nulle part.
    snapshot = await execution_service.positions_snapshot(store, user.tenant_id)
    assert snapshot["wins"] == 0 and snapshot["losses"] == 0
    assert snapshot["open_count"] == 0
    # L'historique des entrées automatiques est vide, donc le délai anti-doublon repart de zéro.
    assert auto_entry_service.recent_events(store, user.tenant_id) == []


async def test_reset_lets_the_next_trigger_fire_immediately(market):
    """Après une remise à zéro, le prochain déclencheur repart : c'est tout l'intérêt du bouton."""
    store = get_store()
    user = _user(store, "reset2@test.com")
    s = get_settings()
    s.playbook_auto_entry_cooldown_min = 45
    candidates = [{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}]
    await auto_entry_service.run_auto_entry(store, candidates=candidates)

    auto_entry_service.reset(store, user.tenant_id)
    again = await auto_entry_service.run_auto_entry(store, candidates=candidates)

    assert len(again["opened"]) == 1, again["note"]


def test_api_auto_entry_reset_endpoint(market):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": "resetapi@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    body = client.post("/api/signals/auto-entry/reset?relaunch=false", headers=h).json()
    assert "closed" in body and "events_cleared" in body
    assert "conservé" in body["note"]        # les trades clôturés ne sont JAMAIS effacés

    status = client.get("/api/signals/auto-entry", headers=h).json()
    assert status["pair_gating"] is False    # plus de refus silencieux par verdict de paire
    assert status["cooldown_min"] >= 0


# ---------------------------------------------------------------------------------------
# 6. Refus non silencieux (garde-fou de portefeuille, corrélation…) — demandé le 28/07/2026
# ---------------------------------------------------------------------------------------
async def test_a_ready_setup_blocked_by_the_portfolio_guard_is_explained_not_silent(market):
    """Un setup EXÉCUTABLE à l'écran mais refusé par un garde-fou doit le dire, pas se taire.

    Avant ce correctif, un passage qui n'ouvrait rien ne laissait AUCUNE trace côté logs ni côté
    API : impossible de distinguer « le robot est en panne » de « le garde-fou de portefeuille a
    fait exactement ce qu'on lui a demandé ».
    """
    from app.core.config import get_settings
    from app.services import execution_service

    store = get_store()
    user = _user(store, "blocked1@test.com")
    s = get_settings()
    s.paper_portfolio_guard = True
    # 0 = AUCUN plafond (décision du 28/07/2026) : pour forcer le refus, on remplit le plafond
    # avec une position déjà ouverte plutôt que de le fixer à zéro.
    s.paper_max_positions = 1
    conn_id = execution_service.ensure_paper_connection(store, user.tenant_id)
    await execution_service.place_order(
        store, user.tenant_id, conn_id=conn_id, symbol="GBP/USD", side="buy", qty=1.0,
    )
    try:
        report = await auto_entry_service.run_auto_entry(
            store, candidates=[{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}],
        )
        assert report["opened"] == []
        assert any("Limite de" in sk["reason"] for sk in report["skipped"]), report["skipped"]

        blocked = auto_entry_service.blocked_for(store, user.tenant_id)
        assert len(blocked) == 1
        assert blocked[0]["symbol"] == "EUR/USD"
        assert "Limite de" in blocked[0]["reason"]
    finally:
        s.paper_portfolio_guard = False
        s.paper_max_positions = 0


async def test_blocked_for_is_scoped_to_the_right_tenant(market):
    """Le refus d'UN tenant ne doit jamais apparaître comme le refus d'un AUTRE."""
    from app.core.config import get_settings
    from app.services import execution_service

    store = get_store()
    user_a = _user(store, "blockeda@test.com")
    user_b = _user(store, "blockedb@test.com")
    s = get_settings()
    s.paper_portfolio_guard = True
    s.paper_max_positions = 1
    for user in (user_a, user_b):
        conn_id = execution_service.ensure_paper_connection(store, user.tenant_id)
        await execution_service.place_order(
            store, user.tenant_id, conn_id=conn_id, symbol="GBP/USD", side="buy", qty=1.0,
        )
    try:
        await auto_entry_service.run_auto_entry(
            store, candidates=[{"symbol": "EUR/USD", "asset_class": "forex", "tier": "armed"}],
        )
        assert len(auto_entry_service.blocked_for(store, user_a.tenant_id)) == 1
        assert len(auto_entry_service.blocked_for(store, user_b.tenant_id)) == 1
        assert auto_entry_service.blocked_for(store, "unknown-tenant") == []
    finally:
        s.paper_portfolio_guard = False
        s.paper_max_positions = 0


def test_api_auto_entry_status_exposes_blocked_setups(market):
    """Le champ `blocked` doit exister dans la réponse (vide ou non) : c'est le contrat d'API que
    la bannière du front consomme. Le comportement de blocage lui-même est vérifié plus haut, au
    niveau service (`test_a_ready_setup_blocked_by_the_portfolio_guard_is_explained_not_silent`)."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.post("/api/auth/register",
                    json={"email": "blockedapi@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    status = client.get("/api/signals/auto-entry", headers=h).json()
    assert "blocked" in status
    assert status["blocked"] == []
