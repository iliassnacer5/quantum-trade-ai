"""INSTANTANÉ du « scanner complémentaire par marché » — le même remède que `live_snapshot`.

Le problème résolu (mesuré le 30/07/2026) : `signal_service.daily_picks` scanne 4 classes d'actifs
puis lance jusqu'à 32 **backtests complets**. Tant que c'est la REQUÊTE HTTP qui déclenche ce
calcul, la page attend — et elle attendait une fois par unité de temps, puisque chaque bouton
(15 min / 1 h / 4 h / 1 j / 1 semaine / 1 mois) a sa propre entrée de cache. Le premier clic sur
chacun coûtait donc plusieurs dizaines de secondes.

La solution est celle qui a déjà fait ses preuves pour les trades du jour (`live_snapshot`) :

- une **boucle de fond** (`scheduler.daily_picks_loop`) calcule les six unités de temps à l'avance ;
- la **route** ne fait plus que lire ce qui est déjà prêt ;
- chaque réponse porte son **âge** (`age_seconds`, `stale`) : une sélection calculée il y a vingt
  minutes est servie comme telle, jamais maquillée en donnée fraîche.

Persistance : les résultats vivent dans `store.records` sous la collection ``daily_picks``, avec la
même convention de clé qu'avant (le 1 h garde la clé « nue » pour rester compatible avec le digest).
Le cache mémoire évite de relire la base à chaque requête, la base permet de survivre à un
redémarrage sans repartir d'une page vide.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.core.config import get_settings

logger = logging.getLogger(__name__)

COLLECTION = "daily_picks"

# Instantané en mémoire, par unité de temps. Un verrou PAR unité de temps : deux unités différentes
# peuvent se calculer en parallèle, mais deux calculs de la MÊME unité ne se doublent jamais.
_cache: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}


def timeframes() -> list[str]:
    """Unités de temps précalculées (`daily_picks_timeframes`)."""
    raw = get_settings().daily_picks_timeframes or "1h"
    return [t.strip() for t in raw.split(",") if t.strip()]


def _key(timeframe: str) -> str:
    """Clé de persistance. Le 1 h garde la clé historique (compatibilité du digest quotidien)."""
    today = datetime.now(UTC).date().isoformat()
    return today if timeframe == "1h" else f"{today}:{timeframe}"


def _age_of(payload: dict) -> float:
    computed = payload.get("generated_at")
    if not computed:
        return float("inf")
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(computed)).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


def _decorate(payload: dict) -> dict:
    """Ajoute l'âge : la fraîcheur fait partie de la donnée, pas du décor."""
    s = get_settings()
    age = _age_of(payload)
    out = dict(payload)
    out["age_seconds"] = round(age, 1) if age != float("inf") else None
    # Périmé au-delà de deux cycles : un seul cycle manqué (réseau lent) n'est pas une anomalie.
    out["stale"] = age > s.daily_picks_precompute_interval * 2
    out["refresh_interval"] = s.daily_picks_precompute_interval
    return out


async def refresh(store, timeframe: str, *, skip_if_newer_than: float | None = None) -> dict:  # noqa: ANN001
    """Recalcule la sélection pour UNE unité de temps et publie le résultat.

    Sérialisé par unité de temps : la boucle de fond et un clic sur « Rafraîchir » ne doivent pas
    lancer deux fois les mêmes 32 backtests.

    `skip_if_newer_than` : une fois le verrou obtenu, si un AUTRE appel vient de publier une
    sélection plus récente que ce seuil, on la sert au lieu de refaire le même travail. Sans ce
    garde-fou, le verrou ne fait que METTRE EN FILE les calculs identiques au lieu de les éviter :
    cinq pages qui arrivent ensemble au démarrage déclenchaient cinq fois les 32 backtests, l'une
    après l'autre. Même mécanisme que `live_snapshot.refresh`.
    """
    from app.services import signal_service

    lock = _locks.setdefault(timeframe, asyncio.Lock())
    async with lock:
        existing = _cache.get(timeframe)
        if (skip_if_newer_than is not None and existing is not None
                and _age_of(existing) <= skip_if_newer_than):
            return _decorate(existing)
        picks = await signal_service.daily_picks(timeframe=timeframe)
        payload = {
            "date": datetime.now(UTC).date().isoformat(),
            "timeframe": timeframe,
            "picks": picks,
            "generated_at": datetime.now(UTC).isoformat(),
        }
        _cache[timeframe] = payload
        try:
            store.records.put(COLLECTION, _key(timeframe), payload)
        except Exception as exc:  # noqa: BLE001 — la persistance n'est qu'un confort
            logger.warning("Sélection du jour (%s) non persistée (%s)", timeframe, exc)
    return _decorate(payload)


async def get(store, timeframe: str = "1h", *, force: bool = False) -> dict:  # noqa: ANN001
    """Ce que sert la route : la sélection déjà prête, calculée seulement si elle n'existe pas.

    `force=True` (bouton « Rafraîchir ») relance le calcul complet pour cette unité de temps.
    """
    if force:
        return await refresh(store, timeframe)

    cached = _cache.get(timeframe)
    if cached is None:
        # Rien en mémoire : on tente la base avant de recalculer — un redémarrage ne doit pas
        # imposer de repayer les 32 backtests, la sélection du jour reste valable.
        try:
            stored = store.records.get(COLLECTION, _key(timeframe))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sélection du jour (%s) illisible (%s)", timeframe, exc)
            stored = None
        if stored and stored.get("picks") is not None:
            _cache[timeframe] = stored
            cached = stored

    if cached is None:
        # Premier passage (boucle de fond pas encore arrivée sur cette unité de temps) : on calcule
        # une fois, sinon la page resterait vide sans rien expliquer. Plusieurs pages peuvent
        # arriver ensemble : la PREMIÈRE calcule, les autres récupèrent son résultat.
        return await refresh(
            store, timeframe,
            skip_if_newer_than=get_settings().daily_picks_precompute_interval,
        )
    return _decorate(cached)


def reset() -> None:
    """Vide l'instantané en mémoire (tests)."""
    _cache.clear()
    _locks.clear()
