"""Portefeuille virtuel (paper) — un VRAI compte simulé pour mesurer la fiabilité.

Agrège tous les ordres papier de l'utilisateur en un solde qui évolue exactement comme un compte
réel : solde de départ + P&L réalisé des trades clôturés, équité = solde + P&L latent des positions
ouvertes, plus les statistiques (taux de réussite, profit factor, meilleur/pire trade, courbe
d'équité). C'est le juge de paix : après des dizaines de trades, le solde dit la vérité.
"""

from __future__ import annotations

import logging

from app.data import markets
from app.repositories.store import AppStore

logger = logging.getLogger(__name__)

WALLET = "wallet_config"
ORDER = "order"
_DEFAULT_BALANCE = 10_000.0


def get_config(store: AppStore, tenant_id: str) -> dict:
    return store.records.get(WALLET, tenant_id) or {"starting_balance": _DEFAULT_BALANCE}


def reset(store: AppStore, tenant_id: str, starting_balance: float, clear_orders: bool) -> dict:
    """Réinitialise le portefeuille : nouveau solde de départ et, au choix, efface l'historique papier."""
    if clear_orders:
        for o in store.records.list(ORDER, tenant_id):
            if o.get("mode") == "paper":
                store.records.delete(ORDER, o["id"])
    return store.records.put(WALLET, tenant_id, {"starting_balance": float(starting_balance)}, tenant_id=tenant_id)


async def _last_price(symbol: str) -> float | None:
    try:
        candles = await markets.load_candles(symbol, interval="1h", limit=2)
        return candles[-1].close if candles else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Prix %s indisponible (%s)", symbol, exc)
        return None


async def compute_wallet(store: AppStore, tenant_id: str) -> dict:
    start = float(get_config(store, tenant_id).get("starting_balance", _DEFAULT_BALANCE))
    orders = [o for o in store.records.list(ORDER, tenant_id) if o.get("mode") == "paper"]
    # Issues NEUTRES (`invalid` : clôture que le marché n'a jamais produite ; `reset` : position
    # neutralisée par une remise à zéro de l'auto-entrée). Ni ouvertes, ni comptées dans les
    # statistiques : elles n'ont produit aucun résultat.
    from app.services.execution_service import NEUTRAL_OUTCOMES

    closed = [o for o in orders if o.get("outcome") in ("won", "lost")]
    open_orders = [o for o in orders
                   if o.get("outcome") not in ("won", "lost", *NEUTRAL_OUTCOMES)]

    realized = sum(float(o.get("realized_pnl") or 0) for o in closed)
    wins = [o for o in closed if o.get("outcome") == "won"]
    losses = [o for o in closed if o.get("outcome") == "lost"]
    gross_win = sum(float(o.get("realized_pnl") or 0) for o in wins)
    gross_loss = abs(sum(float(o.get("realized_pnl") or 0) for o in losses))

    # Positions ouvertes : P&L latent au dernier prix.
    positions: list[dict] = []
    unrealized = 0.0
    price_cache: dict[str, float | None] = {}
    for o in open_orders:
        sym = o.get("symbol", "")
        if sym not in price_cache:
            price_cache[sym] = await _last_price(sym)
        price = price_cache[sym]
        entry = float(o.get("entry") or o.get("filled_price") or 0)
        qty = float(o.get("qty") or 0)
        side = o.get("side")
        pnl = ((price - entry) if side == "buy" else (entry - price)) * qty if price else 0.0
        unrealized += pnl
        positions.append({
            "id": o["id"], "symbol": sym, "side": side, "entry": entry, "qty": qty,
            "current_price": price, "stop_loss": o.get("stop_loss"), "take_profit": o.get("take_profit"),
            "unrealized_pnl": round(pnl, 2),
        })

    balance = start + realized
    equity = balance + unrealized
    n = len(closed)
    win_rate = round(len(wins) / n * 100, 1) if n else 0.0
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else (round(gross_win, 2) if gross_win else 0.0)
    best = max((float(o.get("realized_pnl") or 0) for o in closed), default=0.0)
    worst = min((float(o.get("realized_pnl") or 0) for o in closed), default=0.0)

    # Courbe d'équité (réalisée, chronologique).
    curve = []
    running = start
    for o in sorted(closed, key=lambda x: x.get("closed_at") or ""):
        running += float(o.get("realized_pnl") or 0)
        curve.append({"t": o.get("closed_at"), "equity": round(running, 2),
                      "symbol": o.get("symbol"), "outcome": o.get("outcome"), "pnl": round(float(o.get("realized_pnl") or 0), 2)})

    return {
        "starting_balance": round(start, 2),
        "balance": round(balance, 2),            # solde après trades clôturés
        "equity": round(equity, 2),              # solde + positions ouvertes
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "return_pct": round((equity - start) / start * 100, 2) if start else 0.0,
        "stats": {
            "trades": n, "wins": len(wins), "losses": len(losses),
            "win_rate": win_rate, "profit_factor": profit_factor,
            "open_positions": len(open_orders),
            "best_trade": round(best, 2), "worst_trade": round(worst, 2),
        },
        "positions": positions,
        "equity_curve": curve,
    }
