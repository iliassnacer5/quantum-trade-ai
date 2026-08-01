"""Verrous du PLAN PERFORMANCE (globale/07_PLAN_PERFORMANCE.md), réalisé le 30/07/2026.

Chaque test protège une optimisation contre une régression silencieuse. Le point commun : une
optimisation ne vaut rien si elle change le RÉSULTAT — ces tests vérifient donc d'abord que le
comportement est identique, et seulement ensuite qu'il est moins coûteux.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import get_settings
from app.repositories.memory import RecordRepository
from app.services import execution_service

# Pas de `pytestmark` global ici : ce fichier mêle tests synchrones et asynchrones, et marquer les
# premiers `asyncio` ne fait que produire des avertissements pytest sans rien apporter. Les tests
# asynchrones portent le marqueur individuellement.
_aio = pytest.mark.asyncio


# ---------------------------------------------------------------------------------------
# P1-4 : filtrer les positions OUVERTES au plus près de la base
# ---------------------------------------------------------------------------------------
def _seed(repo, n_closed: int = 50) -> None:
    """Un historique réaliste : beaucoup de trades clôturés, quelques positions ouvertes."""
    for i in range(n_closed):
        repo.put("order", f"closed-{i}", {"outcome": "won" if i % 2 else "lost",
                                          "symbol": "EUR/USD", "mode": "paper"}, tenant_id="t1")
    repo.put("order", "neutral-1", {"outcome": "reset", "symbol": "EUR/USD", "mode": "paper"},
             tenant_id="t1")
    repo.put("order", "neutral-2", {"outcome": "invalid", "symbol": "EUR/USD", "mode": "paper"},
             tenant_id="t1")
    repo.put("order", "open-1", {"symbol": "GBP/USD", "mode": "paper"}, tenant_id="t1")
    repo.put("order", "open-2", {"outcome": "open", "symbol": "NVDA", "mode": "paper"},
             tenant_id="t1")
    repo.put("order", "other-tenant", {"symbol": "AAPL", "mode": "paper"}, tenant_id="t2")


def test_list_where_field_not_in_returns_only_open_orders():
    """Le filtre doit rendre EXACTEMENT ce que le filtre Python d'origine rendait."""
    repo = RecordRepository()
    _seed(repo)

    filtered = repo.list_where_field_not_in(
        "order", "outcome", execution_service.FINAL_OUTCOMES, "t1")
    # L'oracle : l'ancienne façon de faire (tout charger, filtrer en Python).
    reference = [r for r in repo.list("order", "t1")
                 if r.get("outcome") not in execution_service.FINAL_OUTCOMES]

    assert {r["id"] for r in filtered} == {r["id"] for r in reference}
    assert {r["id"] for r in filtered} == {"open-1", "open-2"}


def test_list_where_field_not_in_respects_tenant_isolation():
    """Un filtre de performance ne doit jamais élargir la visibilité entre comptes."""
    repo = RecordRepository()
    _seed(repo)
    ids = {r["id"] for r in repo.list_where_field_not_in(
        "order", "outcome", execution_service.FINAL_OUTCOMES, "t1")}
    assert "other-tenant" not in ids
    # Sans tenant, on voit les deux comptes (c'est ce dont les boucles de fond ont besoin).
    all_open = repo.list_where_field_not_in("order", "outcome", execution_service.FINAL_OUTCOMES)
    assert "other-tenant" in {r["id"] for r in all_open}


def test_list_where_field_not_in_with_no_exclusions_is_a_plain_list():
    """Aucune valeur exclue = aucun filtre : le contrat doit rester prévisible."""
    repo = RecordRepository()
    _seed(repo, n_closed=3)
    assert len(repo.list_where_field_not_in("order", "outcome", set(), "t1")) == \
        len(repo.list("order", "t1"))


def test_open_orders_helper_matches_the_previous_inline_filter():
    """`_open_orders` est le point de vérité unique employé par les 4 passages de surveillance."""
    from app.repositories.store import get_store

    store = get_store()
    _seed(store.records)
    got = {r["id"] for r in execution_service._open_orders(store, "t1")}
    assert got == {"open-1", "open-2"}


# ---------------------------------------------------------------------------------------
# P1-2 / P1-3 : cache court des bougies + délai de connexion borné
# ---------------------------------------------------------------------------------------
@_aio
async def test_load_candles_serves_the_second_call_from_cache(monkeypatch):
    """Deux appels rapprochés pour le même (symbole, unité, limite) = UN SEUL appel réseau.

    C'est ce qui fait tomber `/api/execution/positions` : les quatre passages de `positions_loop`
    et le rafraîchissement de page toutes les 10 s redemandaient les mêmes bougies."""
    from app.data import markets
    from app.domain.indicators import Candle

    calls: list[str] = []

    async def _fake(symbol, interval, limit):  # noqa: ANN001
        calls.append(symbol)
        return [Candle(1.0, 1.01, 0.99, 1.0, 100.0) for _ in range(80)]

    monkeypatch.setattr(markets, "_yahoo_candles", _fake)
    monkeypatch.setattr(markets, "_alpaca_candles", _fake)
    markets.clear_cache()

    first = await markets.load_candles("NVDA", "1h", 60)
    second = await markets.load_candles("NVDA", "1h", 60)

    assert len(first) == len(second) == 80
    assert len(calls) == 1, "le second appel devait être servi par le cache"
    assert markets.data_source("NVDA") == "real", "la source doit survivre au cache"


@_aio
async def test_load_candles_cache_is_keyed_by_limit(monkeypatch):
    """Rendre 60 bougies à un appelant qui en demande 200 tronquerait son analyse en silence."""
    from app.data import markets
    from app.domain.indicators import Candle

    async def _fake(symbol, interval, limit):  # noqa: ANN001
        return [Candle(1.0, 1.01, 0.99, 1.0, 100.0) for _ in range(limit)]

    monkeypatch.setattr(markets, "_yahoo_candles", _fake)
    monkeypatch.setattr(markets, "_alpaca_candles", _fake)
    markets.clear_cache()

    assert len(await markets.load_candles("NVDA", "1h", 60)) == 60
    assert len(await markets.load_candles("NVDA", "1h", 200)) == 200


@_aio
async def test_load_candles_remembers_failures_to_avoid_retry_storms(monkeypatch):
    """Un symbole que PERSONNE ne sert ne doit pas être réinterrogé à chaque rafraîchissement :
    sans mémorisation de l'échec, on repaie le délai de connexion en boucle."""
    from app.data import markets

    calls: list[str] = []

    async def _down(symbol, interval, limit, **kw):  # noqa: ANN001
        calls.append(symbol)
        raise RuntimeError("fournisseur injoignable")

    monkeypatch.setattr(markets, "_yahoo_candles", _down)
    monkeypatch.setattr(markets, "_alpaca_candles", _down)
    markets.clear_cache()
    s = get_settings()
    previous = s.data_allow_synthetic
    s.data_allow_synthetic = False       # comportement de PRODUCTION
    try:
        assert await markets.load_candles("NVDA", "1h", 60) == []
        n_after_first = len(calls)
        assert await markets.load_candles("NVDA", "1h", 60) == []
        assert len(calls) == n_after_first, "l'échec devait être mémorisé, pas retenté aussitôt"
    finally:
        s.data_allow_synthetic = previous


def test_cache_duration_never_hides_a_new_candle():
    """Le cache est proportionnel à la bougie — mais toujours BIEN plus court qu'elle.

    C'est la propriété qui rend l'allongement sûr pour la stratégie : quelle que soit l'unité de
    temps, le cache expire avant la fermeture de la bougie suivante, donc aucune nouvelle bougie ne
    peut être masquée. Et aucune unité n'est cachée MOINS longtemps qu'avant (`market_cache_ttl`
    reste le plancher).
    """
    from app.data import markets

    base = get_settings().market_cache_ttl
    for interval, bar in markets._BAR_SECONDS.items():   # noqa: SLF001
        ttl = markets._ttl_for(interval)                 # noqa: SLF001
        assert ttl >= base, f"{interval} : le cache ne doit jamais raccourcir"
        assert ttl < bar, f"{interval} : un cache de {ttl}s masquerait une bougie de {bar}s"

    # L'intention chiffrée : le journalier n'est plus rechargé aussi souvent que le 15 min.
    assert markets._ttl_for("1d") > markets._ttl_for("15m")     # noqa: SLF001

    # L'INVARIANT DE SÛRETÉ, celui qui compte vraiment : chaque cache reste une petite FRACTION de
    # sa bougie. C'est ce qui garantit qu'aucune nouvelle bougie ne peut être masquée, quel que
    # soit le réglage de `market_cache_ttl_ratio`. (Une version antérieure figeait ici la valeur
    # exacte du 15 min — cela documentait un ratio, pas une garantie, et rendait le réglage
    # intouchable sans casser un test.)
    for interval, bar in markets._BAR_SECONDS.items():          # noqa: SLF001
        assert markets._ttl_for(interval) <= bar * 0.1, (       # noqa: SLF001
            f"{interval} : un cache de plus de 10 % de la bougie devient risqué"
        )
    # Et aucune DÉCISION ne lit ce cache : les chemins qui engagent de l'argent passent par
    # `fresh=True` (cf. `test_a_fresh_read_is_never_throttled` et `_reference_price`).


@_aio
async def test_a_failure_is_never_cached_for_long(monkeypatch):
    """Un ÉCHEC garde le TTL court, même sur une unité de temps longue.

    Mémoriser 24 minutes l'indisponibilité d'une bougie journalière rendrait le symbole aveugle
    tout ce temps après un simple hoquet réseau — l'auto-entrée cesserait de le surveiller, et l'on
    retomberait dans « les agents ouvrent beaucoup moins de positions ».
    """
    from app.data import markets

    async def _down(symbol, interval, limit, **kw):  # noqa: ANN001
        raise RuntimeError("hoquet réseau")

    monkeypatch.setattr(markets, "_yahoo_candles", _down)
    monkeypatch.setattr(markets, "_alpaca_candles", _down)
    markets.clear_cache()
    s = get_settings()
    previous = s.data_allow_synthetic
    s.data_allow_synthetic = False
    try:
        assert await markets.load_candles("NVDA", "1d", 200) == []
        expiry, series, source = markets._CACHE[("NVDA", "1d", 200)]  # noqa: SLF001
        assert series == [] and source == "unavailable"
        import time as _t
        reste = expiry - _t.monotonic()
        assert reste <= s.market_cache_ttl + 1, (
            f"un échec ne doit pas être mémorisé {reste:.0f}s — le symbole resterait aveugle"
        )
    finally:
        s.data_allow_synthetic = previous


@_aio
async def test_a_fresh_read_is_never_throttled(monkeypatch):
    """Une lecture `fresh=True` ne fait JAMAIS la queue derrière le balayage de fond.

    Les lectures fraîches sont celles qui engagent de l'argent (remplissage, déplacement de stop,
    clôture) ou décident d'une entrée. Les réguler reviendrait à rater un déclencheur 15 min pour
    économiser du quota — le compromis exactement inverse de celui qu'on veut.
    """
    import time as _t

    from app.data import markets

    s = get_settings()
    previous = s.yahoo_max_rps
    s.yahoo_max_rps = 2.0                     # 500 ms entre deux requêtes régulées
    markets._yahoo_next_at = 0.0              # noqa: SLF001
    try:
        # Trois créneaux régulés d'affilée : le régulateur doit imposer un espacement.
        t0 = _t.monotonic()
        for _ in range(3):
            await markets._yahoo_slot()       # noqa: SLF001
        regule = _t.monotonic() - t0
        assert regule >= 0.5, "le balayage de fond doit bien être espacé"

        # Une lecture FRAÎCHE ne passe pas par le créneau : elle ne doit rien attendre.
        calls = []

        async def _instant(symbol, interval="1h", limit=200):  # noqa: ANN001
            calls.append(symbol)
            return [{"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}]

        from app.data import yahoo
        monkeypatch.setattr(yahoo, "fetch_ohlcv", _instant)
        t1 = _t.monotonic()
        await markets._yahoo_candles("EUR/USD", "1h", 60, fresh=True)  # noqa: SLF001
        assert _t.monotonic() - t1 < 0.2, "une lecture fraîche ne doit jamais être mise en attente"
        assert calls == ["EUR/USD"]
    finally:
        s.yahoo_max_rps = previous
        markets._yahoo_next_at = 0.0          # noqa: SLF001


@_aio
async def test_cascade_falls_through_to_the_second_provider(monkeypatch):
    """Les deux fournisseurs sont MESURÉS complémentaires : si le premier ne sert pas le symbole,
    le second doit être essayé (c'est ce qui garantit la couverture)."""
    from app.data import markets
    from app.domain.indicators import Candle

    async def _down(symbol, interval, limit, **kw):  # noqa: ANN001
        raise RuntimeError("injoignable")

    async def _ok(symbol, interval, limit, **kw):  # noqa: ANN001
        return [Candle(1.0, 1.01, 0.99, 1.0, 100.0) for _ in range(80)]

    monkeypatch.setattr(markets, "_alpaca_candles", _down)
    monkeypatch.setattr(markets, "_yahoo_candles", _ok)
    markets.clear_cache()
    assert len(await markets.load_candles("NVDA", "1h", 60)) == 80


def test_connect_timeout_is_short_and_read_timeout_is_not():
    """Le temps mort mesuré était dans la CONNEXION (10-12 s), pas dans la lecture — une lecture
    lente mais réussie (Yahoo/PEP en 8,09 s) doit continuer d'aboutir."""
    from app.data.markets import _timeout

    s = get_settings()
    assert s.market_connect_timeout <= 5, "une connexion qui traîne ne s'établira pas"
    assert s.market_read_timeout >= 10, "raccourcir la lecture transformerait des succès en échecs"
    normal, deep = _timeout(60), _timeout(5000)
    assert normal.connect == deep.connect == s.market_connect_timeout
    assert deep.read > normal.read, "un backtest profond a droit à plus de temps de lecture"


# ---------------------------------------------------------------------------------------
# P1-1 : le scanner complémentaire est SERVI, plus calculé dans la requête
# ---------------------------------------------------------------------------------------
@_aio
async def test_daily_picks_route_serves_a_precomputed_snapshot(monkeypatch):
    """La route ne doit relancer les 32 backtests que si on le demande explicitement."""
    from app.repositories.store import get_store
    from app.services import daily_picks_cache, signal_service

    computes: list[str] = []

    async def _fake_picks(**kwargs):
        computes.append(kwargs.get("timeframe", "?"))
        return [{"symbol": "BTC/USDT", "asset_class": "crypto", "tier": "watch"}]

    monkeypatch.setattr(signal_service, "daily_picks", _fake_picks)
    daily_picks_cache.reset()
    store = get_store()

    first = await daily_picks_cache.get(store, "1h")
    second = await daily_picks_cache.get(store, "1h")
    assert len(computes) == 1, "le 2e appel devait lire l'instantané, pas recalculer"
    assert first["picks"] == second["picks"]
    # L'âge fait partie de la donnée : on ne fait pas passer une sélection ancienne pour fraîche.
    assert "age_seconds" in second and "stale" in second

    await daily_picks_cache.get(store, "1h", force=True)
    assert len(computes) == 2, "`refresh=true` doit bien forcer le recalcul"


@_aio
async def test_daily_picks_snapshots_are_isolated_per_timeframe(monkeypatch):
    """Chaque unité de temps a son propre instantané : servir le 1 h à la place du 4 h serait faux."""
    from app.repositories.store import get_store
    from app.services import daily_picks_cache, signal_service

    async def _fake_picks(**kwargs):
        return [{"symbol": f"SYM-{kwargs.get('timeframe')}", "tier": "watch"}]

    monkeypatch.setattr(signal_service, "daily_picks", _fake_picks)
    daily_picks_cache.reset()
    store = get_store()

    h1 = await daily_picks_cache.get(store, "1h")
    h4 = await daily_picks_cache.get(store, "4h")
    assert h1["picks"][0]["symbol"] == "SYM-1h"
    assert h4["picks"][0]["symbol"] == "SYM-4h"
    assert h1["timeframe"] == "1h" and h4["timeframe"] == "4h"


@_aio
async def test_daily_picks_concurrent_calls_compute_only_once(monkeypatch):
    """Le verrou par unité de temps : la boucle de fond et un clic simultané ne doivent pas
    lancer deux fois les mêmes 32 backtests."""
    from app.repositories.store import get_store
    from app.services import daily_picks_cache, signal_service

    computes = []

    async def _slow_picks(**kwargs):
        computes.append(1)
        await asyncio.sleep(0.05)
        return [{"symbol": "BTC/USDT", "tier": "watch"}]

    monkeypatch.setattr(signal_service, "daily_picks", _slow_picks)
    daily_picks_cache.reset()
    store = get_store()

    await asyncio.gather(*(daily_picks_cache.get(store, "1h") for _ in range(5)))
    assert len(computes) == 1, f"un seul calcul attendu, {len(computes)} lancés"


# ---------------------------------------------------------------------------------------
# P0-2 : la sélection du jour évalue les marchés EN PARALLÈLE
# ---------------------------------------------------------------------------------------
@_aio
async def test_daily_picks_evaluates_markets_in_parallel(monkeypatch):
    """4 classes × backtests séquentiels était le cœur de la lenteur. En parallèle, la durée
    totale doit rester proche de celle d'UN marché, pas de leur somme."""
    from app.services import signal_service

    async def _slow_scan(asset_class=None, timeframe="1h", limit=20, **kw):  # noqa: ANN001
        await asyncio.sleep(0.10)
        return [{"symbol": f"{asset_class}-1", "direction": "BUY", "price": 1.0, "rsi": 50,
                 "trend": "haussière", "conviction": 1.0, "high_conviction": False}]

    async def _slow_bt(symbol, interval, limit=500):  # noqa: ANN001
        await asyncio.sleep(0.10)
        return None

    monkeypatch.setattr(signal_service, "scan_market", _slow_scan)
    monkeypatch.setattr(signal_service, "backtest_metrics", _slow_bt)

    loop = asyncio.get_running_loop()
    started = loop.time()
    picks = await signal_service.daily_picks(per_market=1)
    elapsed = loop.time() - started

    assert len(picks) == 4, "les 4 marchés doivent être représentés"
    # Séquentiel : 4 × (0,10 scan + 0,10 backtest) = 0,80 s. En parallèle : ~0,20 s.
    assert elapsed < 0.5, f"les marchés semblent encore séquentiels ({elapsed:.2f}s)"


@_aio
async def test_top_trades_reuses_the_persisted_snapshot_after_a_restart(monkeypatch):
    """Après un redémarrage, la première page ne doit PAS repayer les ~80 s de recalcul complet
    (84 symboles × 5 unités de temps) alors qu'un instantané du jour existe en base."""
    from datetime import UTC, datetime

    from app.repositories.store import get_store
    from app.services import live_snapshot, signal_service

    recomputes = []

    async def _never(*a, **k):
        recomputes.append(1)
        return {"picks": [], "ready": 0}

    monkeypatch.setattr(signal_service, "daily_top_trades", _never)

    store = get_store()
    today = datetime.now(UTC).date().isoformat()
    # Instantané COMPLET, tel que `playbook_service.top_trades` le produit : un enregistrement
    # partiel est désormais refusé et recalculé (cf. `live_snapshot._is_complete`), précisément
    # pour ne pas servir après un déploiement une structure que l'interface ne sait plus lire.
    store.records.put("top_trades", today, {
        "picks": [{"symbol": "EUR/USD", "tier": "ready"}],
        "ready": 1,
        "armed": 0,
        "scanned": 1,
        "conform": 1,
        "session": {"name": "Londres", "utc_time": "09:00"},
        "strategy": "Playbook — test",
        "verdicts": {"EUR/USD": {"tier": "ready", "direction": "BUY", "reason": "test"}},
        "requested": "tous",
        "computed_at": datetime.now(UTC).isoformat(),
        "date": today,
    })
    live_snapshot.reset()   # simule un redémarrage : mémoire vide, base intacte

    out = await live_snapshot.get(store)
    assert not recomputes, "l'instantané persisté devait être relu, pas recalculé"
    assert out["picks"][0]["symbol"] == "EUR/USD"
    # L'âge doit accompagner la donnée : on ne fait pas passer un instantané relu pour du frais.
    assert "age_seconds" in out and "stale" in out


@_aio
async def test_the_session_banner_is_never_served_from_the_snapshot(monkeypatch):
    """« Quelles places sont ouvertes » est une question sur MAINTENANT, pas sur l'instantané.

    Figée dans l'instantané, la réponse pouvait avoir 15 min de retard — et surtout survivre à un
    redémarrage : après correction du bug de week-end, l'instantané persisté par l'ancien code
    continuait d'afficher « 🔥 Chevauchement Londres / New York » un samedi.
    """
    from datetime import UTC, datetime

    from app.services import live_snapshot

    live_snapshot.reset()
    live_snapshot._snapshot = {                      # noqa: SLF001 — instantané au contenu périmé
        "picks": [], "ready": 0, "armed": 0, "scanned": 0, "conform": 0,
        "strategy": "s", "verdicts": {}, "requested": "tous",
        "session": {"label": "🔥 Chevauchement Londres / New York", "overlap": True,
                    "prime": True, "kill_zones": ["overlap"], "active": ["london", "newyork"]},
        "computed_at": datetime.now(UTC).isoformat(),
    }
    try:
        out = await live_snapshot.get(None)
        from app.data import sessions as sessions_mod
        attendu = sessions_mod.session_context()
        assert out["session"]["label"] == attendu["label"], (
            "la session servie doit être celle de MAINTENANT, pas celle figée dans l'instantané"
        )
        assert out["session"]["overlap"] == attendu["overlap"]
        # Les picks, eux, restent bien ceux de l'instantané.
        assert out["picks"] == []
    finally:
        live_snapshot.reset()


@_aio
async def test_partial_persisted_snapshot_is_refused_not_served(monkeypatch):
    """Un instantané persisté INCOMPLET (autre version, écriture partielle) doit être recalculé.

    Le servir tel quel produisait une page amputée — et surtout un `verdicts` absent, qui fait taire
    le scanner et les pages d'analyse sans que rien ne le signale.
    """
    from datetime import UTC, datetime

    from app.repositories.store import get_store
    from app.services import live_snapshot, signal_service

    recomputes = []

    async def _recompute(*a, **k):
        recomputes.append(1)
        return {"picks": [], "ready": 0, "armed": 0, "scanned": 0, "conform": 0,
                "session": {}, "strategy": "s", "verdicts": {}, "requested": "tous"}

    monkeypatch.setattr(signal_service, "daily_top_trades", _recompute)

    store = get_store()
    today = datetime.now(UTC).date().isoformat()
    store.records.put("top_trades", today, {          # forme d'une version antérieure
        "picks": [{"symbol": "EUR/USD", "tier": "ready"}],
        "computed_at": datetime.now(UTC).isoformat(),
        "date": today,
    })
    live_snapshot.reset()

    await live_snapshot.get(store)
    assert recomputes, "un instantané incomplet doit être recalculé, jamais servi tel quel"


@_aio
async def test_top_trades_count_is_applied_to_the_served_snapshot(monkeypatch):
    """`?count=N` doit VRAIMENT plafonner le classement servi depuis l'instantané.

    La route documentait « count>0 n'extrait que les N premiers » mais `_decorate` rendait
    l'instantané tel quel : le paramètre n'avait aucun effet dès qu'un instantané existait, donc
    presque toujours.
    """
    from datetime import UTC, datetime

    from app.services import live_snapshot

    picks = [{"symbol": f"P{i}", "tier": "ready"} for i in range(9)]
    live_snapshot.reset()
    live_snapshot._snapshot = {                       # noqa: SLF001 — instantané déjà publié
        "picks": picks, "ready": 9, "armed": 0, "scanned": 9, "conform": 9,
        "session": {}, "strategy": "s", "verdicts": {}, "requested": "tous",
        "computed_at": datetime.now(UTC).isoformat(),
    }

    out = await live_snapshot.get(None, count=3)
    assert len(out["picks"]) == 3, "le plafond demandé doit être appliqué"
    assert out["requested"] == 3
    assert [p["symbol"] for p in out["picks"]] == ["P0", "P1", "P2"], "les MEILLEURS d'abord"
    # Les compteurs décrivent le balayage complet, pas la tranche servie.
    assert out["scanned"] == 9 and out["conform"] == 9

    full = await live_snapshot.get(None, count=0)
    assert len(full["picks"]) == 9, "count=0 = aucun plafond"
    live_snapshot.reset()


@_aio
async def test_daily_picks_survives_one_broken_market(monkeypatch):
    """Un marché en panne ne doit pas vider la page des trois autres."""
    from app.services import signal_service

    async def _scan(asset_class=None, timeframe="1h", limit=20, **kw):  # noqa: ANN001
        if asset_class == "forex":
            raise RuntimeError("fournisseur forex indisponible")
        return [{"symbol": f"{asset_class}-1", "direction": "BUY", "price": 1.0, "rsi": 50,
                 "trend": "haussière", "conviction": 1.0, "high_conviction": False}]

    async def _bt(symbol, interval, limit=500):  # noqa: ANN001
        return None

    monkeypatch.setattr(signal_service, "scan_market", _scan)
    monkeypatch.setattr(signal_service, "backtest_metrics", _bt)

    picks = await signal_service.daily_picks(per_market=1)
    classes = {p["asset_class"] for p in picks}
    assert "forex" not in classes
    assert classes == {"crypto", "stock", "commodity"}
