"""RÉGRESSION : le rejeu de prix ne doit JAMAIS inventer un résultat.

Bug d'origine, constaté en production : des positions étaient clôturées « gagnantes » à des prix que
le marché n'a jamais atteints. Trois défauts se combinaient :

1. `get_ohlcv` retombait sur des bougies SYNTHÉTIQUES horodatées jusqu'à « maintenant » : elles
   passaient toutes pour postérieures à l'entrée et l'une d'elles finissait par franchir l'objectif ;
2. `iso_to_unix` renvoyait 0 en cas d'échec de parsing -> TOUT l'historique passait pour postérieur
   à l'entrée ;
3. le repli `or rows[-1:]` utilisait la dernière bougie de la série même lorsqu'elle était
   ANTÉRIEURE à l'entrée (cas permanent marchés fermés).

Symptôme observable : des ordres dont `closed_at` précédait `created_at` — clôturés avant d'exister.
"""

from __future__ import annotations

import pytest

from app.data import replay

pytestmark = pytest.mark.asyncio


def _rows(start_ts: int, prices: list[tuple[float, float]], step: int = 900) -> list[dict]:
    """Bougies (high, low) horodatées à partir de `start_ts`."""
    return [
        {"time": start_ts + i * step, "open": lo, "high": hi, "low": lo, "close": (hi + lo) / 2,
         "volume": 1000.0}
        for i, (hi, lo) in enumerate(prices)
    ]


def _patch_source(monkeypatch, rows: list[dict], real: bool = True) -> None:
    async def _fake(symbol, interval="15m", limit=500, **kw):  # noqa: ANN001
        return {"candles": rows, "source": "real" if real else "unavailable", "real": real,
                "note": ""}
    monkeypatch.setattr(replay._ohlcv, "get_ohlcv_with_source", _fake)


ENTRY_ISO = "2026-07-26T18:00:00+00:00"
ENTRY_TS = 1785088800   # correspond à ENTRY_ISO


# ---------------------------------------------------------------------------------------
# 1. Le cœur du bug : aucune conclusion sans données réelles postérieures à l'entrée
# ---------------------------------------------------------------------------------------
async def test_no_verdict_without_real_data(monkeypatch):
    """Données non réelles -> `undetermined`. C'est ce qui fabriquait les faux gagnants."""
    _patch_source(monkeypatch, _rows(ENTRY_TS, [(999.0, 0.1)] * 20), real=False)
    v = await replay.replay_outcome("JPM", "buy", 353.14, 351.66, 354.91, ENTRY_ISO)
    assert v["outcome"] == replay.UNDETERMINED
    assert "RÉELLE" in v["reason"]


async def test_candles_before_the_entry_can_never_close_a_trade(monkeypatch):
    """Marché fermé : toutes les bougies précèdent l'entrée -> position laissée OUVERTE.

    C'est exactement le cas JPM : dernière bougie le 24 juillet, trade ouvert le 26.
    L'ancien code retombait sur `rows[-1:]` et pouvait clôturer sur cette bougie.
    """
    past = _rows(ENTRY_TS - 100 * 900, [(400.0, 340.0)] * 50)   # bien au-dessus du TP
    _patch_source(monkeypatch, past)
    v = await replay.replay_outcome("JPM", "buy", 353.14, 351.66, 354.91, ENTRY_ISO)
    assert v["outcome"] == replay.UNDETERMINED
    assert "aucune bougie depuis l'ouverture" in v["reason"]
    assert v["closed_ts"] is None


async def test_unreadable_open_date_does_not_replay_all_history(monkeypatch):
    """Date d'ouverture illisible -> `undetermined`, jamais « tout l'historique compte »."""
    _patch_source(monkeypatch, _rows(ENTRY_TS, [(400.0, 340.0)] * 20))
    v = await replay.replay_outcome("JPM", "buy", 353.14, 351.66, 354.91, "pas-une-date")
    assert v["outcome"] == replay.UNDETERMINED
    assert v["closed_ts"] is None


def test_iso_to_unix_returns_none_not_zero():
    """Régression : renvoyer 0 faisait passer tout l'historique pour postérieur à l'entrée."""
    assert replay.iso_to_unix(None) is None
    assert replay.iso_to_unix("n'importe quoi") is None
    assert replay.iso_to_unix(ENTRY_ISO) == pytest.approx(ENTRY_TS)


# ---------------------------------------------------------------------------------------
# 2. Verdicts corrects quand les données existent réellement
# ---------------------------------------------------------------------------------------
async def test_target_reached_after_entry_is_a_real_win(monkeypatch):
    rows = _rows(ENTRY_TS, [(353.5, 353.0), (355.2, 353.8)])   # 2e bougie franchit 354.91
    _patch_source(monkeypatch, rows)
    v = await replay.replay_outcome("JPM", "buy", 353.14, 351.66, 354.91, ENTRY_ISO)
    assert v["outcome"] == replay.WON and v["exit_price"] == 354.91
    assert v["closed_ts"] >= ENTRY_TS, "la clôture ne peut pas précéder l'entrée"


async def test_stop_wins_when_both_touched_in_the_same_candle(monkeypatch):
    """Hypothèse prudente : on ignore l'ordre des ticks, le stop l'emporte."""
    _patch_source(monkeypatch, _rows(ENTRY_TS, [(355.5, 351.0)]))
    v = await replay.replay_outcome("JPM", "buy", 353.14, 351.66, 354.91, ENTRY_ISO)
    assert v["outcome"] == replay.LOST


async def test_open_when_nothing_is_touched(monkeypatch):
    _patch_source(monkeypatch, _rows(ENTRY_TS, [(353.5, 352.5)] * 5))
    v = await replay.replay_outcome("JPM", "buy", 353.14, 351.66, 354.91, ENTRY_ISO)
    assert v["outcome"] == replay.OPEN and v["closed_ts"] is None and v["bars"] == 5


async def test_sell_side_is_symmetric(monkeypatch):
    _patch_source(monkeypatch, _rows(ENTRY_TS, [(353.0, 350.0)]))
    v = await replay.replay_outcome("JPM", "sell", 353.14, 354.5, 351.0, ENTRY_ISO)
    assert v["outcome"] == replay.WON and v["exit_price"] == 351.0


# ---------------------------------------------------------------------------------------
# 3. Détection et quarantaine des clôtures impossibles déjà en base
# ---------------------------------------------------------------------------------------
def test_detects_a_closure_before_the_opening():
    """Le symptôme observé en production : clôturé à 17:17, ouvert à 18:13."""
    order = {"outcome": "won", "created_at": "2026-07-26T18:13:24+00:00",
             "closed_at": "2026-07-26T17:17:24+00:00", "exit_price": 354.9}
    reason = replay.is_impossible_closure(order)
    assert reason and "ne peut pas se clôturer avant d'exister" in reason


def test_healthy_orders_are_left_alone():
    assert replay.is_impossible_closure(
        {"outcome": "won", "created_at": "2026-07-20T10:00:00+00:00",
         "closed_at": "2026-07-21T10:00:00+00:00", "exit_price": 354.9}
    ) is None
    assert replay.is_impossible_closure({"outcome": "open"}) is None


async def test_quarantine_neutralises_impossible_closures():
    """Une position clôturée sur un résultat impossible ne compte ni en gain ni en perte."""
    from app.core.security import hash_password
    from app.repositories.store import get_store
    from app.services import execution_service

    store = get_store()
    tenant = store.tenants.create(name="quarantine@test.com")
    store.users.create(tenant_id=tenant.id, email="quarantine@test.com",
                       password_hash=hash_password("password123"), full_name="Q")
    store.records.put(execution_service.ORDER, "bad-1", {
        "symbol": "JPM", "side": "buy", "mode": "paper", "qty": 10, "entry": 353.14,
        "stop_loss": 351.66, "take_profit": 354.91, "outcome": "won", "exit_price": 354.91,
        "realized_pnl": 120.0, "created_at": "2026-07-26T18:13:24+00:00",
        "closed_at": "2026-07-26T17:17:24+00:00",
    }, tenant_id=tenant.id)

    report = execution_service.quarantine_impossible_closures(store, tenant.id)
    assert report["count"] == 1
    bad = store.records.get(execution_service.ORDER, "bad-1")
    assert bad["outcome"] == "invalid"
    assert bad["realized_pnl"] == 0.0
    assert bad["realized_pnl_before_invalidation"] == 120.0
    assert "avant d'exister" in bad["invalid_reason"]

    snap = await execution_service.positions_snapshot(store, tenant.id)
    assert snap["wins"] == 0 and snap["losses"] == 0
    assert snap["realized_pnl"] == 0.0
    assert snap["invalid"] == 1


async def test_check_order_outcome_keeps_the_position_open_when_undetermined(monkeypatch):
    """Sans données réelles, la position reste OUVERTE au lieu d'être clôturée au hasard."""
    from app.core.security import hash_password
    from app.repositories.store import get_store
    from app.services import execution_service

    _patch_source(monkeypatch, [], real=False)
    store = get_store()
    tenant = store.tenants.create(name="undet@test.com")
    store.users.create(tenant_id=tenant.id, email="undet@test.com",
                       password_hash=hash_password("password123"), full_name="U")
    store.records.put(execution_service.ORDER, "ord-1", {
        "symbol": "JPM", "side": "buy", "mode": "paper", "qty": 10, "entry": 353.14,
        "stop_loss": 351.66, "take_profit": 354.91, "created_at": ENTRY_ISO,
    }, tenant_id=tenant.id)

    res = await execution_service.check_order_outcome(store, tenant.id, "ord-1")
    assert res["outcome"] == "open" and res["undetermined"] is True
    # Rien n'a été persisté comme clôture.
    assert store.records.get(execution_service.ORDER, "ord-1").get("outcome") is None


# ---------------------------------------------------------------------------------------
# 4. L'issue est celle du RÉSULTAT, pas celle du niveau touché
# ---------------------------------------------------------------------------------------
async def test_a_secured_stop_closes_the_trade_as_a_WIN(monkeypatch):
    """RÉGRESSION : « ❌ Perdu · +198,71 » sur la même carte.

    Cas réel (GOOGL, 29/07/2026) : achat à 334,355, stop remonté à 338,98 par la sécurisation +2R,
    puis touché. Le rejeu répond à une question de PRIX (« quel niveau en premier ? ») et étiquette
    mécaniquement le stop en `lost` — alors que ce stop-là était DU CÔTÉ DU PROFIT et que la
    position a rapporté +198,71. L'issue doit suivre le P&L, pas le nom du niveau."""
    from app.core.security import hash_password
    from app.repositories.store import get_store
    from app.services import execution_service

    # Le prix monte et vient chercher le stop de sécurisation (338,98), sous l'objectif (339,16).
    _patch_source(monkeypatch, _rows(ENTRY_TS + 900, [(339.00, 338.90)]))
    store = get_store()
    tenant = store.tenants.create(name="secured@test.com")
    store.users.create(tenant_id=tenant.id, email="secured@test.com",
                       password_hash=hash_password("password123"), full_name="S")
    store.records.put(execution_service.ORDER, "ord-sec", {
        "symbol": "GOOGL", "side": "buy", "mode": "paper", "qty": 42.93634317,
        "entry": 334.355, "stop_loss": 338.98305854, "take_profit": 339.16097073,
        "original_stop_loss": 332.04, "profit_secured": True, "secured_at_r": 2,
        "created_at": ENTRY_ISO,
    }, tenant_id=tenant.id)

    res = await execution_service.check_order_outcome(store, tenant.id, "ord-sec")
    assert res["realized_pnl"] > 0
    assert res["outcome"] == "won", "un trade clôturé en profit n'est jamais 'perdu'"
    assert "sécurisation" in res["close_reason"], "le motif doit dire QUEL stop a fermé la position"

    snap = await execution_service.positions_snapshot(store, tenant.id)
    row = next(p for p in snap["positions"] if p["id"] == "ord-sec")
    assert snap["wins"] == 1 and snap["losses"] == 0
    assert row["pips"] > 0, "pips gagnés, signés dans le sens du trade"
    assert row["opened_at"] == ENTRY_ISO, "l'heure d'entrée doit être exposée sur chaque carte"


async def test_a_real_stop_below_the_entry_is_still_a_loss(monkeypatch):
    """Contrepartie indispensable : un stop qui coûte de l'argent reste une PERTE.

    La règle « l'issue suit le P&L » ne doit pas se transformer en machine à gagnants."""
    from app.core.security import hash_password
    from app.repositories.store import get_store
    from app.services import execution_service

    _patch_source(monkeypatch, _rows(ENTRY_TS + 900, [(334.40, 331.90)]))
    store = get_store()
    tenant = store.tenants.create(name="loss@test.com")
    store.users.create(tenant_id=tenant.id, email="loss@test.com",
                       password_hash=hash_password("password123"), full_name="L")
    store.records.put(execution_service.ORDER, "ord-loss", {
        "symbol": "GOOGL", "side": "buy", "mode": "paper", "qty": 10.0,
        "entry": 334.355, "stop_loss": 332.04, "take_profit": 339.16,
        "created_at": ENTRY_ISO,
    }, tenant_id=tenant.id)

    res = await execution_service.check_order_outcome(store, tenant.id, "ord-loss")
    assert res["outcome"] == "lost" and res["realized_pnl"] < 0
    assert res["close_reason"] == "stop touché"

    snap = await execution_service.positions_snapshot(store, tenant.id)
    row = next(p for p in snap["positions"] if p["id"] == "ord-loss")
    assert row["pips"] < 0, "pips perdus : le signe porte l'information"
