"""Service d'exécution broker (M8, Phase 4) — orchestration + garde-fous.

Collections (via store.records) :
- broker_conn : connexions broker (clés API CHIFFRÉES, mode paper/live)
- order       : ordres passés (paper ou réel)
- kyc         : statut KYC/AML par tenant

Garde-fous appliqués ici :
- les clés ne sont jamais stockées en clair (crypto.encrypt) ni renvoyées (mask) ;
- l'exécution réelle exige mode 'live' + KYC vérifié ; sinon on force/refuse le papier.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from app.core import crypto
from app.execution.alpaca import AlpacaBroker
from app.execution.base import OrderResult
from app.execution.paper import PaperBroker
from app.repositories.store import AppStore

logger = logging.getLogger(__name__)

CONN = "broker_conn"
ORDER = "order"
KYC = "kyc"

_SUPPORTED = {"paper", "alpaca"}


class ExecutionError(RuntimeError):
    """Erreur métier d'exécution (garde-fou non satisfait, connexion absente…)."""


# ---------------- KYC ----------------
def kyc_status(store: AppStore, tenant_id: str) -> dict:
    return store.records.get(KYC, tenant_id) or {"status": "none"}


def submit_kyc(store: AppStore, tenant_id: str, *, legal_name: str, country: str, doc_id: str) -> dict:
    # Démo : vérification automatique si les champs requis sont fournis (en prod : fournisseur KYC/AML).
    complete = bool(legal_name.strip() and country.strip() and doc_id.strip())
    status = "verified" if complete else "pending"
    return store.records.put(
        KYC, tenant_id,
        {"status": status, "legal_name": legal_name, "country": country},
        tenant_id=tenant_id,
    )


def is_kyc_verified(store: AppStore, tenant_id: str) -> bool:
    return kyc_status(store, tenant_id).get("status") == "verified"


# ---------------- Connexions broker ----------------
def connect_broker(
    store: AppStore, tenant_id: str, *, broker: str, api_key: str, api_secret: str, mode: str
) -> dict:
    if broker not in _SUPPORTED:
        raise ExecutionError(f"broker non supporté : {broker}")
    mode = "live" if mode == "live" else "paper"
    if mode == "live" and not is_kyc_verified(store, tenant_id):
        raise ExecutionError("KYC non vérifié : connexion réelle interdite (mode papier autorisé)")
    conn_id = str(uuid.uuid4())
    store.records.put(
        CONN, conn_id,
        {
            "broker": broker,
            "mode": mode,
            "api_key_enc": crypto.encrypt(api_key) if api_key else "",
            "api_secret_enc": crypto.encrypt(api_secret) if api_secret else "",
            "key_hint": crypto.mask(api_key),
        },
        tenant_id=tenant_id,
    )
    return public_connection(store.records.get(CONN, conn_id))


def public_connection(rec: dict) -> dict:
    """Vue sans secrets (pour l'API)."""
    return {
        "id": rec["id"],
        "broker": rec.get("broker"),
        "mode": rec.get("mode"),
        "key_hint": rec.get("key_hint", ""),
        "created_at": rec.get("created_at"),
    }


def list_connections(store: AppStore, tenant_id: str) -> list[dict]:
    return [public_connection(r) for r in store.records.list(CONN, tenant_id)]


def revoke_connection(store: AppStore, tenant_id: str, conn_id: str) -> bool:
    rec = store.records.get(CONN, conn_id)
    if rec is None or rec.get("tenant_id") != tenant_id:
        return False
    return store.records.delete(CONN, conn_id)


def _build_broker(rec: dict):
    broker = rec.get("broker")
    mode = rec.get("mode", "paper")
    if broker == "paper" or mode == "paper":
        return PaperBroker()
    if broker == "alpaca":
        return AlpacaBroker(
            crypto.decrypt(rec["api_key_enc"]) if rec.get("api_key_enc") else "",
            crypto.decrypt(rec["api_secret_enc"]) if rec.get("api_secret_enc") else "",
            mode="live",
        )
    raise ExecutionError(f"broker non supporté : {broker}")


# ---------------- Ordres ----------------
async def place_order(
    store: AppStore, tenant_id: str, *, conn_id: str, symbol: str, side: str, qty: float,
    stop_loss: float | None = None, take_profit: float | None = None,
) -> dict:
    if side not in {"buy", "sell"}:
        raise ExecutionError("side invalide (buy|sell)")
    if qty <= 0:
        raise ExecutionError("quantité invalide")
    rec = store.records.get(CONN, conn_id)
    if rec is None or rec.get("tenant_id") != tenant_id:
        raise ExecutionError("connexion broker introuvable")

    # Garde-fou réel : KYC obligatoire pour live ; sinon on rétrograde en papier.
    if rec.get("mode") == "live" and not is_kyc_verified(store, tenant_id):
        raise ExecutionError("KYC requis pour l'exécution réelle")

    # Garde-fou qualité des données : on refuse de trader sur des données synthétiques (démo).
    from app.core.config import get_settings
    from app.data import markets

    if get_settings().block_synthetic_orders:
        await markets.load_candles(symbol, interval="1h", limit=60)  # rafraîchit la source
        if not markets.is_real(symbol):
            raise ExecutionError(
                f"Données indisponibles ou synthétiques pour {symbol} — trade refusé. "
                "Configure une source réelle (clé broker/data) avant de trader ce marché."
            )

    broker = _build_broker(rec)
    result: OrderResult = await broker.place_order(symbol, side, qty)
    # Garde-fou portefeuille (paper) : limite le nombre de positions et l'exposition totale.
    _portfolio_check(store, tenant_id, result)
    levels = _trade_levels(result, side, stop_loss, take_profit)  # valide + calcule R/R, risque, gain
    return _persist_order(store, tenant_id, conn_id, result, levels)


def _portfolio_check(store: AppStore, tenant_id: str, result: OrderResult) -> None:
    """Refuse l'ordre si trop de positions ouvertes ou exposition totale dépassée (protection capital)."""
    from app.core.config import get_settings

    s = get_settings()
    if not s.paper_portfolio_guard:
        return
    open_orders = [
        o for o in store.records.list(ORDER, tenant_id)
        if o.get("mode") == "paper" and o.get("outcome") not in ("won", "lost")
    ]
    if len(open_orders) >= s.paper_max_positions:
        raise ExecutionError(
            f"Limite de {s.paper_max_positions} positions ouvertes atteinte — clôture-en une avant d'en ouvrir une nouvelle."
        )
    users = store.users.list_by_tenant(tenant_id)
    capital = users[0].capital if users else 0.0
    # Plafond d'exposition = celui choisi par l'utilisateur (Paramètres), sinon le défaut global.
    max_exposure = getattr(users[0], "max_exposure_pct", None) if users else None
    max_exposure = max_exposure or s.paper_max_exposure_pct
    if capital > 0:
        # Exposition = RISQUE total ouvert (ce qu'on perdrait si tous les stops sautaient), PAS le
        # notionnel. Une position dimensionnée à 1% de risque ne pèse que ~1% ici (vs un gros
        # notionnel dû au levier implicite d'un stop serré). Définition professionnelle du risque.
        open_risk = sum(float(o.get("risk_amount") or 0) for o in open_orders)
        risk_pct = open_risk / capital * 100
        if risk_pct > max_exposure:
            raise ExecutionError(
                f"Risque total ouvert {risk_pct:.0f}% > ton plafond {max_exposure:.0f}% du capital — "
                f"clôture une position ou relève le plafond dans Paramètres."
            )


def _trade_levels(result: OrderResult, side: str, stop_loss: float | None, take_profit: float | None) -> dict:
    """Valide la cohérence SL/TP vs entrée et calcule les infos du trade (R/R, risque, gain potentiel)."""
    entry = result.filled_price or 0.0
    info: dict = {
        "entry": entry, "stop_loss": stop_loss, "take_profit": take_profit,
        "risk_reward": None, "risk_amount": None, "potential_profit": None,
    }
    if not entry:
        return info
    # Cohérence directionnelle (comme un vrai bracket order).
    if side == "buy":
        if stop_loss is not None and stop_loss >= entry:
            raise ExecutionError(f"Achat : le stop loss ({stop_loss}) doit être SOUS l'entrée ({entry}).")
        if take_profit is not None and take_profit <= entry:
            raise ExecutionError(f"Achat : le take profit ({take_profit}) doit être AU-DESSUS de l'entrée ({entry}).")
    else:  # sell
        if stop_loss is not None and stop_loss <= entry:
            raise ExecutionError(f"Vente : le stop loss ({stop_loss}) doit être AU-DESSUS de l'entrée ({entry}).")
        if take_profit is not None and take_profit >= entry:
            raise ExecutionError(f"Vente : le take profit ({take_profit}) doit être SOUS l'entrée ({entry}).")

    risk_per_unit = abs(entry - stop_loss) if stop_loss is not None else None
    reward_per_unit = abs(take_profit - entry) if take_profit is not None else None
    if risk_per_unit:
        info["risk_amount"] = round(risk_per_unit * result.qty, 2)
    if reward_per_unit:
        info["potential_profit"] = round(reward_per_unit * result.qty, 2)
    if risk_per_unit and reward_per_unit:
        info["risk_reward"] = round(reward_per_unit / risk_per_unit, 2)
    return info


def _persist_order(store: AppStore, tenant_id: str, conn_id: str, result: OrderResult, levels: dict | None = None) -> dict:
    from app.core import metrics
    metrics.inc("orders_placed_total", mode=result.mode, side=result.side)
    order_id = str(uuid.uuid4())
    return store.records.put(
        ORDER, order_id,
        {
            "conn_id": conn_id,
            "broker": result.broker,
            "mode": result.mode,
            "symbol": result.symbol,
            "side": result.side,
            "qty": result.qty,
            "status": result.status,
            "filled_price": result.filled_price,
            **(levels or {}),
        },
        tenant_id=tenant_id,
    )


def list_orders(store: AppStore, tenant_id: str, limit: int = 100) -> list[dict]:
    return store.records.list(ORDER, tenant_id)[:limit]


async def check_order_outcome(store: AppStore, tenant_id: str, order_id: str) -> dict:
    """Vérifie si un trade papier a touché son TP (gagné) ou son SL (perdu) depuis l'entrée.

    Rejoue l'action du prix (cf. data/replay.py) : TP -> ``won``, SL -> ``lost``, sinon ``open``
    (P&L latent). Le résultat clôturé est PERSISTÉ sur l'ordre (statut/issue/P&L réalisé)."""
    from datetime import UTC, datetime

    from app.data import replay

    rec = store.records.get(ORDER, order_id)
    if rec is None or rec.get("tenant_id") != tenant_id:
        raise ExecutionError("ordre introuvable")
    if rec.get("outcome") in {"won", "lost"}:
        return rec  # déjà clôturé

    entry = rec.get("entry") if rec.get("entry") is not None else rec.get("filled_price")
    sl, tp = rec.get("stop_loss"), rec.get("take_profit")
    side, qty = rec.get("side"), rec.get("qty") or 0.0
    if entry is None or (sl is None and tp is None):
        return {**rec, "outcome": "open", "note": "Aucun SL/TP : vérification automatique impossible."}

    outcome, exit_price, closed_ts = await replay.replay_outcome(
        rec["symbol"], side, entry, sl, tp, rec.get("created_at"),
    )
    if outcome == "open":
        unrealized = (exit_price - entry) * qty if side == "buy" else (entry - exit_price) * qty
        return {**rec, "outcome": "open", "current_price": round(exit_price, 8), "unrealized_pnl": round(unrealized, 2)}

    realized = (exit_price - entry) * qty if side == "buy" else (entry - exit_price) * qty
    updated = {
        **rec, "outcome": outcome, "status": "closed", "exit_price": exit_price,
        "realized_pnl": round(realized, 2),
        "closed_at": datetime.fromtimestamp(closed_ts, UTC).isoformat() if closed_ts else None,
    }
    from app.core import metrics
    metrics.inc("paper_orders_closed_total", outcome=outcome)
    return store.records.put(ORDER, order_id, updated, tenant_id=tenant_id)


async def close_order_manual(store: AppStore, tenant_id: str, order_id: str) -> dict:
    """Clôture MANUELLE d'une position papier au prix du marché courant (P&L réalisé immédiat)."""
    from datetime import UTC, datetime

    from app.data import markets

    rec = store.records.get(ORDER, order_id)
    if rec is None or rec.get("tenant_id") != tenant_id:
        raise ExecutionError("ordre introuvable")
    if rec.get("outcome") in {"won", "lost"}:
        return rec  # déjà clôturé

    entry = rec.get("entry") if rec.get("entry") is not None else rec.get("filled_price")
    qty = rec.get("qty") or 0.0
    side = rec.get("side")
    candles = await markets.load_candles(rec["symbol"], interval="1h", limit=2)
    price = candles[-1].close if candles else (entry or 0.0)
    pnl = ((price - entry) if side == "buy" else (entry - price)) * qty
    outcome = "won" if pnl >= 0 else "lost"
    updated = {
        **rec, "outcome": outcome, "status": "closed", "exit_price": round(price, 8),
        "realized_pnl": round(pnl, 2), "closed_at": datetime.now(UTC).isoformat(), "closed_manually": True,
    }
    from app.core import metrics
    metrics.inc("paper_orders_closed_total", outcome=outcome)
    return store.records.put(ORDER, order_id, updated, tenant_id=tenant_id)


# ---------------- Exécution DÉMO des trades du playbook ----------------
_RISK_PCT = {"conservative": 0.5, "moderate": 1.0, "aggressive": 2.0}


def ensure_paper_connection(store: AppStore, tenant_id: str) -> str:
    """Retourne l'id d'une connexion PAPIER, en la créant au besoin (compte démo sans risque)."""
    existing = next(
        (c for c in list_connections(store, tenant_id) if c.get("mode") == "paper"), None
    )
    if existing:
        return existing["id"]
    return connect_broker(
        store, tenant_id, broker="paper", api_key="", api_secret="", mode="paper"
    )["id"]


async def _reference_price(symbol: str) -> float | None:
    """Dernier prix connu, à la MÊME source que celle du broker papier (cohérence du fill)."""
    from app.data import markets

    try:
        candles = await markets.load_candles(symbol, interval="1h", limit=60)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Prix de référence %s indisponible (%s)", symbol, exc)
        return None
    return candles[-1].close if candles else None


def _already_open(store: AppStore, tenant_id: str, symbol: str, side: str) -> bool:
    """Évite d'empiler deux fois le même trade (relances successives de la veille de session)."""
    return any(
        o.get("mode") == "paper" and o.get("symbol") == symbol and o.get("side") == side
        and o.get("outcome") not in ("won", "lost")
        for o in store.records.list(ORDER, tenant_id)
    )


async def execute_playbook_trades(
    store: AppStore, tenant_id: str, *, count: int = 5, picks: list[dict] | None = None,
) -> dict:
    """Ouvre en COMPTE DÉMO (papier) les setups PRÊTS du playbook, avec leur stop et leur TP1.

    - la taille de position vient du profil de risque de l'utilisateur (% du capital risqué au stop) ;
    - le stop et l'objectif sont ceux calculés par la stratégie (jamais recalculés ici) ;
    - les garde-fous habituels s'appliquent (nb max de positions, risque total, données réelles) ;
    - un même symbole/sens déjà ouvert n'est pas repris.

    Retourne un rapport ligne par ligne : ce qui a été ouvert, et pourquoi le reste ne l'a pas été.
    """
    from app.services import playbook_service

    if picks is None:
        payload = await playbook_service.top_trades(count)
        picks = payload.get("picks") or []
        session = payload.get("session")
        note = payload.get("note")
    else:
        session, note = None, None

    ready = [p for p in picks if p.get("tier") == "ready" and p.get("entry")][:count]
    users = store.users.list_by_tenant(tenant_id)
    if not users:
        raise ExecutionError("aucun utilisateur pour ce tenant")
    user = users[0]
    capital = user.capital or 0.0
    risk_pct = _RISK_PCT.get(getattr(user, "risk_profile", "moderate"), 1.0)
    risk_amount = capital * risk_pct / 100

    conn_id = ensure_paper_connection(store, tenant_id)
    opened: list[dict] = []
    skipped: list[dict] = []

    for p in ready:
        symbol, direction = p["symbol"], p["direction"]
        side = "buy" if direction == "BUY" else "sell"
        entry, sl, tp = p["entry"], p["stop_loss"], p["take_profit_1"]
        if not (entry and sl and tp):
            skipped.append({"symbol": symbol, "reason": "niveaux incomplets"})
            continue
        if _already_open(store, tenant_id, symbol, side):
            skipped.append({"symbol": symbol, "reason": "position identique déjà ouverte"})
            continue
        # Prix de référence = celui auquel le broker papier remplira réellement. On dimensionne
        # dessus pour que le montant risqué au stop vaille EXACTEMENT le % voulu du capital.
        fill = await _reference_price(symbol) or entry
        # Le prix a-t-il déjà invalidé le plan pendant le calcul ? (stop franchi ou objectif atteint)
        if (side == "buy" and (fill <= sl or fill >= tp)) or (side == "sell" and (fill >= sl or fill <= tp)):
            skipped.append({
                "symbol": symbol,
                "reason": f"prix ({fill:.6g}) déjà sorti de la zone d'entrée (SL {sl:.6g} / TP {tp:.6g})",
            })
            continue
        stop_dist = abs(fill - sl)
        if stop_dist <= 0:
            skipped.append({"symbol": symbol, "reason": "distance au stop nulle"})
            continue
        qty = round(risk_amount / stop_dist, 8)
        if qty <= 0:
            skipped.append({"symbol": symbol, "reason": "capital insuffisant pour dimensionner"})
            continue
        try:
            order = await place_order(
                store, tenant_id, conn_id=conn_id, symbol=symbol, side=side, qty=qty,
                stop_loss=sl, take_profit=tp,
            )
        except ExecutionError as exc:
            skipped.append({"symbol": symbol, "reason": str(exc)})
            continue
        opened.append({
            "order_id": order["id"], "symbol": symbol, "side": side, "qty": qty,
            "entry": order.get("filled_price"), "stop_loss": sl, "take_profit": tp,
            "risk_reward": p.get("risk_reward"), "target_pips": p.get("reward_pips"),
            "stop_pips": p.get("risk_pips"), "pips_label": p.get("pips_label"),
            "risk_amount": order.get("risk_amount"), "potential_profit": order.get("potential_profit"),
            "trigger": p.get("trigger"), "horizon_days": p.get("horizon_days"),
        })

    armed = [p for p in picks if p.get("tier") == "armed"]
    return {
        "mode": "paper",
        "connection_id": conn_id,
        "requested": count,
        "opened": opened,
        "skipped": skipped,
        "armed_waiting": [{"symbol": p["symbol"], "direction": p["direction"],
                           "reason": (p.get("reasons") or ["déclencheur 15 min non formé"])[0]}
                          for p in armed],
        "session": session,
        "note": note,
        "summary": (
            f"{len(opened)} position(s) ouverte(s) en démo sur {len(ready)} setup(s) exécutable(s) ; "
            f"{len(armed)} en attente du déclencheur 15 min."
        ),
    }


async def positions_snapshot(store: AppStore, tenant_id: str, limit: int = 100) -> dict:
    """Photo COMPLÈTE des positions, prête à afficher — sans aucun clic de vérification.

    Pour chaque ordre : les niveaux CHOISIS à l'ouverture (entrée, stop, objectif, R/R, montant
    risqué, gain visé), puis, pour les positions encore ouvertes, le prix actuel, le P&L latent et
    la progression vers l'objectif. Les positions clôturées portent leur P&L réalisé.

    Un seul appel réseau côté interface -> la page peut se rafraîchir toute seule.
    """
    orders = list_orders(store, tenant_id, limit)
    prices: dict[str, float | None] = {}
    out: list[dict] = []
    open_pnl = realized_pnl = 0.0
    wins = losses = 0

    for rec in orders:
        row = dict(rec)
        entry = rec.get("entry") if rec.get("entry") is not None else rec.get("filled_price")
        sl, tp = rec.get("stop_loss"), rec.get("take_profit")
        side, qty = rec.get("side"), rec.get("qty") or 0.0
        closed = rec.get("outcome") in {"won", "lost"}
        row["closed"] = closed
        if closed:
            realized_pnl += float(rec.get("realized_pnl") or 0.0)
            wins += 1 if rec.get("outcome") == "won" else 0
            losses += 1 if rec.get("outcome") == "lost" else 0
            out.append(row)
            continue

        symbol = rec.get("symbol", "")
        if symbol not in prices:
            prices[symbol] = await _reference_price(symbol)
        price = prices[symbol]
        if price and entry:
            pnl = (price - entry) * qty if side == "buy" else (entry - price) * qty
            row["current_price"] = round(price, 8)
            row["unrealized_pnl"] = round(pnl, 2)
            row["pnl_pct"] = round((price / entry - 1) * 100 * (1 if side == "buy" else -1), 3)
            open_pnl += pnl
            # Progression vers l'objectif (0 % à l'entrée, 100 % sur le TP, négatif vers le stop).
            if tp is not None and abs(tp - entry) > 0:
                row["progress_pct"] = round((price - entry) / (tp - entry) * 100, 1)
            if sl is not None and abs(entry - sl) > 0:
                row["r_multiple"] = round((price - entry) / (entry - sl), 2) if side == "buy" \
                    else round((entry - price) / (sl - entry), 2)
        row["outcome"] = "open"
        out.append(row)

    return {
        "positions": out,
        "open_count": sum(1 for o in out if not o["closed"]),
        "closed_count": sum(1 for o in out if o["closed"]),
        "unrealized_pnl": round(open_pnl, 2),
        "realized_pnl": round(realized_pnl, 2),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) else None,
        "as_of": datetime.now(UTC).isoformat(),
    }


async def monitor_positions(store: AppStore) -> int:
    """Parcourt tous les ordres papier OUVERTS avec SL/TP et clôture ceux dont le niveau est atteint.

    Diffuse un événement temps réel + notifie l'utilisateur à chaque clôture automatique.
    Retourne le nombre d'ordres clôturés sur ce passage."""
    closed = 0
    for rec in store.records.list(ORDER):  # tous tenants
        if rec.get("mode") != "paper" or rec.get("outcome") in {"won", "lost"}:
            continue
        if rec.get("stop_loss") is None and rec.get("take_profit") is None:
            continue
        tenant_id = rec.get("tenant_id")
        try:
            res = await check_order_outcome(store, tenant_id, rec["id"])
        except Exception as exc:  # noqa: BLE001 — un ordre ne doit pas bloquer les autres
            logger.warning("Monitor position %s échoué (%s)", rec.get("id"), exc)
            continue
        if res.get("outcome") in {"won", "lost"}:
            closed += 1
            await _notify_close(store, tenant_id, res)
    return closed


async def _notify_close(store: AppStore, tenant_id: str, order: dict) -> None:
    """Diffuse la clôture auto sur le bus temps réel + push best-effort."""
    from app.realtime import bus

    verdict = "GAGNÉ ✅" if order["outcome"] == "won" else "PERDU 🔴"
    msg = f"Position {order['symbol']} clôturée auto : {verdict} (P&L {order.get('realized_pnl')})"
    try:
        await bus.publish(tenant_id, {"type": "order_closed", "data": order})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Diffusion clôture échouée (%s)", exc)
    try:
        from app.alerts import notifier

        user = next((u for u in store.users.list_by_tenant(tenant_id)), None)
        if user and getattr(user, "push_token", None):
            await notifier.send_push(user.push_token, msg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Notification clôture échouée (%s)", exc)
