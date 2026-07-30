"""ANALYSE QUOTIDIENNE DES MARCHÉS — l'avis du modèle, en dehors de la stratégie du desk.

Deux routes seulement : lire l'analyse du jour, ou en forcer une nouvelle. Le contenu et la
justification de cette séparation vivent dans `services.market_opinion_service`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.core.deps import current_user, store_dep
from app.models.entities import User
from app.repositories.store import AppStore
from app.services import market_opinion_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/analysis", tags=["analysis"])


@router.get("/daily")
async def daily_opinion(
    _user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """L'analyse du jour : l'avis du modèle sur chaque paire forex et sur l'or.

    Produite par les agents (technique, volume, sentiment, figures, macro) arbitrés par le Master,
    **sans la stratégie du desk** — l'agent playbook n'est ni exécuté ni compté, et son droit de
    veto ne s'applique pas. C'est un SECOND REGARD : la stratégie garde seule la décision de trader.

    `stale` indique que l'analyse servie ne porte pas la date du jour (le passage quotidien n'a pas
    encore eu lieu, ou a échoué) — mieux vaut une analyse datée d'hier annoncée comme telle qu'une
    page vide ou, pire, un avis d'hier présenté comme celui d'aujourd'hui.
    """
    payload = market_opinion_service.latest(store)
    if not payload:
        return {
            "available": False,
            "note": ("Aucune analyse produite pour l'instant. Elle est générée automatiquement "
                     "chaque jour, et rattrapée peu après le démarrage si celle du jour manque."),
            "universe": market_opinion_service.symbols(),
        }
    return {
        "available": True,
        "stale": not market_opinion_service.is_fresh(payload),
        **payload,
    }


@router.post("/daily/run")
async def run_daily_opinion(
    _user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Force une analyse immédiate (utile pour rafraîchir sans attendre le passage quotidien).

    Volontairement SYNCHRONE, contrairement au backtest : l'univers est court (une douzaine
    d'instruments) et l'utilisateur qui clique veut voir le résultat, pas suivre une barre de
    progression.
    """
    return await market_opinion_service.run_daily_opinion(store)
