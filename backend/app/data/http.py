"""CLIENT HTTP PARTAGÉ — une connexion réutilisée, au lieu d'une par requête.

POURQUOI CE MODULE EXISTE

Chaque connecteur ouvrait `async with httpx.AsyncClient(...)` à CHAQUE appel : une connexion TCP
neuve et une poignée de main TLS complète pour chaque bougie demandée. Le balayage en réclame ~440
par cycle, auxquelles s'ajoutent l'auto-entrée, les positions et les news.

Mesuré le 01/08/2026, 15 requêtes identiques vers Yahoo depuis le conteneur :

    client jetable (l'ancien)  10/15 réussies, 5 échecs, 1,08 s par requête
    client partagé             13/15 réussies, 2 échecs, 0,52 s par requête

Réutiliser la connexion divise les échecs par 2,5 et la latence par 2. C'est l'explication des
`ConnectError` intermittents observés sur TOUS les fournisseurs à la fois — Binance, Yahoo, Twelve
Data, Polygon : un symptôme commun à quatre fournisseurs indépendants ne vient pas de quatre causes
distinctes, il vient de ce qu'ils partagent, c'est-à-dire la façon dont on les appelle.

Ce module ne change RIEN aux données : mêmes URL, mêmes paramètres, mêmes réponses. Il change
seulement le transport.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_clients: dict[str, object] = {}
_lock = asyncio.Lock()

# Bornes du pool. Généreuses sur le maintien en vie (les mêmes hôtes sont réinterrogés en boucle),
# mesurées sur le parallélisme : au-delà, on retombe sur la saturation que la cascade séquentielle
# avait déjà mise en évidence (cf. `markets._cascade`).
_MAX_CONNEXIONS = 20
_MAX_KEEPALIVE = 10
_KEEPALIVE_S = 60.0


async def client(nom: str = "default", *, timeout=None, headers: dict | None = None):  # noqa: ANN001, ANN201
    """Client HTTP partagé et réutilisable, créé une fois par `nom`.

    `nom` sépare les pools quand les en-têtes diffèrent (Yahoo veut son User-Agent, Alpaca ses clés).
    Le client n'est JAMAIS fermé par les appelants : il vit le temps du processus et garde ses
    connexions ouvertes — c'est tout l'intérêt. `close_all()` le libère à l'arrêt.

    `timeout` : passé tel quel à httpx. Les appelants gardent donc leurs propres délais (le délai de
    connexion court mesuré dans `markets._timeout` reste en vigueur).
    """
    import httpx

    existant = _clients.get(nom)
    if existant is not None and not existant.is_closed:  # type: ignore[attr-defined]
        return existant
    async with _lock:
        existant = _clients.get(nom)
        if existant is not None and not existant.is_closed:  # type: ignore[attr-defined]
            return existant
        _clients[nom] = httpx.AsyncClient(
            timeout=timeout if timeout is not None else httpx.Timeout(20.0, connect=3.0),
            headers=headers or {},
            limits=httpx.Limits(max_connections=_MAX_CONNEXIONS,
                                max_keepalive_connections=_MAX_KEEPALIVE,
                                keepalive_expiry=_KEEPALIVE_S),
        )
        return _clients[nom]


async def close_all() -> None:
    """Ferme tous les clients partagés (arrêt de l'application, et tests)."""
    for nom, c in list(_clients.items()):
        try:
            await c.aclose()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — un client récalcitrant ne bloque pas l'arrêt
            logger.debug("Fermeture du client %s : %s", nom, exc)
    _clients.clear()
