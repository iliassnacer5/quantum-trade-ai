"""INSTANTANÉ TEMPS RÉEL de la stratégie — ce qui rend les pages instantanées.

Le problème résolu : appliquer la stratégie à 40 symboles × 5 unités de temps prend plusieurs
dizaines de secondes. Tant que c'est la PAGE qui déclenche ce calcul, elle attend — d'où les 150 s
observés. La solution professionnelle est toujours la même : découpler.

- Une **boucle de fond** recalcule la sélection complète toutes les `playbook_snapshot_interval`
  secondes et publie le résultat ici.
- Les **endpoints** ne calculent plus rien : ils servent l'instantané déjà prêt, en quelques
  millisecondes. La page peut donc se rafraîchir toutes les 10 secondes sans jamais bloquer.
- Chaque réponse porte son **âge** (`age_seconds`, `stale`) : on ne fait jamais passer une donnée
  vieille de 5 minutes pour une donnée fraîche.

Au tout premier appel (boucle pas encore passée), on calcule une fois de façon synchrone pour ne
pas servir une page vide.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_snapshot: dict | None = None
_lock = asyncio.Lock()


def _age_of(payload: dict) -> float:
    computed = payload.get("computed_at")
    if not computed:
        return float("inf")
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(computed)).total_seconds()
    except ValueError:
        return float("inf")


def _decorate(payload: dict) -> dict:
    """Ajoute l'âge de l'instantané — la fraîcheur fait partie de la donnée, pas du décor."""
    s = get_settings()
    age = _age_of(payload)
    out = dict(payload)
    out["age_seconds"] = round(age, 1) if age != float("inf") else None
    out["stale"] = age > s.playbook_snapshot_max_age
    out["refresh_interval"] = s.playbook_snapshot_interval
    return out


def current() -> dict | None:
    """Instantané en mémoire, sans jamais déclencher de calcul (None s'il n'y en a pas encore)."""
    return _decorate(_snapshot) if _snapshot else None


async def refresh(store=None, *, count: int | None = None, skip_if_newer_than: float | None = None) -> dict:  # noqa: ANN001
    """Recalcule la sélection complète et publie le nouvel instantané.

    Sérialisé par un verrou : deux rafraîchissements simultanés (la boucle et un clic sur
    « recalculer ») ne doivent pas doubler la charge réseau.

    `skip_if_newer_than` : une fois le verrou obtenu, si un AUTRE appel vient de publier un
    instantané plus récent que ce seuil, on le sert au lieu de refaire le même travail. C'est le
    cas typique du démarrage, où plusieurs pages arrivent avant le premier calcul.
    """
    global _snapshot

    from app.services import signal_service

    s = get_settings()
    async with _lock:
        if (skip_if_newer_than is not None and _snapshot is not None
                and _age_of(_snapshot) <= skip_if_newer_than):
            return _decorate(_snapshot)
        payload = await signal_service.daily_top_trades(count or s.daily_top_trades_count)
        payload["computed_at"] = datetime.now(UTC).isoformat()
        payload["date"] = datetime.now(UTC).date().isoformat()
        _snapshot = payload
        if store is not None:
            try:
                store.records.put("top_trades", payload["date"], payload)
            except Exception as exc:  # noqa: BLE001 — la persistance n'est qu'un confort
                logger.warning("Instantané non persisté (%s)", exc)
    return _decorate(payload)


async def get(store=None, *, count: int | None = None, force: bool = False) -> dict:  # noqa: ANN001
    """Ce que servent les endpoints : l'instantané prêt, recalculé seulement s'il n'existe pas.

    `force=True` (bouton « recalculer maintenant ») relance le calcul complet.
    """
    s = get_settings()
    if force:
        return await refresh(store, count=count)
    if _snapshot is None:
        # Plusieurs pages peuvent arriver ensemble avant le premier calcul : la première déclenche,
        # les autres récupèrent son résultat au lieu de relancer le même balayage.
        return await refresh(store, count=count, skip_if_newer_than=s.playbook_snapshot_interval)
    # Filet de sécurité : si la boucle de fond est tombée, on recalcule plutôt que de servir
    # indéfiniment une donnée périmée.
    if _age_of(_snapshot) > max(s.playbook_snapshot_max_age, s.playbook_snapshot_interval * 4):
        logger.warning("Instantané périmé (boucle de fond arrêtée ?) — recalcul synchrone")
        return await refresh(store, count=count)
    return _decorate(_snapshot)


def verdict_for(symbol: str) -> dict | None:
    """Verdict de la STRATÉGIE pour ce symbole, tel que calculé par la boucle de fond.

    C'est ce qui garantit qu'une même paire ne peut pas être « BUY » dans le scanner et « pas de
    trade » dans les trades du jour : les deux lisent le même calcul.
    """
    if not _snapshot:
        return None
    return (_snapshot.get("verdicts") or {}).get(symbol.upper())


def verdicts() -> dict[str, dict]:
    """Tous les verdicts du dernier instantané (dictionnaire vide s'il n'y en a pas encore)."""
    return dict((_snapshot or {}).get("verdicts") or {})


def armed_and_ready() -> list[dict]:
    """Setups du dernier instantané susceptibles d'être ouverts automatiquement."""
    if not _snapshot:
        return []
    return [p for p in (_snapshot.get("picks") or []) if p.get("tier") in ("ready", "armed")]


def reset() -> None:
    """Vide l'instantané (tests)."""
    global _snapshot
    _snapshot = None
