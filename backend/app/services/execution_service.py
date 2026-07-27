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

# Issues TERMINALES d'une position : elle ne sera plus suivie ni rouverte. `invalid` désigne un
# ordre clôturé sur un résultat que le marché n'a jamais produit (cf. quarantine_impossible_closures) :
# il est neutralisé, ni gagnant ni perdant, et exclu de toutes les statistiques.
_FINAL_OUTCOMES = {"won", "lost", "invalid"}


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
        if o.get("mode") == "paper" and o.get("outcome") not in _FINAL_OUTCOMES
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
        # Risque d'ORIGINE, figé : c'est lui qui définit ce que vaut « 1 R » pour cette position.
        # Sans ça, la sécurisation du profit (stop remonté sur +2R) recalculerait le R sur un stop
        # déjà déplacé et le niveau dériverait à chaque passage.
        info["initial_risk"] = risk_per_unit
        info["original_stop_loss"] = stop_loss
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
    if rec.get("outcome") in _FINAL_OUTCOMES:
        return rec  # déjà clôturé

    entry = rec.get("entry") if rec.get("entry") is not None else rec.get("filled_price")
    sl, tp = rec.get("stop_loss"), rec.get("take_profit")
    side, qty = rec.get("side"), rec.get("qty") or 0.0
    if entry is None or (sl is None and tp is None):
        return {**rec, "outcome": "open", "note": "Aucun SL/TP : vérification automatique impossible."}

    verdict = await replay.replay_outcome(
        rec["symbol"], side, entry, sl, tp, rec.get("created_at"),
    )
    outcome, exit_price, closed_ts = verdict["outcome"], verdict["exit_price"], verdict["closed_ts"]

    # Verdict INDÉTERMINÉ (pas de données réelles, marché fermé, horodatage illisible) : la position
    # reste ouverte. Clôturer ici reviendrait à inventer un résultat que le marché n'a pas produit.
    if outcome == replay.UNDETERMINED:
        return {**rec, "outcome": "open", "undetermined": True, "note": verdict["reason"]}

    if outcome == replay.OPEN:
        unrealized = (exit_price - entry) * qty if side == "buy" else (entry - exit_price) * qty
        return {**rec, "outcome": "open", "current_price": round(exit_price, 8),
                "unrealized_pnl": round(unrealized, 2), "note": verdict["reason"]}

    # Garde-fou de cohérence temporelle : une clôture ne peut pas précéder l'ouverture.
    opened_ts = replay.iso_to_unix(rec.get("created_at"))
    if closed_ts is not None and opened_ts is not None and closed_ts < opened_ts:
        logger.warning(
            "Clôture incohérente ignorée pour %s : %s < ouverture %s",
            rec.get("symbol"), closed_ts, opened_ts,
        )
        return {**rec, "outcome": "open", "undetermined": True,
                "note": "horodatage de clôture antérieur à l'ouverture — résultat rejeté"}

    realized = (exit_price - entry) * qty if side == "buy" else (entry - exit_price) * qty
    updated = {
        **rec, "outcome": outcome, "status": "closed", "exit_price": exit_price,
        "realized_pnl": round(realized, 2), "close_reason": verdict["reason"],
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
    if rec.get("outcome") in _FINAL_OUTCOMES:
        return rec  # déjà clôturé

    entry = rec.get("entry") if rec.get("entry") is not None else rec.get("filled_price")
    qty = rec.get("qty") or 0.0
    side = rec.get("side")
    from app.core.config import get_settings

    candles = await markets.load_candles(rec["symbol"], interval="15m", limit=5)
    # Sans prix de marché RÉEL, on ne clôture pas : retomber sur le prix d'entrée produirait un P&L
    # nul étiqueté « gagnant », c'est-à-dire un résultat inventé. On s'appuie sur le MÊME réglage
    # que l'ouverture (`block_synthetic_orders`) : ce qui interdit d'ouvrir sur des données
    # factices doit interdire d'en tirer un résultat.
    real_price = bool(candles) and markets.is_real(rec["symbol"])
    if not real_price and get_settings().block_synthetic_orders:
        raise ExecutionError(
            f"Clôture impossible : aucun prix de marché réel pour {rec['symbol']}. "
            "La position reste ouverte plutôt que d'être fermée sur un prix supposé."
        )
    if not candles:
        raise ExecutionError(f"Aucune bougie disponible pour {rec['symbol']} — clôture impossible.")
    price = candles[-1].close
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
        and o.get("outcome") not in _FINAL_OUTCOMES
        for o in store.records.list(ORDER, tenant_id)
    )


def _currencies(symbol: str) -> set[str]:
    """Les deux devises d'une paire (« EUR/USD » -> {EUR, USD}) ; vide hors forex/métaux."""
    if "/" not in (symbol or ""):
        return set()
    base, _, quote = symbol.partition("/")
    return {base.strip().upper(), quote.strip().upper()} - {""}


def _correlation_conflict(store: AppStore, tenant_id: str, symbol: str) -> str | None:
    """GARDE DE CORRÉLATION (plan, Phase 3.2) : max N positions partageant une même devise.

    EUR/USD + EUR/JPY + EUR/GBP n'est pas trois paris : c'est trois fois le pari « euro fort » —
    un seul retournement de l'euro toucherait les trois stops en même temps. Retourne le motif du
    refus, ou None si l'ouverture est acceptable.
    """
    from app.core.config import get_settings

    s = get_settings()
    if not s.correlation_guard_enabled:
        return None
    wanted = _currencies(symbol)
    if not wanted:
        return None
    exposure: dict[str, int] = {}
    for o in store.records.list(ORDER, tenant_id):
        if o.get("mode") != "paper" or o.get("outcome") in _FINAL_OUTCOMES:
            continue
        for cur in _currencies(o.get("symbol", "")) & wanted:
            exposure[cur] = exposure.get(cur, 0) + 1
    for cur, count in exposure.items():
        if count >= s.max_positions_per_currency:
            return (
                f"déjà {count} position(s) ouverte(s) exposée(s) à {cur} "
                f"(plafond {s.max_positions_per_currency}) — ouvrir {symbol} doublerait le même pari"
            )
    return None


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
    from app.core.config import get_settings
    from app.data import sessions as sessions_mod
    from app.services import playbook_service, risk_service, verdict_service

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
    base_risk_pct = _RISK_PCT.get(getattr(user, "risk_profile", "moderate"), 1.0)
    s = get_settings()

    # GEL DES ENTRÉES (plan, Phase 3.3) : journée à −3 % ou semaine à −6 % -> plus aucune nouvelle
    # entrée playbook, automatique comme manuelle. La discipline ne se négocie pas un jour de perte.
    frozen, freeze_reason = risk_service.entries_frozen(store, tenant_id)
    if frozen:
        return {
            "mode": "paper", "requested": count, "opened": [], "frozen": True,
            "skipped": [{"symbol": p.get("symbol"), "reason": freeze_reason} for p in ready],
            "armed_waiting": [], "session": session, "note": note,
            "summary": f"🧊 {freeze_reason}",
        }

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
        conflict = _correlation_conflict(store, tenant_id, symbol)
        if conflict:
            skipped.append({"symbol": symbol, "reason": conflict})
            continue
        # SIZING PAR CONVICTION (plan, Phase 3.1) : le risque de base (profil) est modulé par le
        # verdict MESURÉ de la paire (×1,25 🟢 solide, ×0,5 🟡/🔴), plafonné en absolu.
        conviction_mult, verdict_status = verdict_service.sizing_multiplier(store, symbol)
        risk_pct = min(base_risk_pct * conviction_mult, s.conviction_risk_cap_pct) \
            if s.conviction_sizing_enabled else base_risk_pct
        risk_amount = capital * risk_pct / 100
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
        # JOURNAL ENRICHI (plan, Phase 3.4) : chaque trade emporte le contexte qui a motivé sa
        # taille et son entrée — verdict de paire, déclencheur, fenêtre de session, volatilité.
        # C'est la matière première du futur méta-filtre : sans ces champs, impossible de savoir
        # a posteriori QUELLES conditions produisent les gagnants.
        daily_metrics = ((p.get("layers") or {}).get("daily") or {}).get("metrics") or {}
        sess_ctx = sessions_mod.session_context()
        order = store.records.put(ORDER, order["id"], {
            **order,
            "pair_verdict": verdict_status,
            "conviction_mult": conviction_mult,
            "risk_pct": risk_pct,
            "trigger": p.get("trigger"),
            "trigger_type": (p.get("trigger") or "").split(" — ", 1)[0].strip() or None,
            "session_window": (sess_ctx.get("kill_zones") or ["hors_fenetre"])[0],
            "atr_pct": daily_metrics.get("atr_pct"),
        }, tenant_id=tenant_id)
        opened.append({
            "order_id": order["id"], "symbol": symbol, "side": side, "qty": qty,
            "entry": order.get("filled_price"), "stop_loss": sl, "take_profit": tp,
            "risk_reward": p.get("risk_reward"), "target_pips": p.get("reward_pips"),
            "stop_pips": p.get("risk_pips"), "pips_label": p.get("pips_label"),
            "risk_amount": order.get("risk_amount"), "potential_profit": order.get("potential_profit"),
            "trigger": p.get("trigger"), "horizon_days": p.get("horizon_days"),
            "pair_verdict": verdict_status, "conviction_mult": conviction_mult,
            "risk_pct": risk_pct,
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


async def execute_playbook_symbol(store: AppStore, tenant_id: str, symbol: str) -> dict:
    """Ouvre en compte DÉMO le trade d'UN seul symbole (bouton « ouvrir ce trade »).

    La stratégie est RECALCULÉE ici : on n'accepte jamais des niveaux envoyés par le navigateur,
    qui pourraient être périmés ou manipulés. Si le déclencheur 15 min n'est plus actif, rien n'est
    ouvert et le refus est expliqué.
    """
    from app.data import markets
    from app.services import playbook_service

    setup = await playbook_service.build_setup(symbol)
    pick = setup.as_dict()
    pick["asset_class"] = markets.asset_class(symbol)
    pick["tier"] = "ready" if setup.ready else "armed"
    report = await execute_playbook_trades(store, tenant_id, count=1, picks=[pick])
    if not setup.ready:
        report["summary"] = (
            f"{symbol} n'est pas exécutable maintenant : "
            + ((setup.reasons or ["déclencheur 15 min non formé"])[0])
        )
    report["symbol"] = symbol
    report["setup"] = pick
    return report


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
    wins = losses = invalid = 0

    for rec in orders:
        row = dict(rec)
        entry = rec.get("entry") if rec.get("entry") is not None else rec.get("filled_price")
        sl, tp = rec.get("stop_loss"), rec.get("take_profit")
        side, qty = rec.get("side"), rec.get("qty") or 0.0
        closed = rec.get("outcome") in _FINAL_OUTCOMES
        row["closed"] = closed
        if closed:
            # Une position INVALIDÉE (clôturée sur un résultat que le marché n'a pas produit) ne
            # compte ni en gain, ni en perte, ni au P&L : elle n'a jamais eu lieu.
            if rec.get("outcome") == "invalid":
                invalid += 1
                out.append(row)
                continue
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
        # Positions neutralisées parce que leur clôture était impossible (cf. quarantaine).
        "invalid": invalid,
        "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) else None,
        "as_of": datetime.now(UTC).isoformat(),
    }


async def secure_open_profits(store: AppStore) -> int:
    """SÉCURISATION DU PROFIT — remonte le stop sur +2R dès que le trade a parcouru +2R.

    La règle demandée : une position qui a déjà gagné deux fois son risque ne doit plus pouvoir
    redevenir perdante. On déplace donc le stop EXACTEMENT sur le niveau +2R (et non à l'entrée) :
    le gain de 2R est verrouillé, et la position continue de courir vers son objectif final (R/R
    maximum). Le stop ne recule jamais — on ne le redescend pas si le prix revient en arrière.

    Retourne le nombre de positions dont le stop a été relevé sur ce passage.
    """
    from app.core.config import get_settings
    from app.domain.playbook import secured_stop

    s = get_settings()
    if not s.playbook_secure_profit_enabled:
        return 0

    secured = 0
    prices: dict[str, float | None] = {}
    for rec in store.records.list(ORDER):  # tous tenants
        if rec.get("mode") != "paper" or rec.get("outcome") in _FINAL_OUTCOMES:
            continue
        entry = rec.get("entry") if rec.get("entry") is not None else rec.get("filled_price")
        sl, side = rec.get("stop_loss"), rec.get("side")
        if entry is None or sl is None or rec.get("profit_secured"):
            continue
        # Le risque de RÉFÉRENCE est celui d'origine : après sécurisation, la distance entrée-stop
        # ne vaut plus 1R et recalculer dessus ferait dériver le niveau à chaque passage.
        initial_risk = rec.get("initial_risk") or abs(entry - sl)
        if initial_risk <= 0:
            continue
        symbol = rec.get("symbol", "")
        if symbol not in prices:
            prices[symbol] = await _reference_price(symbol)
        price = prices[symbol]
        if not price:
            continue
        buy = side == "buy"
        progress_r = ((price - entry) if buy else (entry - price)) / initial_risk
        if progress_r < s.playbook_secure_at_r:
            continue
        new_stop = secured_stop(entry, entry - initial_risk if buy else entry + initial_risk,
                                "buy" if buy else "sell", at_r=s.playbook_secure_stop_at_r)
        # Garde-fou : le stop ne recule jamais, et il doit rester du bon côté du prix courant.
        if (buy and new_stop <= sl) or (not buy and new_stop >= sl):
            continue
        if (buy and new_stop >= price) or (not buy and new_stop <= price):
            continue
        store.records.put(ORDER, rec["id"], {
            **rec, "stop_loss": round(new_stop, 8), "initial_risk": initial_risk,
            "profit_secured": True, "secured_at_r": s.playbook_secure_stop_at_r,
            "original_stop_loss": rec.get("original_stop_loss", sl),
            "secured_at": datetime.now(UTC).isoformat(),
        }, tenant_id=rec.get("tenant_id"))
        secured += 1
        await _notify_secured(store, rec.get("tenant_id"), rec, new_stop, progress_r)
    return secured


async def _notify_secured(store: AppStore, tenant_id: str, order: dict, new_stop: float, progress_r: float) -> None:
    """Prévient que le profit est verrouillé : c'est un événement que le trader veut connaître."""
    from app.realtime import bus

    msg = (
        f"🔒 {order['symbol']} : +{progress_r:.1f}R atteint — stop remonté sur "
        f"{new_stop:.6g}. La position ne peut plus être perdante, elle court vers son objectif."
    )
    try:
        await bus.publish(tenant_id, {"type": "profit_secured",
                                      "data": {**order, "stop_loss": new_stop, "message": msg}})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Diffusion sécurisation échouée (%s)", exc)
    try:
        from app.alerts import notifier

        user = next((u for u in store.users.list_by_tenant(tenant_id)), None)
        if user and getattr(user, "push_token", None):
            await notifier.send_push(user.push_token, msg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Notification sécurisation échouée (%s)", exc)


def quarantine_impossible_closures(store: AppStore, tenant_id: str | None = None) -> dict:
    """Met en quarantaine les positions clôturées sur un résultat que le marché n'a pas produit.

    L'ancien rejeu pouvait clôturer un trade sur des bougies SYNTHÉTIQUES (horodatées jusqu'à
    « maintenant ») ou sur une bougie antérieure à l'entrée. Ces ordres portent un P&L inventé ;
    les laisser dans l'historique fausserait le taux de réussite, le portefeuille et l'apprentissage
    des agents.

    On ne les supprime pas — on les marque `invalid` avec le motif, et on remet leur P&L à zéro :
    ce qui n'a jamais eu lieu ne doit ni compter comme gain, ni comme perte.
    """
    from app.data import replay

    scanned = 0
    quarantined: list[dict] = []
    for rec in store.records.list(ORDER, tenant_id) if tenant_id else store.records.list(ORDER):
        scanned += 1
        reason = replay.is_impossible_closure(rec)
        if not reason:
            continue
        store.records.put(ORDER, rec["id"], {
            **rec, "outcome": "invalid", "status": "invalidated",
            "invalid_reason": reason,
            "realized_pnl_before_invalidation": rec.get("realized_pnl"),
            "realized_pnl": 0.0,
            "invalidated_at": datetime.now(UTC).isoformat(),
        }, tenant_id=rec.get("tenant_id"))
        quarantined.append({"id": rec["id"], "symbol": rec.get("symbol"), "reason": reason})
    if quarantined:
        logger.warning(
            "Quarantaine : %d position(s) clôturée(s) sur un résultat impossible ont été invalidées",
            len(quarantined),
        )
    return {"scanned": scanned, "quarantined": quarantined, "count": len(quarantined)}


async def monitor_positions(store: AppStore) -> int:
    """Parcourt tous les ordres papier OUVERTS avec SL/TP et clôture ceux dont le niveau est atteint.

    Diffuse un événement temps réel + notifie l'utilisateur à chaque clôture automatique.
    Retourne le nombre d'ordres clôturés sur ce passage."""
    closed = 0
    for rec in store.records.list(ORDER):  # tous tenants
        if rec.get("mode") != "paper" or rec.get("outcome") in _FINAL_OUTCOMES:
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
