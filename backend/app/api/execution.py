"""Exécution broker (M8, Phase 4) — réservé Elite (auto_execution). Mode papier par défaut.

Garde-fous : clés chiffrées (jamais renvoyées), exécution réelle conditionnée au KYC vérifié.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.deps import current_user, store_dep
from app.core.plans import plan_allows, plan_of
from app.models.entities import User
from app.repositories.store import AppStore
from app.services import audit, execution_service

router = APIRouter(prefix="/api/execution", tags=["execution"])


def _require_live_allowed(user: User, store: AppStore) -> None:
    """Le trading RÉEL exige le plan Elite (auto_execution). Le KYC est vérifié par connect_broker.
    Le mode papier est libre (apprentissage sans risque)."""
    if not plan_allows(plan_of(user, store), "auto_execution"):
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "Le trading réel est réservé au plan Elite. Le mode papier reste gratuit.",
        )


class ConnectRequest(BaseModel):
    broker: str = "paper"  # paper | alpaca
    api_key: str = ""
    api_secret: str = ""
    mode: str = "paper"  # paper | live


class OrderRequest(BaseModel):
    conn_id: str
    symbol: str
    side: str  # buy | sell
    qty: float
    stop_loss: float | None = None
    take_profit: float | None = None


class LevelsRequest(BaseModel):
    stop_loss: float | None = None
    take_profit: float | None = None


@router.post("/brokers", status_code=status.HTTP_201_CREATED)
async def connect(
    body: ConnectRequest,
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    if body.mode == "live":
        _require_live_allowed(user, store)
    try:
        conn = execution_service.connect_broker(
            store, user.tenant_id, broker=body.broker, api_key=body.api_key,
            api_secret=body.api_secret, mode=body.mode,
        )
    except execution_service.ExecutionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record("execution.broker_connected", actor=user.email, tenant_id=user.tenant_id, detail=f"{body.broker}/{conn['mode']}")
    return conn


@router.get("/brokers")
async def brokers(
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> list[dict]:
    return execution_service.list_connections(store, user.tenant_id)


@router.delete("/brokers/{conn_id}")
async def revoke(
    conn_id: str,
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    if not execution_service.revoke_connection(store, user.tenant_id, conn_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connexion introuvable")
    audit.record("execution.broker_revoked", actor=user.email, tenant_id=user.tenant_id, detail=conn_id)
    return {"revoked": True}


@router.post("/orders", status_code=status.HTTP_201_CREATED)
async def place_order(
    body: OrderRequest,
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    # Un ordre sur une connexion RÉELLE exige Elite + KYC ; le papier est libre.
    conn = next((c for c in execution_service.list_connections(store, user.tenant_id) if c["id"] == body.conn_id), None)
    if conn and conn.get("mode") == "live":
        _require_live_allowed(user, store)
    try:
        order = await execution_service.place_order(
            store, user.tenant_id, conn_id=body.conn_id, symbol=body.symbol, side=body.side,
            qty=body.qty, stop_loss=body.stop_loss, take_profit=body.take_profit,
        )
    except execution_service.ExecutionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record("execution.order_placed", actor=user.email, tenant_id=user.tenant_id, detail=f"{order['side']} {order['qty']} {order['symbol']} ({order['mode']})")
    return order


@router.post("/playbook/execute", status_code=status.HTTP_201_CREATED)
async def execute_playbook(
    count: int = 5,
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Ouvre EN COMPTE DÉMO les trades prêts du playbook, avec leur stop et leur objectif.

    Toujours en mode **papier** (aucun argent réel, aucune clé broker requise) : la connexion démo
    est créée automatiquement si elle n'existe pas. La taille de position découle du profil de
    risque (% du capital risqué au stop) ; le stop et le TP1 sont ceux calculés par la stratégie.

    Les garde-fous s'appliquent : nombre maximum de positions ouvertes, risque total plafonné,
    refus si les données de marché ne sont pas réelles, pas de doublon sur un même symbole/sens.
    Les setups « armés » (en attente du déclencheur 15 min) ne sont PAS ouverts et sont listés.
    """
    try:
        report = await execution_service.execute_playbook_trades(
            store, user.tenant_id, count=max(1, min(count, 10)),
        )
    except execution_service.ExecutionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record(
        "execution.playbook_executed", actor=user.email, tenant_id=user.tenant_id,
        detail=f"{len(report['opened'])} ouverte(s), {len(report['skipped'])} ignorée(s)",
    )
    return report


@router.get("/orders")
async def orders(
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> list[dict]:
    return execution_service.list_orders(store, user.tenant_id)


@router.post("/playbook/execute-symbol/{symbol:path}", status_code=status.HTTP_201_CREATED)
async def execute_playbook_one(
    symbol: str,
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Ouvre EN COMPTE DÉMO le trade d'un seul symbole — le bouton d'une carte « trade du jour ».

    La stratégie est recalculée côté serveur avant l'ouverture : les niveaux (entrée, stop,
    objectif) sont ceux du playbook au moment du clic, jamais ceux affichés dans le navigateur.
    Si le déclencheur 15 min n'est plus actif, rien n'est ouvert et la raison est renvoyée.
    """
    from app.data import symbols as symbols_catalog

    try:
        report = await execution_service.execute_playbook_symbol(
            store, user.tenant_id, symbols_catalog.normalize(symbol),
        )
    except execution_service.ExecutionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record(
        "execution.playbook_symbol", actor=user.email, tenant_id=user.tenant_id,
        detail=f"{symbol} -> {len(report['opened'])} ouverte(s)",
    )
    return report


@router.get("/positions")
async def positions(
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Photo complète des positions, avec le P&L LATENT calculé — aucun clic requis.

    Pensé pour un rafraîchissement automatique de la page Paper Trading : un seul appel renvoie,
    pour chaque trade, les niveaux choisis à l'ouverture (entrée, stop, objectif, R/R, montant
    risqué, gain visé) plus le prix actuel, le gain/perte latent, la progression vers l'objectif et
    le multiple de R. Les positions clôturées portent leur P&L réalisé.
    """
    return await execution_service.positions_snapshot(store, user.tenant_id)


@router.post("/orders/{order_id}/close")
async def close_order(
    order_id: str,
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Clôture manuelle d'une position papier au prix du marché (P&L réalisé immédiat)."""
    try:
        result = await execution_service.close_order_manual(store, user.tenant_id, order_id)
    except execution_service.MarketDataUnavailable as exc:
        # 503 et non 404 : la position EXISTE, c'est la cotation qui manque. L'opération est
        # reportée, pas refusée — et l'interface doit pouvoir le dire dans ces termes.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except execution_service.ExecutionError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    audit.record("execution.order_closed_manual", actor=user.email, tenant_id=user.tenant_id,
                 detail=f"{result['outcome']} {result['symbol']} pnl={result.get('realized_pnl')}")
    return result


@router.post("/orders/close-all")
async def close_all_orders(
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """CLÔTURE TOUTES les positions papier ouvertes du compte, au prix du marché.

    Mêmes règles que la clôture unitaire : prix réel obligatoire, aucun résultat inventé. Une
    position qu'on ne sait pas valoriser reste ouverte, et le rapport dit laquelle et pourquoi.
    """
    result = await execution_service.close_all_open(store, user.tenant_id)
    audit.record("execution.orders_closed_all", actor=user.email, tenant_id=user.tenant_id,
                 detail=f"{len(result['closed'])} fermée(s), P&L {result['realized_pnl']}")
    return result


@router.post("/orders/{order_id}/levels")
async def update_levels(
    order_id: str,
    body: LevelsRequest,
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Modifie MANUELLEMENT le stop et/ou l'objectif d'une position papier encore ouverte.

    Réservé au papier — ajuster un ordre réel passerait par le broker, pas par cette route. Le
    risque d'origine (ce que vaut 1R pour la sécurisation automatique) n'est jamais redéfini par
    cette modification ; seuls les niveaux affichés et le R/R informatif le sont.
    """
    try:
        result = await execution_service.update_order_levels(
            store, user.tenant_id, order_id,
            stop_loss=body.stop_loss, take_profit=body.take_profit,
        )
    except execution_service.ExecutionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record(
        "execution.order_levels_edited", actor=user.email, tenant_id=user.tenant_id,
        detail=f"{result['symbol']} SL={result.get('stop_loss')} TP={result.get('take_profit')}",
    )
    return result


@router.post("/orders/{order_id}/check")
async def check_order(
    order_id: str,
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Vérifie si le trade papier a gagné (TP) / perdu (SL) / est encore ouvert, depuis l'entrée."""
    try:
        result = await execution_service.check_order_outcome(store, user.tenant_id, order_id)
    except execution_service.ExecutionError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if result.get("outcome") in {"won", "lost"}:
        audit.record("execution.order_closed", actor=user.email, tenant_id=user.tenant_id,
                     detail=f"{result['outcome']} {result['symbol']} pnl={result.get('realized_pnl')}")
    return result
