"""AUTO-ENTRÉE — le robot prend le trade tout seul, en COMPTE DÉMO, sans aucun clic.

Principe : un setup « 🟡 ARMÉ » a déjà validé les étapes 1 à 3 de la stratégie (tendance de fond
mensuelle + journalière, niveaux majeurs, confirmation journalière, confirmation 4 h). Il ne lui
manque que le déclencheur 15 min — et ce déclencheur peut apparaître à n'importe quelle bougie.
Attendre qu'un humain rafraîchisse une page et clique, c'est rater l'entrée.

Cette veille tourne donc en continu (`playbook_auto_entry_interval`, 60 s par défaut) :

1. elle reprend les setups ARMÉS et PRÊTS du dernier instantané ;
2. elle **recalcule la stratégie** pour chacun (jamais sur des niveaux affichés, toujours sur les
   données du moment) ;
3. dès qu'un déclencheur 15 min est actif, elle ouvre la position en compte DÉMO avec son stop
   15 min et son objectif borné 1 h, dimensionnée au profil de risque de l'utilisateur ;
4. elle notifie (temps réel + push) et journalise l'ouverture.

GARDE-FOUS — l'auto-entrée ne touche QUE du papier :
- seules les connexions en mode ``paper`` sont utilisées ; une connexion ``live`` est ignorée, quel
  que soit son statut KYC. Aucun argent réel ne peut être engagé par cette boucle ;
- les protections de portefeuille s'appliquent (nombre de positions max, plafond de risque total) ;
- un même symbole/sens déjà ouvert n'est jamais doublé ;
- rien ne part sur données synthétiques (le playbook refuse déjà d'affirmer un trade dans ce cas).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.core.config import get_settings
from app.data import sessions as sessions_mod
from app.services import execution_service, playbook_service

logger = logging.getLogger(__name__)

EVENTS = "auto_entry_event"


def enabled() -> bool:
    s = get_settings()
    return s.playbook_auto_entry_enabled and s.playbook_auto_paper_execute


def _paper_tenants(store) -> list[str]:  # noqa: ANN001
    """Tenants éligibles à l'auto-entrée en démo.

    Avec `playbook_auto_entry_autoprovision`, tout tenant est éligible : le compte démo est créé au
    besoin (il n'engage aucun argent). Sinon, seuls ceux ayant déjà connecté un broker papier le
    sont — c'est-à-dire ceux qui ont donné leur accord explicite.
    """
    s = get_settings()
    try:
        tenants = sorted({u.tenant_id for u in store.users.list_all()})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Auto-entrée : liste des tenants indisponible (%s)", exc)
        return []
    if s.playbook_auto_entry_autoprovision:
        return tenants
    return [
        tid for tid in tenants
        if any(c.get("mode") == "paper" for c in execution_service.list_connections(store, tid))
    ]


async def _fresh_ready(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    """Recalcule la stratégie pour chaque candidat et sépare « déclencheur actif » du reste.

    On ne fait JAMAIS confiance aux niveaux d'un instantané pour passer un ordre : entre le calcul
    et l'ouverture, le prix a pu sortir de la zone. Tout est refait ici.
    """
    ready: list[dict] = []
    still_armed: list[dict] = []
    for cand in candidates:
        symbol = cand.get("symbol")
        if not symbol:
            continue
        try:
            setup = await playbook_service.build_setup(symbol)
        except Exception as exc:  # noqa: BLE001 — un symbole KO n'arrête pas la veille
            logger.warning("Auto-entrée : recalcul %s échoué (%s)", symbol, exc)
            continue
        pick = setup.as_dict()
        pick["asset_class"] = cand.get("asset_class", "")
        if setup.ready:
            pick["tier"] = "ready"
            ready.append(pick)
        elif setup.context_ok:
            pick["tier"] = "armed"
            still_armed.append(pick)
    return ready, still_armed


async def run_auto_entry(store, *, candidates: list[dict] | None = None) -> dict:  # noqa: ANN001
    """Un passage de veille : ouvre en démo tous les setups dont le déclencheur 15 min vient d'apparaître."""
    from app.services import live_snapshot

    if not enabled():
        return {"enabled": False, "opened": [], "armed": 0, "note": "auto-entrée désactivée"}

    # Heures de marché : on continue d'ANALYSER en permanence, mais on n'ouvre rien quand Londres
    # et New York sont fermées (ou le week-end). C'est le garde-fou le plus simple contre les
    # entrées dans un carnet vide.
    if get_settings().playbook_trade_only_when_open:
        tradable, why = sessions_mod.can_trade()
        if not tradable:
            return {"enabled": True, "opened": [], "armed": len(candidates or []),
                    "market_open": False, "note": f"aucune ouverture — {why}"}

    pool = candidates if candidates is not None else live_snapshot.armed_and_ready()
    if not pool:
        return {"enabled": True, "opened": [], "armed": 0,
                "note": "aucun setup armé à surveiller pour l'instant"}

    ready, still_armed = await _fresh_ready(pool)
    if not ready:
        return {
            "enabled": True, "opened": [], "armed": len(still_armed),
            "checked": len(pool),
            "note": (f"{len(still_armed)} setup(s) toujours armé(s) — aucun déclencheur 15 min "
                     "formé sur ce passage"),
        }

    # GATING PAR VERDICT (plan, tâche 2.2) : l'auto-entrée ne trade QUE les paires notées 🟢 par
    # le backtest hebdomadaire (espérance ≥ +0,4 R, n ≥ 20, deux passages consécutifs), et jamais
    # sur un déclencheur désactivé par la matrice paire × déclencheur. Les 🟡 restent analysées et
    # affichées, les 🔴 sont exclues. Chaque refus est journalisé (« trades évités »).
    from app.services import verdict_service

    ready, gate_refused = verdict_service.filter_auto_ready(store, ready)
    if not ready:
        return {
            "enabled": True, "opened": [], "armed": len(still_armed),
            "checked": len(pool), "gate_refused": gate_refused,
            "note": (
                f"{len(gate_refused)} déclencheur(s) formé(s) mais refusé(s) par le verdict de "
                "paire (seules les paires 🟢 sont auto-tradées) — refus journalisés."
            ),
        }

    opened_all: list[dict] = []
    for tenant_id in _paper_tenants(store):
        try:
            report = await execution_service.execute_playbook_trades(
                store, tenant_id, count=len(ready), picks=ready,
            )
        except Exception as exc:  # noqa: BLE001 — un tenant en échec n'arrête pas les autres
            logger.warning("Auto-entrée : tenant %s échoué (%s)", tenant_id, exc)
            continue
        for order in report.get("opened") or []:
            order["tenant_id"] = tenant_id
            opened_all.append(order)
            await _announce(store, tenant_id, order)

    if opened_all:
        logger.info(
            "Auto-entrée : %d position(s) ouverte(s) automatiquement en démo (%s)",
            len(opened_all), ", ".join(sorted({o["symbol"] for o in opened_all})),
        )
    return {
        "enabled": True,
        "checked": len(pool),
        "triggered": [p["symbol"] for p in ready],
        "opened": opened_all,
        "armed": len(still_armed),
        "gate_refused": gate_refused,
        "at": datetime.now(UTC).isoformat(),
        "note": (
            f"{len(opened_all)} position(s) ouverte(s) automatiquement en démo sur "
            f"{len(ready)} déclencheur(s) 15 min formé(s)."
            if opened_all else
            f"{len(ready)} déclencheur(s) formé(s) mais aucune ouverture (position déjà en cours "
            "ou garde-fou de portefeuille)."
        ),
    }


async def _announce(store, tenant_id: str, order: dict) -> None:  # noqa: ANN001
    """Diffuse l'ouverture automatique : bus temps réel, notification push, et trace persistée."""
    from app.realtime import bus

    msg = (
        f"🤖 Entrée AUTOMATIQUE en démo — {order['symbol']} {order['side'].upper()} "
        f"@ {order['entry']} · SL {order['stop_loss']} · TP {order['take_profit']} "
        f"(R/R 1:{order.get('risk_reward')})"
    )
    try:
        store.records.put(
            EVENTS, f"{tenant_id}:{order['order_id']}",
            {"symbol": order["symbol"], "side": order["side"], "entry": order.get("entry"),
             "stop_loss": order.get("stop_loss"), "take_profit": order.get("take_profit"),
             "trigger": order.get("trigger"), "message": msg},
            tenant_id=tenant_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Auto-entrée : trace non persistée (%s)", exc)
    try:
        await bus.publish(tenant_id, {"type": "auto_entry", "data": order})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Auto-entrée : diffusion échouée (%s)", exc)
    try:
        from app.alerts import notifier

        user = next((u for u in store.users.list_by_tenant(tenant_id)), None)
        if user and getattr(user, "push_token", None):
            await notifier.send_push(user.push_token, msg)
        if user and getattr(user, "alert_telegram", False) and getattr(user, "telegram_chat_id", None):
            await notifier.send_telegram(user.telegram_chat_id, msg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Auto-entrée : notification échouée (%s)", exc)


def recent_events(store, tenant_id: str, limit: int = 20) -> list[dict]:  # noqa: ANN001
    """Historique des entrées automatiques (ce que le robot a fait pendant ton absence)."""
    return store.records.list(EVENTS, tenant_id)[:limit]
