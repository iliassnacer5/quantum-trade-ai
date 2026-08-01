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


def _decorate(payload: dict, *, count: int | None = None) -> dict:
    """Ajoute l'âge de l'instantané — la fraîcheur fait partie de la donnée, pas du décor.

    `count` applique le PLAFOND DEMANDÉ par l'appelant au classement déjà calculé. Sans lui, la
    route servait l'instantané tel quel : `?count=5` rendait en réalité tous les setups (ou le
    plafond qu'avait utilisé la boucle de fond), et la réponse pouvait même ne pas porter la clé
    `requested` quand l'instantané venait de la base. Le paramètre documenté était donc sans effet
    dès qu'un instantané existait — c'est-à-dire presque toujours.

    Tronquer ici est correct et sans recalcul : `picks` est déjà trié par fiabilité décroissante,
    prendre les N premiers est exactement ce que « les N meilleurs » veut dire. Les compteurs
    (`ready`, `armed`, `conform`, `scanned`) décrivent le BALAYAGE complet et ne sont pas retouchés
    — ce sont deux questions différentes, et les confondre masquerait ce que le plafond a écarté.
    """
    s = get_settings()
    age = _age_of(payload)
    out = dict(payload)
    # LA SESSION EST RECALCULÉE À CHAQUE LECTURE, jamais servie depuis l'instantané.
    #
    # « Quelles places sont ouvertes ? » est une question sur l'INSTANT PRÉSENT. Figée dans
    # l'instantané, la réponse pouvait avoir jusqu'à `playbook_snapshot_max_age` (15 min) de retard,
    # et surtout survivre à un redémarrage : après correction du bug de week-end, l'instantané
    # persisté par l'ancien code continuait d'afficher « 🔥 Chevauchement Londres / New York » un
    # samedi, alors que `/api/market/sessions` répondait correctement « hors sessions majeures ».
    #
    # Le calcul est purement horaire (aucun accès réseau ni base) : le refaire à chaque lecture ne
    # coûte rien. Les `picks`, eux, restent bien ceux de l'instantané — leur date de calcul est
    # portée par `computed_at` et `age_seconds`.
    try:
        from app.data import sessions as sessions_mod

        out["session"] = sessions_mod.session_context()
    except Exception as exc:  # noqa: BLE001 — un contexte manquant ne doit pas casser la page
        logger.warning("Contexte de session non recalculé (%s)", exc)
    if count:
        picks = out.get("picks") or []
        if len(picks) > count:
            out["picks"] = picks[:count]
        out["requested"] = count
    out["age_seconds"] = round(age, 1) if age != float("inf") else None
    out["stale"] = age > s.playbook_snapshot_max_age
    out["refresh_interval"] = s.playbook_snapshot_interval
    return out


# Clés STRUCTURELLES d'un instantané complet, telles que `playbook_service.top_trades` les produit.
# Servent à refuser un enregistrement partiel relu depuis la base (cf. `_is_complete`).
_REQUIRED_KEYS = ("picks", "session", "strategy", "verdicts", "scanned")


def _is_complete(payload: dict | None) -> bool:
    """L'enregistrement relu a-t-il la forme d'un instantané complet ?

    Le contenu de `top_trades` en base survit aux redémarrages — donc aussi aux DÉPLOIEMENTS. Un
    enregistrement écrit par une version antérieure (ou une écriture partielle) n'a pas forcément
    les clés que l'interface lit aujourd'hui : servi tel quel, il produit une page cassée ou, pire,
    un `verdicts` vide qui fait taire le scanner et les pages d'analyse sans le dire.

    On préfère donc REcalculer que servir une structure incomplète : c'est plus lent une fois, et
    correct. Le coût ne se paie qu'au premier appel suivant un déploiement.
    """
    if not payload or payload.get("picks") is None:
        return False
    return all(k in payload for k in _REQUIRED_KEYS)


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
            return _decorate(_snapshot, count=count)
        # `count=None` -> le réglage (0 = tous les setups conformes). On ne remplace pas un 0
        # explicite par le défaut : « aucun plafond » est une valeur, pas une absence de valeur.
        payload = await signal_service.daily_top_trades(
            s.daily_top_trades_count if count is None else count)
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
    global _snapshot

    s = get_settings()
    if force:
        return await refresh(store, count=count)
    if _snapshot is None and store is not None:
        # REDÉMARRAGE : la boucle de fond persiste son instantané à chaque passage. Le relire est
        # instantané, alors que le recalculer coûte ~80 s (84 symboles × 5 unités de temps) —
        # mesuré le 30/07/2026. Une page ouverte dans la minute qui suit un redémarrage payait ce
        # calcul complet, alors qu'un instantané du jour existait déjà en base.
        # Il est servi avec son ÂGE : une donnée de dix minutes est annoncée comme telle, jamais
        # maquillée en donnée fraîche (`_decorate`), et la boucle de fond la remplacera d'elle-même.
        try:
            stored = store.records.get("top_trades", datetime.now(UTC).date().isoformat())
        except Exception as exc:  # noqa: BLE001 — la relecture n'est qu'un raccourci
            logger.warning("Instantané persisté illisible (%s)", exc)
            stored = None
        if _is_complete(stored):
            _snapshot = stored
            return _decorate(stored, count=count)
        if stored:
            logger.warning(
                "Instantané persisté incomplet (clés manquantes : %s) — recalcul plutôt que "
                "de servir une structure partielle",
                ", ".join(k for k in _REQUIRED_KEYS if k not in stored) or "picks",
            )
    if _snapshot is None:
        # Plusieurs pages peuvent arriver ensemble avant le premier calcul : la première déclenche,
        # les autres récupèrent son résultat au lieu de relancer le même balayage.
        return await refresh(store, count=count, skip_if_newer_than=s.playbook_snapshot_interval)
    # Filet de sécurité : si la boucle de fond est tombée, on recalcule plutôt que de servir
    # indéfiniment une donnée périmée.
    if _age_of(_snapshot) > max(s.playbook_snapshot_max_age, s.playbook_snapshot_interval * 4):
        logger.warning("Instantané périmé (boucle de fond arrêtée ?) — recalcul synchrone")
        return await refresh(store, count=count)
    return _decorate(_snapshot, count=count)


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


def pool_is_usable() -> bool:
    """Le vivier de surveillance de l'auto-entrée est-il exploitable, ou périmé/absent ?

    `armed_and_ready()` lit l'instantané SANS aucun contrôle de fraîcheur. C'est acceptable pour
    afficher une page, ça ne l'est pas pour décider de trader : si la boucle de fond s'arrête (ou
    n'est jamais passée, ou vient d'être remise à zéro), le vivier reste vide ou figé et l'auto-
    entrée conclut « aucun setup armé » en boucle. Un cache devient alors, en pratique, l'autorité
    qui décide qu'il n'y a pas de trade — exactement ce que l'architecture interdit.

    Cette fonction dit seulement si le vivier mérite confiance ; c'est à l'appelant de le
    rafraîchir. Le seuil est celui du filet de sécurité déjà utilisé par `get()`.
    """
    if _snapshot is None:
        return False
    s = get_settings()
    return _age_of(_snapshot) <= max(s.playbook_snapshot_max_age, s.playbook_snapshot_interval * 4)


def reset() -> None:
    """Vide l'instantané (tests)."""
    global _snapshot
    _snapshot = None
