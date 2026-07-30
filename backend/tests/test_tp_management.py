"""Tests de la gestion TP1 -> TP2 en exécution (compte démo).

La règle : quand TP1 est touché, on ne sort pas mécaniquement. Si le momentum 15 min confirme
encore la continuation, le stop remonte à 80 % du chemin parcouru et la position part chercher TP2.
Sinon on prend le gain. Dans tous les cas le stop ne recule jamais, et la sécurisation +2R continue
de s'appliquer en parallèle.
"""

from __future__ import annotations

import pytest

from app.domain.indicators import Candle
from app.repositories.store import get_store
from app.services import execution_service

pytestmark = pytest.mark.asyncio

ORDER = "order"


def _c(o, h, low, c, v=1000.0):
    return Candle(o, h, low, c, v)


def _tenant(store, email="tp@test.com"):
    from app.core.security import hash_password

    tenant = store.tenants.create(name=email)
    return store.users.create(
        tenant_id=tenant.id, email=email, password_hash=hash_password("password123"),
        full_name="Compte démo",
    )


def _order(store, tenant_id, **over) -> dict:
    rec = {
        "id": over.pop("id", "ord-1"), "mode": "paper", "symbol": "EUR/USD", "side": "buy",
        "qty": 1000.0, "entry": 1.1000, "filled_price": 1.1000, "stop_loss": 1.0950,
        "take_profit_1": 1.1100, "take_profit_2": 1.1200, "tp1_lock_stop": 1.1080,
        "initial_risk": 0.0050, "outcome": None, "tenant_id": tenant_id,
    }
    rec.update(over)
    return store.records.put(ORDER, rec["id"], rec, tenant_id=tenant_id)


def _patch_price(monkeypatch, price: float) -> None:
    async def fake_price(symbol):
        return price

    monkeypatch.setattr(execution_service, "_reference_price", fake_price)


def _patch_momentum(monkeypatch, ok: bool, reasons=None) -> None:
    from app.domain import exits

    def fake(candles, direction):
        return {"ok": ok, "reasons": reasons or ([] if ok else ["momentum retombé"]),
                "rsi": 60.0, "macd_hist": 0.1, "choch": None}

    monkeypatch.setattr(exits, "momentum_still_supports", fake)

    async def fake_load(symbol, interval, limit):
        return [_c(1.10, 1.101, 1.099, 1.1005) for _ in range(60)], "real"

    from app.services import playbook_service

    monkeypatch.setattr(playbook_service, "_load", fake_load)


async def test_tp1_reached_with_momentum_locks_eighty_percent(monkeypatch):
    """Momentum confirmé : le stop monte au verrou et la position continue vers TP2."""
    store = get_store()
    user = _tenant(store)
    _order(store, user.tenant_id)
    _patch_price(monkeypatch, 1.1105)          # TP1 (1,1100) dépassé
    _patch_momentum(monkeypatch, ok=True)

    moved = await execution_service.manage_tp_progression(store)
    assert moved == 1
    rec = store.records.get(ORDER, "ord-1")
    assert rec["stop_loss"] == 1.1080          # 80 % du chemin verrouillé
    assert rec["tp1_reached"] is True
    assert rec["tp_lock_rule"] == "tp1_80pct"
    assert rec["original_stop_loss"] == 1.0950


async def test_tp1_reached_without_momentum_takes_the_gain(monkeypatch):
    """Momentum retombé : on ne prolonge pas le risque, le stop vient sur TP1."""
    store = get_store()
    user = _tenant(store)
    _order(store, user.tenant_id)
    _patch_price(monkeypatch, 1.1105)
    _patch_momentum(monkeypatch, ok=False)

    moved = await execution_service.manage_tp_progression(store)
    assert moved == 1
    rec = store.records.get(ORDER, "ord-1")
    assert rec["stop_loss"] == 1.1100          # sur TP1
    assert rec["tp_lock_rule"] == "tp1_exit"
    assert rec["momentum_check"]["ok"] is False
    assert rec["momentum_check"]["reasons"]


async def test_nothing_happens_before_tp1(monkeypatch):
    store = get_store()
    user = _tenant(store)
    _order(store, user.tenant_id)
    _patch_price(monkeypatch, 1.1050)          # sous TP1
    _patch_momentum(monkeypatch, ok=True)

    assert await execution_service.manage_tp_progression(store) == 0
    assert store.records.get(ORDER, "ord-1")["stop_loss"] == 1.0950


async def test_the_stop_never_moves_backwards(monkeypatch):
    """Un stop déjà sécurisé plus haut que le verrou ne doit pas redescendre."""
    store = get_store()
    user = _tenant(store)
    _order(store, user.tenant_id, stop_loss=1.1090)     # déjà au-dessus du verrou (1,1080)
    _patch_price(monkeypatch, 1.1105)
    _patch_momentum(monkeypatch, ok=True)

    await execution_service.manage_tp_progression(store)
    assert store.records.get(ORDER, "ord-1")["stop_loss"] == 1.1090


async def test_a_position_without_a_second_objective_is_left_alone(monkeypatch):
    """Sans TP2, il n'y a rien à arbitrer : le comportement historique reste inchangé."""
    store = get_store()
    user = _tenant(store)
    _order(store, user.tenant_id, take_profit_2=None)
    _patch_price(monkeypatch, 1.1105)
    _patch_momentum(monkeypatch, ok=True)

    assert await execution_service.manage_tp_progression(store) == 0


async def test_a_position_is_arbitrated_only_once(monkeypatch):
    store = get_store()
    user = _tenant(store)
    _order(store, user.tenant_id)
    _patch_price(monkeypatch, 1.1105)
    _patch_momentum(monkeypatch, ok=True)

    assert await execution_service.manage_tp_progression(store) == 1
    assert await execution_service.manage_tp_progression(store) == 0


async def test_the_sell_side_mirrors(monkeypatch):
    store = get_store()
    user = _tenant(store)
    _order(store, user.tenant_id, side="sell", entry=1.1000, filled_price=1.1000,
           stop_loss=1.1050, take_profit_1=1.0900, take_profit_2=1.0800, tp1_lock_stop=1.0920)
    _patch_price(monkeypatch, 1.0895)          # TP1 dépassé vers le bas
    _patch_momentum(monkeypatch, ok=True)

    assert await execution_service.manage_tp_progression(store) == 1
    assert store.records.get(ORDER, "ord-1")["stop_loss"] == 1.0920


async def test_the_feature_can_be_switched_off(monkeypatch):
    from app.core.config import get_settings

    s = get_settings()
    store = get_store()
    user = _tenant(store)
    _order(store, user.tenant_id)
    _patch_price(monkeypatch, 1.1105)
    _patch_momentum(monkeypatch, ok=True)

    s.playbook_tp2_management = False
    try:
        assert await execution_service.manage_tp_progression(store) == 0
    finally:
        s.playbook_tp2_management = True
