"""ANALYSE QUOTIDIENNE DES MARCHÉS — l'avis du modèle, HORS stratégie du desk.

Ce que ces tests garantissent :

1. **L'analyse est vraiment hors stratégie** — l'agent playbook n'est ni exécuté ni compté, et son
   droit de veto ne s'applique pas. C'est la promesse centrale : un second regard qui subirait le
   veto de la stratégie ne ferait que répéter la stratégie.
2. **Aucun avis inventé** — un symbole sans données réelles est listé avec son motif, pas deviné.
3. **Le raisonnement est conservé** — agents, métriques et pesée du Master, sinon l'avis n'est pas
   vérifiable.
4. **Elle est prête chaque jour** — `is_fresh` déclenche le rattrapage au démarrage.
"""

from __future__ import annotations

import pytest

from app.services import market_opinion_service as mos

pytestmark = pytest.mark.asyncio


@pytest.fixture
def market(monkeypatch):
    """Marché déterministe et RÉEL (pas de repli synthétique) pour tous les symboles."""
    from app.data import markets
    from tests.test_playbook import _tf

    series = _tf("h4")

    async def _load(symbol, interval="1h", limit=200):  # noqa: ANN001
        return series

    async def _news(symbol, limit=20):  # noqa: ANN001
        return []

    async def _macro():
        return {}

    monkeypatch.setattr(markets, "load_candles", _load)
    monkeypatch.setattr(markets, "data_source", lambda symbol: "real")
    monkeypatch.setattr("app.data.news.fetch_news", _news)
    monkeypatch.setattr("app.data.macro.fetch_macro_data", _macro)
    return series


# ---------------------------------------------------------------------------------------
# 1. Hors stratégie : c'est LA promesse de cette analyse
# ---------------------------------------------------------------------------------------
async def test_the_playbook_agent_is_absent_from_the_opinion(market):
    """L'agent playbook ne doit apparaître NI dans le vote, NI dans les métriques rendues."""
    from app.core.config import get_settings

    s = get_settings()
    s.playbook_enabled = True      # la stratégie est active pour le reste du produit…
    try:
        row = await mos.analyse_symbol("EUR/USD")
    finally:
        s.playbook_enabled = False

    assert not row.get("error"), row
    names = {a["name"] for a in row["agents"]}
    assert "playbook" not in names, "…mais elle doit rester HORS de cette analyse"
    assert "playbook" not in row["metrics"]
    assert row["master"]["weights_used"] is not None


async def test_the_playbook_veto_does_not_apply(market, monkeypatch):
    """Même un playbook qui REFUSE tout ne doit pas forcer l'avis à « neutre ».

    Sans cette garantie, l'analyse afficherait « aucune opinion » exactement les jours où la
    stratégie refuse d'entrer — c'est-à-dire presque tous, ce qui la rendrait inutile.
    """
    from app.agents.base import AgentOutput
    from app.core.config import get_settings

    async def refusing_playbook(setup):  # noqa: ANN001
        return AgentOutput(
            name="playbook", score=0.0, confidence=1.0, rationale="refus",
            details={"veto": True, "reasons": ["test"], "direction": "NO_TRADE"},
        )

    monkeypatch.setattr("app.agents.playbook.run", refusing_playbook)
    s = get_settings()
    s.playbook_enabled = True
    s.playbook_veto = True
    try:
        row = await mos.analyse_symbol("EUR/USD")
    finally:
        s.playbook_enabled = False

    assert not row.get("error"), row
    assert row["master"].get("playbook_veto") in (None, ""), "aucun veto ne doit être appliqué ici"
    assert "playbook" not in {a["name"] for a in row["agents"]}


# ---------------------------------------------------------------------------------------
# 2. Jamais d'avis inventé
# ---------------------------------------------------------------------------------------
async def test_no_opinion_on_synthetic_data(monkeypatch):
    """Données non réelles -> le symbole est listé AVEC son motif, jamais avec un avis fabriqué."""
    from app.data import markets
    from tests.test_playbook import _tf

    async def _load(symbol, interval="1h", limit=200):  # noqa: ANN001
        return _tf("h4")

    monkeypatch.setattr(markets, "load_candles", _load)
    monkeypatch.setattr(markets, "data_source", lambda symbol: "synthetic")

    row = await mos.analyse_symbol("EUR/USD")
    assert "non réelles" in row["error"]
    assert "direction" not in row


async def test_no_opinion_on_thin_history(monkeypatch):
    from app.data import markets
    from app.domain.indicators import Candle

    async def _load(symbol, interval="1h", limit=200):  # noqa: ANN001
        return [Candle(1.0, 1.0, 1.0, 1.0, 10.0)] * 5

    monkeypatch.setattr(markets, "load_candles", _load)
    monkeypatch.setattr(markets, "data_source", lambda symbol: "real")

    row = await mos.analyse_symbol("EUR/USD")
    assert "historique insuffisant" in row["error"]


# ---------------------------------------------------------------------------------------
# 3. Le raisonnement est conservé — un avis sans son détail n'est pas vérifiable
# ---------------------------------------------------------------------------------------
async def test_the_opinion_carries_everything_that_produced_it(market):
    row = await mos.analyse_symbol("XAU/USD")
    assert not row.get("error"), row
    assert row["direction"] in ("BUY", "SELL", "HOLD")
    assert row["stance"] in ("haussier", "baissier", "neutre")
    assert 0 <= row["confidence"] <= 100
    assert row["conviction"]
    assert row["headline"].startswith("XAU/USD")
    assert row["rationale"]
    assert len(row["agents"]) >= 3, "le détail agent par agent doit être présent"
    for agent in row["agents"]:
        assert {"name", "score", "confidence", "rationale"} <= set(agent)
    assert row["master"]["score"] is not None
    assert row["metrics"], "les indicateurs mesurés doivent être conservés"
    assert row["levels"]["source"] == "atr", "hors stratégie, les niveaux sont indicatifs (ATR)"


# ---------------------------------------------------------------------------------------
# 4. Passage complet + fraîcheur
# ---------------------------------------------------------------------------------------
async def test_full_run_summarises_and_persists(market):
    from app.repositories.store import get_store

    store = get_store()
    payload = await mos.run_daily_opinion(store, universe=["EUR/USD", "XAU/USD", "GBP/USD"])

    assert payload["summary"]["analysed"] == 3
    assert payload["summary"]["bullish"] + payload["summary"]["bearish"] + \
        payload["summary"]["neutral"] == 3
    assert payload["summary"]["strongest"]["symbol"] in ("EUR/USD", "XAU/USD", "GBP/USD")
    assert "SANS la stratégie du desk" in payload["method"]
    # Persistée sous la date du jour ET sous `latest`.
    assert mos.latest(store)["date"] == payload["date"]
    assert mos.is_fresh(payload) is True


async def test_a_failing_symbol_does_not_break_the_run(market, monkeypatch):
    """Un symbole KO est compté dans `failed` et n'empêche pas les autres d'être analysés."""
    from app.repositories.store import get_store

    original = mos.analyse_symbol

    async def flaky(symbol, **kw):  # noqa: ANN001
        if symbol == "GBP/USD":
            return {"symbol": symbol, "error": "connecteur indisponible"}
        return await original(symbol, **kw)

    monkeypatch.setattr(mos, "analyse_symbol", flaky)
    payload = await mos.run_daily_opinion(get_store(), universe=["EUR/USD", "GBP/USD"])
    assert payload["summary"]["analysed"] == 1
    assert payload["summary"]["failed"] == 1
    assert "sans données exploitables" in payload["summary"]["note"]


def test_is_fresh_only_accepts_today():
    assert mos.is_fresh(None) is False
    assert mos.is_fresh({"date": "2020-01-01"}) is False


# ---------------------------------------------------------------------------------------
# 5. API
# ---------------------------------------------------------------------------------------
def test_api_daily_analysis_endpoints(market):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.post("/api/auth/register", json={"email": "opinion@test.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # Rien encore produit : la route le DIT au lieu de rendre une page vide.
    empty = client.get("/api/analysis/daily", headers=h).json()
    assert empty["available"] is False
    assert "universe" in empty and len(empty["universe"]) > 0

    ran = client.post("/api/analysis/daily/run", headers=h).json()
    assert ran["summary"]["analysed"] >= 1

    body = client.get("/api/analysis/daily", headers=h).json()
    assert body["available"] is True and body["stale"] is False
    assert body["opinions"] and body["summary"]
    assert "SANS la stratégie du desk" in body["method"]
