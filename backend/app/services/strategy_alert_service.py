"""Alertes de LA stratégie du desk — transforme l'outil en assistant quotidien.

Le desk n'applique qu'une stratégie : le playbook. Ce service surveille les paires de chaque
utilisateur (watchlist) sur le marché en direct et envoie une alerte dès que le playbook passe à un
NOUVEAU signal directionnel (BUY/SELL), par email / Telegram / push selon ses préférences.
Anti-spam : on ne notifie qu'au CHANGEMENT d'état.

Les niveaux annoncés sont ceux du playbook lui-même (stop posé sur le niveau qui invalide le
scénario, objectifs devant le premier obstacle réel) — pas un calcul séparé qui dirait autre chose
que ce que la stratégie exécute réellement.
"""

from __future__ import annotations

import logging

from app.alerts import notifier
from app.data import markets

logger = logging.getLogger(__name__)

_STATE = "strategy_alert_state"
_DEFAULT_SYMBOLS = ["EUR/USD", "XAU/USD", "SPX500"]
STRATEGY_ID = "playbook"


async def check_strategy_alerts(store) -> int:
    """Surveille la watchlist de chaque utilisateur et notifie les nouveaux signaux. -> nb d'alertes."""
    sent = 0
    try:
        users = store.users.list_all()
    except Exception:  # noqa: BLE001
        return 0

    for user in users:
        symbols = (getattr(user, "watchlist", None) or _DEFAULT_SYMBOLS)[:5]
        for symbol in symbols:
            try:
                if await _check_symbol(store, user, symbol):
                    sent += 1
            except Exception as exc:  # noqa: BLE001 — un symbole ne bloque pas les autres
                logger.warning("Alerte stratégie %s/%s échouée (%s)", user.tenant_id, symbol, exc)
    return sent


async def _check_symbol(store, user, symbol: str) -> bool:
    from app.services import playbook_service

    setup = await playbook_service.build_setup(symbol)
    # On n'alerte jamais sur des données non réelles, ni sur un setup que la stratégie refuse.
    if setup.insufficient or not markets.is_real(symbol):
        return False
    key = f"{user.tenant_id}:{symbol}:{STRATEGY_ID}"
    prev = (store.records.get(_STATE, key) or {}).get("direction")
    new_dir = setup.direction

    # Mémorise l'état courant (pour détecter les changements).
    store.records.put(_STATE, key, {"direction": new_dir, "strategy": STRATEGY_ID},
                      tenant_id=user.tenant_id)

    # Alerte uniquement sur un NOUVEAU signal directionnel, et seulement s'il est exécutable.
    if new_dir == "NO_TRADE" or new_dir == prev or not setup.ready:
        return False

    entry = setup.entry
    levels = setup
    msg = (
        f"📊 Playbook — {symbol} : signal {new_dir}\n"
        f"Entrée ~{round(entry, 6)} | SL {setup.stop_loss} | TP {setup.take_profit_1} "
        f"(R/R 1:{setup.risk_reward:.2f}). Déclencheur : {setup.trigger}. "
        "Aide à la décision, pas un conseil."
    )
    await _notify(user, f"Signal {new_dir} — {symbol}", msg)
    logger.info("Alerte stratégie envoyée : %s %s %s", user.tenant_id, symbol, new_dir)

    # FORWARD TEST AUTO (opt-in) : ouvre le trade PAPIER automatiquement (risque 1%, SL/TP inclus).
    # C'est le juge final de l'edge : des semaines de trades réels simulés, sans intervention.
    if (store.records.get("auto_trade", user.tenant_id) or {}).get("enabled"):
        from app.core.config import get_settings
        from app.services import edge_map_service

        s = get_settings()
        # Règle d'or : on n'auto-trade QUE les combos verts de la carte de l'edge (alpha>0 +
        # PF>=1,2 out-of-sample). Ailleurs : alerte seulement, pas de trade.
        if s.auto_trade_green_only and not edge_map_service.is_combo_green(
            store, STRATEGY_ID, symbol, min_streak=s.edge_min_green_streak
        ):
            logger.info("Auto-trade ignoré (%s pas vert sur la carte de l'edge)", symbol)
        else:
            await _auto_paper_trade(store, user, symbol, new_dir, levels)
    return True


async def _auto_paper_trade(store, user, symbol: str, direction: str, setup) -> None:
    """Ouvre le trade papier correspondant au signal, avec LES niveaux du playbook.

    Le dimensionnement se fait sur la distance au stop réelle du setup : c'est la seule façon que
    le risque engagé vaille exactement le pourcentage voulu du capital.
    """
    from app.services import execution_service

    try:
        conns = [c for c in execution_service.list_connections(store, user.tenant_id) if c["mode"] == "paper"]
        conn = conns[0] if conns else execution_service.connect_broker(
            store, user.tenant_id, broker="paper", api_key="", api_secret="", mode="paper")
        stop_dist = abs(setup.entry - setup.stop_loss)
        if stop_dist <= 0:
            return
        qty = round((user.capital * 0.01) / stop_dist, 6)   # 1 % du capital risqué au stop
        if qty <= 0:
            return
        await execution_service.place_order(
            store, user.tenant_id, conn_id=conn["id"], symbol=symbol,
            side="buy" if direction == "BUY" else "sell", qty=qty,
            stop_loss=setup.stop_loss, take_profit=setup.take_profit_1,
        )
        logger.info("Auto-trade papier ouvert : %s %s qty=%.6f", symbol, direction, qty)
    except Exception as exc:  # noqa: BLE001 — garde-fous (exposition, synthétique) peuvent refuser : normal
        logger.info("Auto-trade papier refusé/échoué (%s) : %s", symbol, exc)


async def _notify(user, subject: str, msg: str) -> None:
    if getattr(user, "alert_email", False) and user.email:
        await notifier.send_email(user.email, subject, msg)
    if getattr(user, "alert_telegram", False) and getattr(user, "telegram_chat_id", None):
        await notifier.send_telegram(user.telegram_chat_id, msg)
    if getattr(user, "push_token", None):
        await notifier.send_push(user.push_token, msg)
