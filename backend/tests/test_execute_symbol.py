"""Tests de l'ouverture démo PAR SYMBOLE (bouton d'une carte « trade du jour »).

Vérifie que la stratégie est RECALCULÉE au moment de l'appel (jamais de niveaux transmis par le
client), qu'un setup exécutable s'ouvre avec ses vrais niveaux, et qu'un setup non exécutable
explique pourquoi sans rien ouvrir.
"""

from __future__ import annotations

import pytest

from tests.test_playbook import (
    _END, _build, _h1, _m15_with_pullback_entry, _no_trigger_15m, _tenant, _tf,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def playbook_on(monkeypatch):
    from app.core.config import get_settings
    from app.data import markets
    from app.services import playbook_service

    s = get_settings()
    s.playbook_enabled = True
    playbook_service.clear_cache()

    # end_close=_END aligne le 15 min sur le prix courant du 4h/journalier/mensuel — sans cet
    # alignement, le stop structurel (recalé sur le 4h quand le 15 min est trop serré) se calcule
    # entre deux échelles de prix incohérentes et le setup est refusé pour un stop artificiellement
    # large.
    series = {
        "1M": _tf("monthly"), "1d": _tf("daily"), "4h": _tf("h4"), "1h": _h1(True),
        "15m": _m15_with_pullback_entry(True, end_close=_END),
    }
    # La garde d'heures de marché est testée dans test_auto_entry : ici on veut vérifier
    # l'ouverture par symbole, indépendamment du jour où la suite tourne.
    previous_gate = s.playbook_trade_only_when_open
    s.playbook_trade_only_when_open = False

    async def _load(symbol, interval="1h", limit=200, **kw):  # noqa: ANN001
        return series.get(interval, series["4h"])

    monkeypatch.setattr(markets, "load_candles", _load)
    monkeypatch.setattr(markets, "data_source", lambda symbol: "real")
    yield s
    s.playbook_enabled = False
    s.playbook_trade_only_when_open = previous_gate
    playbook_service.clear_cache()


async def test_execute_symbol_opens_the_recomputed_setup(playbook_on):
    """La stratégie est recalculée : le trade ouvert reflète les niveaux ACTUELS, pas ceux du client."""
    from app.repositories.store import get_store
    from app.services import execution_service

    store = get_store()
    user = _tenant(store, "onesym@test.com")
    setup = _build()
    assert setup.ready, setup.reasons

    report = await execution_service.execute_playbook_symbol(store, user.tenant_id, "EUR/USD")
    assert report["symbol"] == "EUR/USD"
    assert len(report["opened"]) == 1
    o = report["opened"][0]
    assert o["stop_loss"] == setup.stop_loss and o["take_profit"] == setup.take_profit_1
    assert o["side"] == ("buy" if setup.direction == "BUY" else "sell")


async def test_execute_symbol_explains_when_not_ready(playbook_on, monkeypatch):
    """Si le déclencheur 15 min n'est plus actif au moment du clic, rien n'est ouvert."""
    from app.data import markets
    from app.repositories.store import get_store
    from app.services import execution_service, playbook_service

    store = get_store()
    user = _tenant(store, "onesym2@test.com")

    series = {
        "1M": _tf("monthly"), "1d": _tf("daily"), "4h": _tf("h4"),
        "15m": _no_trigger_15m(price=_tf("h4")[-1].close),
    }

    async def _load(symbol, interval="1h", limit=200, **kw):  # noqa: ANN001
        return series.get(interval, series["4h"])

    monkeypatch.setattr(markets, "load_candles", _load)
    playbook_service.clear_cache()

    report = await execution_service.execute_playbook_symbol(store, user.tenant_id, "GBP/USD")
    assert report["opened"] == []
    assert "GBP/USD" in report["summary"]
    assert "n'est pas exécutable" in report["summary"]


async def test_execute_symbol_never_opens_twice(playbook_on):
    """Cliquer deux fois de suite n'empile pas la même position."""
    from app.repositories.store import get_store
    from app.services import execution_service

    store = get_store()
    user = _tenant(store, "onesym3@test.com")

    first = await execution_service.execute_playbook_symbol(store, user.tenant_id, "EUR/USD")
    assert len(first["opened"]) == 1

    second = await execution_service.execute_playbook_symbol(store, user.tenant_id, "EUR/USD")
    assert second["opened"] == []
    assert any("déjà ouverte" in s["reason"] for s in second["skipped"])


# ---------------------------------------------------------------------------------------
# RÉGRESSION (29/07/2026) : jamais de position ouverte à un prix FANTÔME (entrée à 0)
# ---------------------------------------------------------------------------------------
# Cas réel observé : LINK/USDT vendu « @ 0 » -> pips affichés à -83 848 (le calcul de pips divise
# une distance de prix réelle par un pip fabriqué sur une entrée nulle). Cause : le broker papier
# remplissait l'ordre au prix 0.0 quand `load_candles` ne renvoyait rien, au lieu de refuser.
async def test_paper_broker_refuses_to_fill_without_real_candles(monkeypatch):
    """Le cœur du bug : plus aucun repli à 0.0 quand aucune bougie n'est disponible."""
    from app.data import markets
    from app.execution.paper import PaperBroker

    async def _empty(symbol, interval="1h", limit=200, **kw):  # noqa: ANN001
        return []
    monkeypatch.setattr(markets, "load_candles", _empty)

    with pytest.raises(RuntimeError, match="[Aa]ucun prix"):
        await PaperBroker().place_order("LINK/USDT", "sell", 10.0)


async def test_place_order_reports_a_clean_refusal_not_a_phantom_position(monkeypatch):
    """`place_order` traduit l'échec du broker en `ExecutionError` motivée — jamais une position
    silencieusement ouverte à un prix inventé."""
    from app.data import markets
    from app.repositories.store import get_store
    from app.services import execution_service

    async def _empty(symbol, interval="1h", limit=200, **kw):  # noqa: ANN001
        return []
    monkeypatch.setattr(markets, "load_candles", _empty)

    store = get_store()
    user = _tenant(store, "phantom@test.com")
    conn_id = execution_service.ensure_paper_connection(store, user.tenant_id)

    with pytest.raises(execution_service.ExecutionError, match="LINK/USDT"):
        await execution_service.place_order(
            store, user.tenant_id, conn_id=conn_id, symbol="LINK/USDT", side="sell", qty=10.0,
            stop_loss=8.38, take_profit=7.80,
        )
    # Rien n'a été persisté : le refus n'a laissé aucune trace de position à corriger après coup.
    assert execution_service.list_orders(store, user.tenant_id) == []


async def test_a_bad_symbol_does_not_abort_the_rest_of_the_batch(playbook_on, monkeypatch):
    """`execute_playbook_trades` n'attrape que `ExecutionError` : si le broker levait autre chose,
    UN symbole en échec de prix aurait interrompu toute la passe au lieu de continuer sur les
    suivants. Le broker papier doit donc toujours échouer en `ExecutionError` via `place_order`,
    jamais en exception brute."""
    from app.data import markets
    from app.repositories.store import get_store
    from app.services import execution_service

    store = get_store()
    user = _tenant(store, "batch@test.com")
    real_load = markets.load_candles

    async def _flaky(symbol, interval="1h", limit=200, **kw):  # noqa: ANN001
        if symbol == "EUR/USD" and interval == "1h":
            return []  # ce SEUL symbole échoue au moment du remplissage
        return await real_load(symbol, interval=interval, limit=limit)
    monkeypatch.setattr(markets, "load_candles", _flaky)

    # Niveaux GBP/USD alignés sur le prix de référence RÉEL de la fixture (le repli `_load` sert la
    # même série 1h à tout symbole, dont le dernier close vaut ~1,085) : un prix d'entrée arbitraire
    # aurait été refusé par le garde-fou « prix déjà sorti de la zone », un motif différent de celui
    # qu'on veut isoler ici.
    picks = [
        {"symbol": "EUR/USD", "direction": "BUY", "tier": "ready",
         "entry": 1.1, "stop_loss": 1.09, "take_profit_1": 1.12, "take_profit_2": 1.14},
        {"symbol": "GBP/USD", "direction": "BUY", "tier": "ready",
         "entry": 1.085, "stop_loss": 1.07, "take_profit_1": 1.10, "take_profit_2": 1.12},
    ]
    report = await execution_service.execute_playbook_trades(store, user.tenant_id, picks=picks)
    assert any("EUR/USD" in s["symbol"] for s in report["skipped"]), \
        "le refus doit être journalisé, pas planter la passe"
    assert any(o["symbol"] == "GBP/USD" for o in report["opened"]), \
        "GBP/USD ne devait PAS être emporté par l'échec d'EUR/USD dans la même passe"


def test_api_execute_symbol_endpoint(playbook_on):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": "onesymapi@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    resp = client.post("/api/execution/playbook/execute-symbol/EUR%2FUSD", headers=h)
    assert resp.status_code == 201
    body = resp.json()
    assert body["symbol"] == "EUR/USD"
    assert isinstance(body["opened"], list)


# ---------------------------------------------------------------------------------------
# Cache court du prix de référence (mesuré le 28/07/2026 : positions_snapshot mettait 10-22 s
# à répondre sous charge, un fournisseur gratuit comme Yahoo devenant plus lent quand on le
# sollicite trop souvent pour le même symbole en quelques secondes)
# ---------------------------------------------------------------------------------------
async def test_reference_price_is_cached_for_a_short_time(monkeypatch):
    from app.data import markets
    from app.domain.indicators import Candle
    from app.services import execution_service

    calls = {"n": 0}

    async def fake_load(symbol, interval="1h", limit=60, **kw):  # noqa: ANN001
        calls["n"] += 1
        return [Candle(1.10, 1.101, 1.099, 1.10 + calls["n"] * 0.001, 1000.0)]

    monkeypatch.setattr(markets, "load_candles", fake_load)
    execution_service._price_cache.clear()

    first = await execution_service._reference_price("EUR/USD")
    second = await execution_service._reference_price("EUR/USD")
    assert first == second, "le deuxième appel doit servir le prix en cache, pas en refaire un"
    assert calls["n"] == 1


async def test_reference_price_cache_expires(monkeypatch):
    import time as time_mod

    from app.data import markets
    from app.domain.indicators import Candle
    from app.services import execution_service

    execution_service._price_cache.clear()
    execution_service._price_cache["EUR/USD"] = (time_mod.monotonic() - 1, 1.2345)  # déjà expiré

    async def fake_load(symbol, interval="1h", limit=60, **kw):  # noqa: ANN001
        return [Candle(9.0, 9.0, 9.0, 9.999, 1000.0)]

    monkeypatch.setattr(markets, "load_candles", fake_load)
    price = await execution_service._reference_price("EUR/USD")
    assert price == 9.999, "un cache expiré ne doit jamais être servi"


async def test_reference_price_cache_is_per_symbol(monkeypatch):
    from app.data import markets
    from app.domain.indicators import Candle
    from app.services import execution_service

    execution_service._price_cache.clear()
    prices = {"EUR/USD": 1.10, "GBP/USD": 1.27}

    async def fake_load(symbol, interval="1h", limit=60, **kw):  # noqa: ANN001
        return [Candle(1.0, 1.0, 1.0, prices[symbol], 1000.0)]

    monkeypatch.setattr(markets, "load_candles", fake_load)

    assert await execution_service._reference_price("EUR/USD") == 1.10
    assert await execution_service._reference_price("GBP/USD") == 1.27


# ---------------------------------------------------------------------------------------
# Modification MANUELLE du stop / de l'objectif sur une position papier ouverte
#
# `_reference_price` retombe sur `markets.load_candles` dès que son cache est vide : on fixe donc
# le prix au niveau du CONNECTEUR (pas de `_reference_price` directement), ce qui rend déterministe
# à la fois le prix de REMPLISSAGE (`PaperBroker.place_order`, qui appelle `load_candles` lui aussi)
# et le prix COURANT lu par `update_order_levels`.
# ---------------------------------------------------------------------------------------
def _fix_price(monkeypatch, value: float) -> None:
    from app.data import markets
    from app.domain.indicators import Candle
    from app.services import execution_service

    async def fake_load(symbol, interval="1h", limit=60, **kw):  # noqa: ANN001
        return [Candle(value, value, value, value, 1000.0)]

    monkeypatch.setattr(markets, "load_candles", fake_load)
    execution_service._price_cache.clear()


def _store_for_execution():
    from app.repositories.store import get_store
    return get_store()


async def _open_googl_buy(store, tenant_id: str, *, qty: float = 1.0):
    from app.services import execution_service

    conn_id = execution_service.ensure_paper_connection(store, tenant_id)
    return await execution_service.place_order(
        store, tenant_id, conn_id=conn_id, symbol="GOOGL", side="buy", qty=qty,
        stop_loss=95.0, take_profit=110.0,
    )


async def test_update_order_levels_changes_stop_and_target(monkeypatch):
    from app.services import execution_service
    from tests.test_playbook import _tenant

    store = _store_for_execution()
    user = _tenant(store, "edit1@test.com")
    _fix_price(monkeypatch, 100.0)   # prix figé et connu pour un calcul de niveaux prévisible
    order = await _open_googl_buy(store, user.tenant_id, qty=2.0)
    assert order["entry"] == 100.0

    updated = await execution_service.update_order_levels(
        store, user.tenant_id, order["id"], stop_loss=97.0, take_profit=115.0,
    )
    assert updated["stop_loss"] == 97.0 and updated["take_profit"] == 115.0
    # Risque et gain visé RECALCULÉS sur les nouveaux niveaux : (100-97)*2=6, (115-100)*2=30.
    assert updated["risk_amount"] == 6.0
    assert updated["potential_profit"] == 30.0
    assert updated["risk_reward"] == 5.0
    # Le risque D'ORIGINE (ce que vaut 1R pour la sécurisation +2R) ne bouge PAS.
    assert updated["initial_risk"] == 5.0
    assert updated["levels_edited_manually"] is True


async def test_update_order_levels_rejects_wrong_direction(monkeypatch):
    """L'OBJECTIF garde sa contrainte de sens : il doit rester du côté du gain.

    Un « objectif » placé dans la perte n'est pas un objectif, c'est un stop — et il y a déjà un
    champ pour ça. (Le STOP, lui, peut désormais passer des deux côtés de l'entrée : c'est le
    principe de la sécurisation, cf. `test_the_stop_can_be_secured_on_the_profit_side`.)
    """
    from app.services import execution_service
    from tests.test_playbook import _tenant

    store = _store_for_execution()
    user = _tenant(store, "edit2@test.com")
    _fix_price(monkeypatch, 100.0)
    order = await _open_googl_buy(store, user.tenant_id)
    with pytest.raises(execution_service.ExecutionError, match="doit rester AU-DESSUS de l'entrée"):
        await execution_service.update_order_levels(
            store, user.tenant_id, order["id"], take_profit=95.0)


async def test_update_order_levels_rejects_a_stop_already_hit(monkeypatch):
    """Le prix courant a déjà franchi le nouveau stop : la modification est refusée, pas silencieuse."""
    from app.services import execution_service
    from tests.test_playbook import _tenant

    store = _store_for_execution()
    user = _tenant(store, "edit3@test.com")
    _fix_price(monkeypatch, 100.0)
    order = await _open_googl_buy(store, user.tenant_id)
    # Le prix a bougé depuis l'entrée (100 -> 96) : un nouveau stop à 97, pourtant valide
    # directionnellement (< entrée), est déjà en dessous du prix courant.
    _fix_price(monkeypatch, 96.0)
    with pytest.raises(execution_service.ExecutionError, match="a déjà franchi ce niveau"):
        await execution_service.update_order_levels(store, user.tenant_id, order["id"], stop_loss=97.0)


async def test_update_order_levels_rejects_on_closed_position(monkeypatch):
    from app.services import execution_service
    from tests.test_playbook import _tenant

    store = _store_for_execution()
    user = _tenant(store, "edit4@test.com")
    _fix_price(monkeypatch, 100.0)
    order = await _open_googl_buy(store, user.tenant_id)
    await execution_service.close_order_manual(store, user.tenant_id, order["id"])
    with pytest.raises(execution_service.ExecutionError, match="déjà clôturée"):
        await execution_service.update_order_levels(store, user.tenant_id, order["id"], stop_loss=90.0)


async def test_a_position_that_can_be_priced_can_always_be_closed(monkeypatch):
    """Ce qu'on sait VALORISER, on doit pouvoir le FERMER — même source de prix des deux côtés.

    La clôture manuelle chargeait des bougies 15 MIN, alors que le remplissage à l'ouverture, le
    P&L latent affiché et la sécurisation du stop passent tous par `_reference_price` (1 h). Quand
    le fournisseur limitait le débit sur le 15 min mais servait le 1 h, la position restait
    parfaitement valorisée à l'écran et devenait pourtant IMPOSSIBLE à clôturer : le bouton
    « Clôturer maintenant » échouait sans rien fermer.

    On simule exactement cela : le 1 h répond, le 15 min non.
    """
    from app.data import markets
    from app.domain.indicators import Candle
    from app.services import execution_service
    from tests.test_playbook import _tenant

    store = _store_for_execution()
    user = _tenant(store, "close-tf@test.com")

    async def _only_hourly(symbol, interval="1h", limit=200, **kw):  # noqa: ANN001
        if interval == "15m":
            return []                      # débit limité sur cette unité de temps
        return [Candle(100.0, 100.0, 100.0, 100.0, 1000.0) for _ in range(60)]

    monkeypatch.setattr(markets, "load_candles", _only_hourly)
    monkeypatch.setattr(markets, "is_real", lambda symbol: True)
    execution_service._price_cache.clear()

    order = await _open_googl_buy(store, user.tenant_id)
    closed = await execution_service.close_order_manual(store, user.tenant_id, order["id"])

    assert closed["outcome"] in execution_service.FINAL_OUTCOMES, "la position devait se fermer"
    assert closed["exit_price"] == 100.0
    assert closed.get("closed_manually") is True


async def test_closing_without_a_real_price_is_reported_as_unavailable(monkeypatch):
    """Sans prix réel, on refuse — mais avec une erreur qui dit « plus tard », pas « introuvable ».

    L'API traduit `MarketDataUnavailable` en 503 : la position EXISTE, c'est la cotation qui manque.
    Le 404 d'avant se lisait comme un ordre inexistant et rendait le refus incompréhensible.
    """
    from app.data import markets
    from app.services import execution_service
    from tests.test_playbook import _tenant

    store = _store_for_execution()
    user = _tenant(store, "close-nodata@test.com")
    _fix_price(monkeypatch, 100.0)
    order = await _open_googl_buy(store, user.tenant_id)

    async def _nothing(symbol, interval="1h", limit=200, **kw):  # noqa: ANN001
        return []

    monkeypatch.setattr(markets, "load_candles", _nothing)
    monkeypatch.setattr(markets, "is_real", lambda symbol: False)
    execution_service._price_cache.clear()

    with pytest.raises(execution_service.MarketDataUnavailable):
        await execution_service.close_order_manual(store, user.tenant_id, order["id"])
    # La position reste OUVERTE : on ne ferme jamais sur un prix supposé.
    still = store.records.get(execution_service.ORDER, order["id"])
    assert still.get("outcome") not in execution_service.FINAL_OUTCOMES


async def test_the_stop_can_be_secured_on_the_profit_side(monkeypatch):
    """Le stop doit pouvoir passer DU CÔTÉ DU PROFIT, des deux sens — c'est la sécurisation.

    La validation exigeait qu'il reste du côté perdant de l'entrée. Elle interdisait donc à
    l'utilisateur exactement ce que le moteur fait tout seul : `secure_open_profits` remonte le stop
    sur +2R, et `manage_tp_progression` le verrouille à 80 % après TP1. Le message d'erreur
    affirmait une règle que le système lui-même ne respecte pas.
    """
    from app.services import execution_service
    from tests.test_playbook import _tenant

    store = _store_for_execution()
    user = _tenant(store, "secure-manuel@test.com")

    # ACHAT : le prix est monté (sans atteindre l'objectif), on verrouille un stop AU-DESSUS
    # de l'entrée — la position devient nécessairement gagnante.
    _fix_price(monkeypatch, 100.0)
    achat = await _open_googl_buy(store, user.tenant_id)
    _fix_price(monkeypatch, 102.0)
    maj = await execution_service.update_order_levels(
        store, user.tenant_id, achat["id"], stop_loss=101.0)
    assert maj["stop_loss"] == 101.0, "un achat doit pouvoir verrouiller un stop au-dessus de l'entrée"
    assert maj["stop_loss"] > maj["entry"]

    # VENTE : le prix est descendu, on verrouille un stop SOUS l'entrée.
    vente = store.records.put(execution_service.ORDER, "vente-sec", {
        "id": "vente-sec", "mode": "paper", "symbol": "GOOGL", "side": "sell", "qty": 10.0,
        "entry": 100.0, "filled_price": 100.0, "stop_loss": 105.0, "take_profit": 90.0,
        "initial_risk": 5.0, "outcome": None, "tenant_id": user.tenant_id,
    }, tenant_id=user.tenant_id)
    _fix_price(monkeypatch, 96.0)
    maj = await execution_service.update_order_levels(
        store, user.tenant_id, vente["id"], stop_loss=98.0)
    assert maj["stop_loss"] == 98.0, "une vente doit pouvoir verrouiller un stop sous l'entrée"
    assert maj["stop_loss"] < maj["entry"]


async def test_a_stop_already_crossed_is_still_refused(monkeypatch):
    """La seule contrainte qui compte reste : le prix ne doit pas avoir déjà franchi le stop.

    Sinon la position se clôturerait au passage de surveillance suivant, sans que rien ne
    l'explique à l'écran.
    """
    from app.services import execution_service
    from tests.test_playbook import _tenant

    store = _store_for_execution()
    user = _tenant(store, "stop-franchi@test.com")
    _fix_price(monkeypatch, 100.0)
    ordre = await _open_googl_buy(store, user.tenant_id)
    _fix_price(monkeypatch, 96.0)
    with pytest.raises(execution_service.ExecutionError, match="a déjà franchi ce niveau"):
        await execution_service.update_order_levels(
            store, user.tenant_id, ordre["id"], stop_loss=97.0)


async def test_close_all_closes_every_open_position(monkeypatch):
    """« Tout clôturer » ferme tout — et laisse ouvert ce qu'il ne sait pas valoriser."""
    from app.services import execution_service
    from tests.test_playbook import _tenant

    store = _store_for_execution()
    user = _tenant(store, "close-all@test.com")
    _fix_price(monkeypatch, 100.0)
    for _ in range(3):
        await _open_googl_buy(store, user.tenant_id)

    ouvertes = [o for o in execution_service.list_orders(store, user.tenant_id)
                if o.get("outcome") not in execution_service.FINAL_OUTCOMES]
    assert len(ouvertes) == 3, "état de départ du test"

    _fix_price(monkeypatch, 110.0)
    res = await execution_service.close_all_open(store, user.tenant_id)

    assert len(res["closed"]) == 3
    assert res["failed"] == []
    assert res["realized_pnl"] > 0, "les trois positions étaient en gain"
    restantes = [o for o in execution_service.list_orders(store, user.tenant_id)
                 if o.get("outcome") not in execution_service.FINAL_OUTCOMES]
    assert restantes == [], "plus aucune position ouverte"


async def test_close_all_never_closes_without_a_real_price(monkeypatch):
    """Sans prix de marché réel, la position reste OUVERTE — même dans un ordre groupé."""
    from app.data import markets
    from app.services import execution_service
    from tests.test_playbook import _tenant

    store = _store_for_execution()
    user = _tenant(store, "close-all-nodata@test.com")
    _fix_price(monkeypatch, 100.0)
    await _open_googl_buy(store, user.tenant_id)

    async def _rien(symbol, interval="1h", limit=200, **kw):  # noqa: ANN001
        return []

    monkeypatch.setattr(markets, "load_candles", _rien)
    monkeypatch.setattr(markets, "is_real", lambda symbol: False)
    execution_service._price_cache.clear()

    res = await execution_service.close_all_open(store, user.tenant_id)
    assert res["closed"] == []
    assert len(res["failed"]) == 1, "l'échec doit être rendu, pas silencieux"
    assert "impossible" in res["failed"][0]["reason"].lower()
    restantes = [o for o in execution_service.list_orders(store, user.tenant_id)
                 if o.get("outcome") not in execution_service.FINAL_OUTCOMES]
    assert len(restantes) == 1, "on ne ferme jamais sur un prix supposé"


async def test_update_order_levels_requires_at_least_one_field():
    from app.services import execution_service
    from tests.test_playbook import _tenant

    store = _store_for_execution()
    user = _tenant(store, "edit5@test.com")
    with pytest.raises(execution_service.ExecutionError, match="au moins un"):
        await execution_service.update_order_levels(store, user.tenant_id, "unknown-order-id")


def test_api_update_order_levels_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    _fix_price(monkeypatch, 100.0)
    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": "editapi@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    open_resp = client.post("/api/execution/brokers", json={"broker": "paper", "mode": "paper"}, headers=h)
    conn_id = open_resp.json()["id"]
    order_resp = client.post("/api/execution/orders", json={
        "conn_id": conn_id, "symbol": "GOOGL", "side": "buy", "qty": 1.0,
        "stop_loss": 95.0, "take_profit": 110.0,
    }, headers=h)
    order_id = order_resp.json()["id"]

    edit = client.post(f"/api/execution/orders/{order_id}/levels",
                       json={"stop_loss": 97.0}, headers=h)
    assert edit.status_code == 200
    assert edit.json()["stop_loss"] == 97.0

    bad = client.post(f"/api/execution/orders/{order_id}/levels",
                      json={"stop_loss": 200.0}, headers=h)
    assert bad.status_code == 400
