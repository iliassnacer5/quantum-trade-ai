"""VERDICT PAR PAIRE — la note 🟢/🟡/🔴 que le backtest hebdomadaire donne à chaque paire.

C'est la pièce qui relie la MESURE (le backtest) à l'ACTION (l'auto-entrée et le sizing) :

- 🟢 (green)  : espérance ≥ +0,4 R avec n ≥ 20 trades, constatée sur DEUX passages hebdomadaires
  consécutifs. C'est le seul statut auto-tradé : une seule bonne semaine peut être un accident,
  deux mesures indépendantes qui disent la même chose commencent à ressembler à un edge.
- 🟡 (yellow) : tout ce qui n'est ni prouvé ni condamné — espérance positive mais fragile,
  échantillon trop court, premier passage vert. Analysé et affiché, jamais auto-tradé.
- 🔴 (red)    : espérance ≤ 0 avec un échantillon suffisant pour y croire (n ≥ 8). La stratégie
  PERD sur cette paire : exclue de l'auto-trade.

Le service tient aussi la MATRICE paire × déclencheur : un déclencheur mesuré sous +0,4 R sur
n ≥ 15 trades pour UNE paire y est désactivé comme déclencheur d'auto-entrée (il reste calculé et
affiché — l'information ne coûte rien, l'ordre si).

Chaque refus d'auto-entrée motivé par un verdict est journalisé (« trades évités ») : le rituel
hebdomadaire du forward test doit pouvoir répondre à « les gates ont-ils eu raison ? ».
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.core.config import get_settings

logger = logging.getLogger(__name__)

COLLECTION = "playbook_pair_verdicts"
LATEST = "latest"
REFUSALS = "gate_refusal"

_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


# =======================================================================================
# Calcul du verdict à partir d'une passe de backtest
# =======================================================================================
def _pair_status(m: dict, prev_streak: int) -> tuple[str, int, str]:
    """(statut, nouvelle série verte, raison) d'une paire à partir de ses métriques mesurées."""
    s = get_settings()
    exp = float(m.get("expectancy_r") or 0.0)
    n = int(m.get("trades") or 0)
    meets_green = exp >= s.playbook_verdict_min_expectancy and n >= s.playbook_verdict_min_trades
    streak = prev_streak + 1 if meets_green else 0

    if meets_green and streak >= s.playbook_verdict_green_streak:
        return ("green", streak,
                f"espérance {exp:+.2f} R sur {n} trades, critère tenu sur {streak} passages consécutifs")
    if n >= s.playbook_verdict_red_min_trades and exp <= 0:
        return ("red", streak,
                f"espérance {exp:+.2f} R sur {n} trades — la stratégie perd ici")
    if meets_green:
        return ("yellow", streak,
                f"espérance {exp:+.2f} R sur {n} trades — 1er passage au-dessus du seuil, "
                f"confirmation attendue au prochain backtest")
    if n < s.playbook_verdict_min_trades:
        return ("yellow", streak,
                f"échantillon insuffisant ({n} trades < {s.playbook_verdict_min_trades}) — on ne sait pas")
    return ("yellow", streak,
            f"espérance {exp:+.2f} R sur {n} trades — positive mais sous le seuil de "
            f"{s.playbook_verdict_min_expectancy:+.1f} R")


def _disabled_triggers(by_pair_trigger: dict) -> dict[str, list[str]]:
    """Cellules (paire, déclencheur) mesurées sous le seuil avec assez de trades -> désactivées."""
    s = get_settings()
    out: dict[str, list[str]] = {}
    for key, m in (by_pair_trigger or {}).items():
        if "|" not in key:
            continue
        symbol, trigger = key.split("|", 1)
        if (int(m.get("trades") or 0) >= s.playbook_trigger_matrix_min_trades
                and float(m.get("expectancy_r") or 0.0) < s.playbook_trigger_matrix_min_expectancy):
            out.setdefault(symbol, []).append(trigger)
    return out


def update_from_backtest(store, payload: dict) -> dict | None:  # noqa: ANN001
    """Met à jour le record des verdicts après UN passage de backtest (la passe portée, 1 h).

    La série verte (`green_streak`) se lit d'un passage à l'autre : c'est elle qui impose les deux
    passages consécutifs avant d'autoriser l'auto-trade. Une paire absente du passage (données en
    échec) ne conserve JAMAIS son vert : on ne trade pas sur une mesure qu'on n'a pas pu refaire.
    """
    if store is None:
        return None
    s = get_settings()
    scope = payload.get("scope") or {}
    by_symbol = scope.get("by_symbol") or {}
    date = payload.get("date") or datetime.now(UTC).date().isoformat()
    prev = store.records.get(COLLECTION, LATEST) or {}
    prev_pairs = prev.get("pairs") or {}
    if prev.get("date") == date:
        # Deux backtests le même jour (relance manuelle) ne comptent qu'UN passage : sinon il
        # suffirait de relancer deux fois dans la journée pour fabriquer une « série » verte.
        prev_pairs = {k: {**v, "green_streak": v.get("prev_green_streak", 0)}
                      for k, v in prev_pairs.items()}

    pairs: dict[str, dict] = {}
    for symbol, m in by_symbol.items():
        prev_row = prev_pairs.get(symbol) or {}
        prev_streak = int(prev_row.get("green_streak") or 0)
        status, streak, reason = _pair_status(m, prev_streak)
        pairs[symbol] = {
            "symbol": symbol,
            "status": status,
            "emoji": _EMOJI[status],
            "expectancy_r": m.get("expectancy_r"),
            "trades": m.get("trades"),
            "win_rate": m.get("win_rate"),
            "profit_factor": m.get("profit_factor"),
            "green_streak": streak,
            "prev_green_streak": prev_streak,
            "reason": reason,
            "measured_at": date,
        }

    # Paires connues avant mais absentes de ce passage : on garde la ligne (l'information reste
    # utile) mais un vert est dégradé en jaune — pas d'auto-trade sur une mesure périmée.
    for symbol, row in prev_pairs.items():
        if symbol in pairs:
            continue
        degraded = dict(row)
        if row.get("status") == "green":
            degraded.update(status="yellow", emoji=_EMOJI["yellow"], green_streak=0)
        degraded["reason"] = (f"paire absente du backtest du {date} (données indisponibles) — "
                              f"dernière mesure : {row.get('measured_at')}")
        pairs[symbol] = degraded

    record = {
        "date": date,
        "updated_at": datetime.now(UTC).isoformat(),
        "criteria": {
            "green_min_expectancy_r": s.playbook_verdict_min_expectancy,
            "green_min_trades": s.playbook_verdict_min_trades,
            "green_streak_required": s.playbook_verdict_green_streak,
            "red_min_trades": s.playbook_verdict_red_min_trades,
            "trigger_min_trades": s.playbook_trigger_matrix_min_trades,
            "trigger_min_expectancy_r": s.playbook_trigger_matrix_min_expectancy,
        },
        "pairs": pairs,
        "disabled_triggers": _disabled_triggers(scope.get("by_pair_trigger") or {}),
        "gating_enabled": s.playbook_pair_gating,
    }
    try:
        store.records.put(COLLECTION, date, record)
        store.records.put(COLLECTION, LATEST, record)
    except Exception as exc:  # noqa: BLE001 — la persistance ne doit pas casser le backtest
        logger.warning("Verdicts par paire non persistés (%s)", exc)
    logger.info(
        "Verdicts par paire (%s) : %d 🟢 · %d 🟡 · %d 🔴",
        date,
        sum(1 for p in pairs.values() if p["status"] == "green"),
        sum(1 for p in pairs.values() if p["status"] == "yellow"),
        sum(1 for p in pairs.values() if p["status"] == "red"),
    )
    return record


def bootstrap_from_history(store) -> dict | None:  # noqa: ANN001
    """Reconstruit les verdicts depuis les backtests DÉJÀ persistés (ordre chronologique).

    Utile au premier déploiement : les passages hebdomadaires passés comptent pour la série verte,
    on ne repart pas de zéro alors que la mesure existe.
    """
    try:
        recs = store.records.list("playbook_backtest")
    except Exception:  # noqa: BLE001
        return None
    dated = sorted(
        (r for r in recs if r.get("id") != LATEST and r.get("date") and r.get("scope")),
        key=lambda r: r["date"],
    )
    # Dédoublonne par date (le record `latest` duplique le dernier passage daté).
    seen: set[str] = set()
    out = None
    for rec in dated:
        if rec["date"] in seen:
            continue
        seen.add(rec["date"])
        out = update_from_backtest(store, rec)
    return out


# =======================================================================================
# Lecture
# =======================================================================================
def report(store) -> dict:  # noqa: ANN001
    """Le record complet des verdicts, avec amorçage depuis l'historique si nécessaire."""
    rec = store.records.get(COLLECTION, LATEST)
    if rec is None:
        rec = bootstrap_from_history(store)
    if rec is None:
        return {"available": False, "pairs": {}, "disabled_triggers": {},
                "note": ("Aucun backtest hebdomadaire encore exécuté : aucune paire n'est notée. "
                         "Sans verdict, l'auto-entrée reste fermée (aucune paire 🟢).")}
    return {"available": True, **rec}


def verdict_for(store, symbol: str) -> dict | None:  # noqa: ANN001
    """La ligne de verdict d'une paire (None si jamais mesurée)."""
    rec = store.records.get(COLLECTION, LATEST) or bootstrap_from_history(store)
    if not rec:
        return None
    return (rec.get("pairs") or {}).get(symbol.upper())


def brief_for(store, symbol: str) -> dict:  # noqa: ANN001
    """Vue courte du verdict, prête à accrocher sur un pick / une carte signal."""
    row = verdict_for(store, symbol)
    if not row:
        return {"status": "unrated", "emoji": "⚪", "reason": "paire jamais notée par le backtest hebdomadaire"}
    return {k: row.get(k) for k in
            ("status", "emoji", "expectancy_r", "trades", "win_rate", "green_streak", "reason")}


def trigger_disabled(store, symbol: str, trigger_type: str | None) -> str | None:  # noqa: ANN001
    """Motif de refus si ce déclencheur est désactivé pour cette paire (None = autorisé)."""
    if not trigger_type or not get_settings().playbook_trigger_matrix_gating:
        return None
    rec = store.records.get(COLLECTION, LATEST)
    disabled = (rec or {}).get("disabled_triggers") or {}
    if trigger_type in (disabled.get(symbol.upper()) or []):
        return (f"déclencheur « {trigger_type} » désactivé sur {symbol} : mesuré sous "
                f"+{get_settings().playbook_trigger_matrix_min_expectancy:g} R par le backtest")
    return None


# =======================================================================================
# Gating de l'auto-entrée + journal des trades évités
# =======================================================================================
def filter_auto_ready(store, ready: list[dict]) -> tuple[list[dict], list[dict]]:  # noqa: ANN001
    """Sépare les setups auto-tradables (paire 🟢, déclencheur autorisé) des refusés motivés.

    Chaque refus est persisté dans `gate_refusal` avec les niveaux du trade : le rituel hebdo
    pourra rejouer ces trades évités et dire si les gates ont protégé ou coûté.
    """
    if not get_settings().playbook_pair_gating:
        return ready, []
    allowed: list[dict] = []
    refused: list[dict] = []
    for pick in ready:
        symbol = (pick.get("symbol") or "").upper()
        brief = brief_for(store, symbol)
        trigger_type = (pick.get("trigger") or "").split(" — ", 1)[0].strip() or None
        why: str | None = None
        if brief["status"] != "green":
            label = {"red": "paire 🔴 (la stratégie perd ici)",
                     "yellow": "paire 🟡 (edge non confirmé)",
                     "unrated": "paire jamais notée"}.get(brief["status"], brief["status"])
            why = f"{label} — {brief.get('reason')}"
        else:
            why = trigger_disabled(store, symbol, trigger_type)
        if why is None:
            pick["pair_verdict"] = brief
            allowed.append(pick)
        else:
            refused.append({"symbol": symbol, "reason": why, "verdict": brief})
            _record_refusal(store, pick, why)
    return allowed, refused


def _record_refusal(store, pick: dict, reason: str) -> None:  # noqa: ANN001
    """Trace un trade ÉVITÉ par les gates (avec ses niveaux : il reste rejouable a posteriori)."""
    now = datetime.now(UTC)
    key = f"{now.date().isoformat()}:{pick.get('symbol')}:{now.strftime('%H%M%S')}"
    try:
        store.records.put(REFUSALS, key, {
            "symbol": pick.get("symbol"), "direction": pick.get("direction"),
            "entry": pick.get("entry"), "stop_loss": pick.get("stop_loss"),
            "take_profit_1": pick.get("take_profit_1"), "trigger": pick.get("trigger"),
            "reason": reason, "at": now.isoformat(),
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("Trade évité non journalisé (%s)", exc)


def recent_refusals(store, limit: int = 50) -> list[dict]:  # noqa: ANN001
    """Les trades refusés par les gates, les plus récents d'abord (rituel hebdomadaire)."""
    try:
        rows = store.records.list(REFUSALS)
    except Exception:  # noqa: BLE001
        return []
    rows.sort(key=lambda r: r.get("at") or "", reverse=True)
    return rows[:limit]


# =======================================================================================
# Sizing par conviction (plan Phase 3.1)
# =======================================================================================
def sizing_multiplier(store, symbol: str) -> tuple[float, str]:  # noqa: ANN001
    """(multiplicateur de risque, statut du verdict) pour dimensionner une position.

    ×1,25 sur une paire 🟢 à échantillon solide (n ≥ 30), ×0,5 sur une 🟡 ou une 🔴 (le chemin
    manuel peut encore les trader — mais à demi-risque), ×1 sur une paire non mesurée.
    """
    s = get_settings()
    if not s.conviction_sizing_enabled:
        return 1.0, "sizing par conviction désactivé"
    row = verdict_for(store, symbol)
    if not row:
        return 1.0, "unrated"
    status = row.get("status")
    if status == "green":
        if int(row.get("trades") or 0) >= s.conviction_green_min_trades:
            return s.conviction_green_mult, "green"
        return 1.0, "green"
    if status in ("yellow", "red"):
        return s.conviction_yellow_mult, status
    return 1.0, str(status)
