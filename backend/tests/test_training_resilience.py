"""ROBUSTESSE DE L'ENTRAÎNEMENT sur une exploitation de plusieurs semaines sans surveillance.

Ce que ces tests protègent : l'APPRENTISSAGE ACCUMULÉ des agents. Il est facile de le détruire
sans s'en apercevoir — il suffit d'une nuit où les fournisseurs limitent leur débit pour qu'un
passage ne mesure plus que deux symboles et remplace des semaines de mesures.
"""

from __future__ import annotations

import pytest

from app.services import training_service

pytestmark = pytest.mark.asyncio


async def test_training_is_deferred_while_a_backtest_runs(monkeypatch):
    """Entraînement et backtest puisent au MÊME quota : jamais en même temps.

    Mesuré le 01/08/2026 en les lançant ensemble : 741 échecs de chargement en quinze minutes,
    18 symboles écartés en trois. Chacun isolément fonctionne ; ensemble, ils s'affament.
    """
    from app.backtest import playbook_backtest as pbt

    monkeypatch.setattr(pbt, "run_state", lambda: {"running": True, "phase": "passe portée"})
    out = await training_service.run_training(None)

    assert out.get("deferred") is True
    assert out.get("trained") is False
    assert "quota" in out["note"]


async def test_a_degraded_run_never_overwrites_a_better_one(monkeypatch):
    """Un passage qui ne mesure que 2 symboles ne remplace pas un passage qui en mesurait 20.

    Sans cette garde, une seule mauvaise nuit efface des semaines d'apprentissage — et rien ne le
    signale, puisque l'instantané dégradé a exactement la même forme qu'un bon.
    """
    from app.backtest import playbook_backtest as pbt

    monkeypatch.setattr(pbt, "run_state", lambda: {"running": False})
    # Passage précédent, riche.
    training_service._STATE = {"symbols_trained": 20, "date": "2026-07-29",   # noqa: SLF001
                               "overall": {"win_rate": 27.0, "expectancy_r": -0.09}}

    async def _un_symbole(symbol, asset_class=""):  # noqa: ANN001
        return {"symbol": symbol, "trades": []}

    monkeypatch.setattr(training_service, "train_symbol", _un_symbole)
    monkeypatch.setattr(training_service, "training_universe",
                        lambda _s: [{"symbol": "EUR/USD", "asset_class": "forex"}])

    ecrits: list = []

    class _Store:
        class records:
            @staticmethod
            def get(*a, **k):
                return None

            @staticmethod
            def put(*a, **k):
                ecrits.append(a)

    out = await training_service.run_training(_Store(), write_expertise=False)

    assert ecrits == [], "un passage dégradé ne doit RIEN persister"
    assert "NON publié" in out.get("note", "")
    assert training_service._STATE["symbols_trained"] == 20, (   # noqa: SLF001
        "l'apprentissage précédent doit rester en place"
    )
    training_service._STATE = None                               # noqa: SLF001


async def test_a_complete_run_is_published_normally(monkeypatch):
    """La garde ne doit pas empêcher un bon passage de remplacer un bon passage."""
    from app.backtest import playbook_backtest as pbt

    monkeypatch.setattr(pbt, "run_state", lambda: {"running": False})
    training_service._STATE = {"symbols_trained": 2}             # noqa: SLF001

    async def _un_symbole(symbol, asset_class=""):  # noqa: ANN001
        return {"symbol": symbol, "trades": []}

    monkeypatch.setattr(training_service, "train_symbol", _un_symbole)
    monkeypatch.setattr(training_service, "training_universe",
                        lambda _s: [{"symbol": s, "asset_class": "forex"}
                                    for s in ("EUR/USD", "GBP/USD", "USD/CHF", "AUD/USD")])

    ecrits: list = []

    class _Store:
        class records:
            @staticmethod
            def get(*a, **k):
                return None

            @staticmethod
            def put(*a, **k):
                ecrits.append(a)

    out = await training_service.run_training(_Store(), write_expertise=False)

    assert out["symbols_trained"] == 4
    assert ecrits, "un passage complet doit bien être persisté"
    training_service._STATE = None                               # noqa: SLF001
