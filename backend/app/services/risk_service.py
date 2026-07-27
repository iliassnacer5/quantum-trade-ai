"""Application des règles de protection du capital (Lot 2).

Calcule l'état de risque d'un utilisateur (exposition, signaux du jour) et applique des garde-fous
au moment de la génération : exposition maximale, nombre de signaux quotidien, alerte drawdown.
Déterministe — aucun LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.models.entities import User
from app.repositories.store import AppStore


def _is_today(iso_or_dt) -> bool:
    if iso_or_dt is None:
        return False
    if isinstance(iso_or_dt, str):
        try:
            dt = datetime.fromisoformat(iso_or_dt)
        except ValueError:
            return False
    else:
        dt = iso_or_dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).date() == datetime.now(UTC).date()


@dataclass
class RiskStatus:
    capital: float
    exposure_value: float
    exposure_pct: float
    max_exposure_pct: float
    daily_signals: int
    max_daily_signals: int
    breaches: list[str]

    def as_dict(self) -> dict:
        return {
            "capital": round(self.capital, 2),
            "exposure_value": round(self.exposure_value, 2),
            "exposure_pct": round(self.exposure_pct, 2),
            "max_exposure_pct": self.max_exposure_pct,
            "daily_signals": self.daily_signals,
            "max_daily_signals": self.max_daily_signals,
            "breaches": self.breaches,
            "ok": not self.breaches,
        }


def compute_status(user: User, store: AppStore) -> RiskStatus:
    signals = store.signals.list_for_tenant(user.tenant_id, limit=500)
    today = [s for s in signals if _is_today(s.payload.get("created_at") or s.created_at)]
    exposure_value = sum(
        float(s.payload.get("position_value") or 0)
        for s in today
        if s.payload.get("direction") in ("BUY", "SELL")
    )
    exposure_pct = (exposure_value / user.capital * 100) if user.capital > 0 else 0.0
    daily_signals = len(today)

    breaches: list[str] = []
    if exposure_pct > user.max_exposure_pct:
        breaches.append(
            f"Exposition {exposure_pct:.0f}% > plafond {user.max_exposure_pct:.0f}%"
        )
    if daily_signals >= user.max_daily_signals:
        breaches.append(f"Limite de {user.max_daily_signals} signaux/jour atteinte")

    return RiskStatus(
        capital=user.capital,
        exposure_value=exposure_value,
        exposure_pct=exposure_pct,
        max_exposure_pct=user.max_exposure_pct,
        daily_signals=daily_signals,
        max_daily_signals=user.max_daily_signals,
        breaches=breaches,
    )


def check_can_generate(user: User, store: AppStore) -> tuple[bool, str | None]:
    """Autorise ou non une nouvelle génération. (ok, raison_si_bloque).

    Générer un signal = produire une ANALYSE, pas ouvrir une position : on ne bloque donc PAS sur
    l'exposition (qui devient un simple avertissement sur la carte). On conserve uniquement une
    limite de débit quotidienne anti-abus (coûts API/LLM).
    """
    status = compute_status(user, store)
    if status.daily_signals >= user.max_daily_signals:
        return False, f"Limite quotidienne de {user.max_daily_signals} analyses atteinte."
    return True, None


def real_exposure_pct(user: User, store: AppStore) -> float:
    """Exposition issue des ordres RÉELLEMENT exécutés (papier/réel), pas des analyses générées.

    Utilisée par l'Agent Risque pour ne pénaliser la confiance qu'en présence de vraies positions :
    générer une analyse ne doit pas dégrader la qualité des signaux suivants.
    """
    try:
        orders = store.records.list("order", user.tenant_id)
    except Exception:  # noqa: BLE001 — pas de store records (mode dégradé)
        return 0.0
    notional = sum(float(o.get("filled_price") or 0) * float(o.get("qty") or 0) for o in orders)
    return (notional / user.capital * 100) if user.capital > 0 else 0.0


def generation_warning(user: User, store: AppStore) -> str | None:
    """Avertissement de risque (non bloquant) à afficher sur la carte signal."""
    status = compute_status(user, store)
    if status.exposure_pct >= user.max_exposure_pct:
        return (
            f"⚠️ Exposition simulée {status.exposure_pct:.0f}% ≥ plafond {user.max_exposure_pct:.0f}%. "
            f"Prudence avant d'ouvrir une nouvelle position."
        )
    return None


# =======================================================================================
# GEL DES ENTRÉES SUR PERTE (plan, Phase 3.3) — stop quotidien −3 %, hebdomadaire −6 %
# =======================================================================================
# Le pire ennemi d'une stratégie mesurée est la journée où l'on « se refait » : le gel coupe
# mécaniquement toute NOUVELLE entrée playbook dès que la perte réalisée du jour (ou de la
# semaine) franchit le seuil. Les positions déjà ouvertes restent gérées (sécurisation, SL/TP) :
# on arrête d'empiler du risque, pas de gérer celui qui existe.

FREEZE_EVENTS = "risk_freeze"


def _paper_realized_pnl_since(store: AppStore, tenant_id: str, since: datetime) -> float:
    """P&L RÉALISÉ des positions papier clôturées depuis `since` (les `invalid` ne comptent pas)."""
    total = 0.0
    try:
        orders = store.records.list("order", tenant_id)
    except Exception:  # noqa: BLE001
        return 0.0
    for o in orders:
        if o.get("mode") != "paper" or o.get("outcome") not in ("won", "lost"):
            continue
        closed_at = o.get("closed_at")
        if not closed_at:
            continue
        try:
            dt = datetime.fromisoformat(closed_at)
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        if dt >= since:
            total += float(o.get("realized_pnl") or 0.0)
    return total


def entries_frozen(store: AppStore, tenant_id: str) -> tuple[bool, str | None]:
    """Les nouvelles entrées playbook sont-elles gelées pour ce tenant ? (gelé, motif).

    Seuils sur le P&L RÉALISÉ (déterministe et rejouable) : perte du jour ≥ `daily_loss_freeze_pct`
    du capital, ou perte depuis lundi ≥ `weekly_loss_freeze_pct`. Le gel expire tout seul au
    changement de période — aucune intervention à faire, aucune exception possible.
    """
    from datetime import timedelta

    from app.core.config import get_settings

    s = get_settings()
    if not s.loss_freeze_enabled:
        return False, None
    users = store.users.list_by_tenant(tenant_id)
    capital = users[0].capital if users else 0.0
    if capital <= 0:
        return False, None

    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=now.weekday())

    daily_pnl = _paper_realized_pnl_since(store, tenant_id, day_start)
    daily_limit = -capital * s.daily_loss_freeze_pct / 100
    if daily_pnl <= daily_limit:
        return True, (
            f"GEL QUOTIDIEN : perte réalisée du jour {daily_pnl:+.2f} "
            f"({daily_pnl / capital * 100:+.1f} % du capital) ≤ seuil −{s.daily_loss_freeze_pct:g} %. "
            f"Aucune nouvelle entrée avant demain — les positions ouvertes restent gérées."
        )
    weekly_pnl = _paper_realized_pnl_since(store, tenant_id, week_start)
    weekly_limit = -capital * s.weekly_loss_freeze_pct / 100
    if weekly_pnl <= weekly_limit:
        return True, (
            f"GEL HEBDOMADAIRE : perte réalisée de la semaine {weekly_pnl:+.2f} "
            f"({weekly_pnl / capital * 100:+.1f} % du capital) ≤ seuil −{s.weekly_loss_freeze_pct:g} %. "
            f"Aucune nouvelle entrée avant lundi — les positions ouvertes restent gérées."
        )
    return False, None


async def notify_freezes(store: AppStore) -> int:
    """Détecte les gels et prévient chaque tenant concerné UNE fois par période (pas de spam).

    Appelé par la boucle de surveillance des positions : c'est elle qui voit passer les clôtures,
    donc c'est elle qui peut constater qu'un seuil vient d'être franchi.
    """
    import logging

    logger = logging.getLogger(__name__)
    notified = 0
    try:
        tenants = {u.tenant_id for u in store.users.list_all()}
    except Exception:  # noqa: BLE001
        return 0
    today = datetime.now(UTC).date().isoformat()
    for tenant_id in tenants:
        frozen, reason = entries_frozen(store, tenant_id)
        if not frozen:
            continue
        key = f"{tenant_id}:{today}"
        if store.records.get(FREEZE_EVENTS, key):
            continue  # déjà annoncé aujourd'hui
        store.records.put(FREEZE_EVENTS, key, {"reason": reason, "date": today}, tenant_id=tenant_id)
        notified += 1
        try:
            from app.realtime import bus

            await bus.publish(tenant_id, {"type": "risk_freeze", "data": {"reason": reason}})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Diffusion du gel échouée (%s)", exc)
        try:
            from app.alerts import notifier

            user = next((u for u in store.users.list_by_tenant(tenant_id)), None)
            if user and getattr(user, "push_token", None):
                await notifier.send_push(user.push_token, f"🧊 {reason}")
            if user and getattr(user, "alert_telegram", False) and getattr(user, "telegram_chat_id", None):
                await notifier.send_telegram(user.telegram_chat_id, f"🧊 {reason}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Notification du gel échouée (%s)", exc)
    return notified
