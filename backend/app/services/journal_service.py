"""Service du Journal d'apprentissage (Learning Loop) — 100% synchrone.

Enregistre l'issue des signaux et calcule des multiplicateurs de fiabilité par agent, utilisés par
le Master pour la pondération dynamique. Cohérent avec l'architecture sync (store + repositories).
"""

from __future__ import annotations

import logging

from app.agents.journal import compute_weight_multipliers

logger = logging.getLogger(__name__)


def record_signal(store, tenant_id: str, card, signal_id: str | None = None) -> None:
    """Enregistre un signal ACTIONNABLE (BUY/SELL) avec le détail des scores d'agents.

    Les HOLD ne sont PAS enregistrés : sans trade, il n'y a aucune issue à apprendre — les inscrire
    ne ferait que gonfler indéfiniment le compteur « ouverts » (ils ne se résolvent jamais)."""
    direction = card.direction.value if hasattr(card.direction, "value") else str(card.direction)
    if direction == "HOLD":
        return
    try:
        agent_scores = {a["name"]: a["score"] for a in (getattr(card, "agents", None) or [])}
        store.journal.add(
            tenant_id,
            {
                "signal_id": signal_id,
                "symbol": card.asset,
                "direction": direction,
                "outcome": "open",
                "pnl": None,
                "agent_scores": agent_scores,
            },
        )
    except Exception as exc:  # noqa: BLE001 — l'apprentissage ne doit jamais casser le flux
        logger.warning("Enregistrement journal échoué (%s)", exc)


def playbook_entries(store, tenant_id: str, limit: int = 200) -> list[dict]:
    """Positions ouvertes en démo (playbook) reformatées comme des entrées de journal.

    Le Journal n'enregistrait QUE les signaux du flux classique (bouton « Générer un signal » du
    dashboard) — un flux distinct de celui que la plupart des utilisateurs emploient réellement
    (auto-entrée, « Ouvrir en démo », trades du jour). Résultat mesuré : un compte avec plusieurs
    positions ouvertes et clôturées voyait le Journal afficher zéro partout, ce qui ressemble à une
    panne alors que les deux flux n'ont simplement jamais partagé leurs données.

    Cette fonction ne CRÉE aucun enregistrement : elle traduit à la volée les ordres papier
    (`execution_service`) dans la forme attendue par `stats()` et par l'affichage. Les issues
    NEUTRES (`reset`, `invalid` — jamais jouées jusqu'au bout) sont exclues : ce ne sont pas des
    trades gagnés ou perdus, les compter fausserait le taux de réussite.
    """
    from app.domain import pips as pips_mod
    from app.services import execution_service

    out: list[dict] = []
    for o in execution_service.list_orders(store, tenant_id, limit=limit):
        if o.get("mode") != "paper":
            continue
        outcome_raw = o.get("outcome")
        if outcome_raw in execution_service.NEUTRAL_OUTCOMES:
            continue
        outcome = {"won": "win", "lost": "loss"}.get(outcome_raw, "open")
        # Pips réalisés (entrée -> sortie) sur un trade clôturé : la même lecture que sur la carte
        # de Paper Trading, pour que les deux pages ne racontent pas deux histoires différentes.
        pips = pips_mod.signed_pips(
            o.get("symbol", ""), o.get("side"),
            o.get("entry") if o.get("entry") is not None else o.get("filled_price"),
            o.get("exit_price"),
        ) if outcome != "open" else None
        out.append({
            "id": o["id"],
            "source": "playbook",
            "symbol": o.get("symbol"),
            "direction": "BUY" if o.get("side") == "buy" else "SELL",
            "outcome": outcome,
            "pips": pips,
            "pips_label": pips_mod.label(o.get("symbol", "")),
            "pnl": round(float(o["realized_pnl"]), 2) if outcome != "open" and o.get("realized_pnl") is not None else None,
            "agent_scores": {},
            "trigger": o.get("trigger"),
            "entry": o.get("entry") or o.get("filled_price"),
            "stop_loss": o.get("stop_loss"),
            "take_profit": o.get("take_profit"),
            "created_at": o.get("created_at"),
            "closed_at": o.get("closed_at"),
        })
    return out


def all_entries(store, tenant_id: str, limit: int = 200) -> list[dict]:
    """Le Journal COMPLET : signaux classiques + trades playbook, les deux flux réunis.

    C'est cette liste que la page doit afficher — l'utilisateur voit « ses trades », pas « les
    trades du flux qu'il n'a pas utilisé ». Triée du plus récent au plus ancien.
    """
    merged = recent_entries(store, tenant_id, limit) + playbook_entries(store, tenant_id, limit)
    merged.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    return merged[:limit]


def recent_entries(store, tenant_id: str, limit: int = 200) -> list[dict]:
    """Entrées de journal récentes pour `compute_weight_multipliers`."""
    journal_repo = getattr(store, "journal", None)
    if journal_repo is None:
        return []
    try:
        return journal_repo.list_for_tenant(tenant_id, limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lecture journal échouée (%s)", exc)
        return []


def compute_multipliers(store, tenant_id: str, market: str | None = None) -> dict[str, float]:
    """Multiplicateurs de poids par agent, dérivés de l'historique (taux de réussite).

    `market` (crypto|forex|stock) : apprentissage SÉPARÉ par marché — un agent mauvais en forex ne
    pénalise plus son score crypto. Rétrocompatible : sans `market`, on apprend sur tout l'historique.
    """
    entries = recent_entries(store, tenant_id)
    if market:
        from app.data import markets
        entries = [e for e in entries if markets.asset_class(e.get("symbol", "")) == market]
    return compute_weight_multipliers(entries)


def close_trade(store, tenant_id: str, entry_id: str, outcome: str, pnl: float | None) -> dict | None:
    """Enregistre l'issue d'un trade (win/loss/breakeven) — alimente la boucle d'apprentissage."""
    if outcome not in {"win", "loss", "breakeven", "open"}:
        raise ValueError("issue invalide")
    return store.journal.update_outcome(tenant_id, entry_id, outcome=outcome, pnl=pnl)


async def auto_resolve(store, tenant_id: str) -> int:
    """Résout automatiquement les signaux 'open' dont le prix a touché le SL/TP -> win/loss.

    C'est le moteur de l'apprentissage continu : sans clic manuel, chaque signal directionnel finit
    par recevoir une issue réelle (rejeu du prix via data/replay), ce qui alimente les
    multiplicateurs de fiabilité par agent. Retourne le nombre d'entrées résolues sur ce passage."""
    from app.data import replay

    resolved = 0
    for e in recent_entries(store, tenant_id, limit=500):
        if e.get("outcome") != "open":
            continue
        sig_id = e.get("signal_id")
        if not sig_id:
            continue
        stored = store.signals.get(sig_id)
        if stored is None:
            continue
        p = stored.payload or {}
        direction = p.get("direction")
        entry, sl, tp = p.get("entry"), p.get("stop_loss"), p.get("take_profit_1")
        if direction in (None, "HOLD") or not entry or sl is None:
            continue
        try:
            verdict = await replay.replay_outcome(
                p.get("asset", e.get("symbol")), direction, entry, sl, tp, p.get("created_at"),
            )
        except Exception as exc:  # noqa: BLE001 — l'apprentissage ne doit jamais casser
            logger.warning("Auto-résolution %s échouée (%s)", sig_id, exc)
            continue
        outcome, exit_price = verdict["outcome"], verdict["exit_price"]
        # Un verdict indéterminé (données non réelles, marché fermé) ne doit RIEN apprendre :
        # entraîner les agents sur un résultat inventé est pire que ne rien apprendre du tout.
        if outcome in ("won", "lost"):
            mapped = "win" if outcome == "won" else "loss"
            pnl = (exit_price - entry) if str(direction).lower() == "buy" else (entry - exit_price)
            close_trade(store, tenant_id, e["id"], mapped, round(pnl, 4))
            resolved += 1
    return resolved


def stats(entries: list[dict]) -> dict:
    """KPI agrégés du journal (trades clôturés)."""
    closed = [e for e in entries if e.get("outcome") in {"win", "loss", "breakeven"}]
    wins = [e for e in closed if e.get("outcome") == "win"]
    losses = [e for e in closed if e.get("outcome") == "loss"]
    total_pnl = sum(float(e.get("pnl") or 0.0) for e in closed)
    n = len(closed)
    return {
        "total_entries": len(entries),
        "closed": n,
        # On ne compte que les vrais trades en attente (BUY/SELL) ; les anciens HOLD enregistrés
        # ne se résolvent jamais et ne doivent pas gonfler le compteur.
        "open": len([e for e in entries if e.get("outcome") == "open" and e.get("direction") != "HOLD"]),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
        "total_pnl": round(total_pnl, 2),
    }


async def explain_trade(entry: dict) -> str:
    """Explication IA d'un trade (post-mortem) + analyse des erreurs ; repli déterministe sans LLM."""
    from app.agents import llm

    scores = entry.get("agent_scores") or {}
    drivers = ", ".join(f"{k} ({v:+.2f})" for k, v in scores.items()) or "aucun score d'agent"
    outcome = entry.get("outcome", "open")
    base = (
        f"Trade {entry.get('direction')} sur {entry.get('symbol')} — issue : {outcome}, "
        f"P&L : {entry.get('pnl')}. Moteurs au moment du signal : {drivers}."
    )
    if not llm.available():
        verdict = {
            "win": "Les agents alignés sur la direction ont été confirmés par le marché.",
            "loss": "Le marché a invalidé le biais : revoir la pondération des agents divergents.",
            "breakeven": "Issue neutre : la conviction des agents était probablement faible.",
            "open": "Trade encore ouvert : pas d'analyse post-mortem disponible.",
        }.get(outcome, "")
        return f"{base}\n{verdict}\n(Analyse déterministe — configurez une clé LLM pour un post-mortem détaillé.)"
    try:
        prompt = (
            "Tu es un coach de trading. Analyse ce trade de façon concise (3-4 phrases) : "
            "qu'est-ce qui a fonctionné ou non, et quelle leçon en tirer pour les pondérations d'agents. "
            f"Ne donne pas de conseil financier.\n\nDONNÉES : {base}"
        )
        return (await llm.complete(prompt, role="reasoning", max_tokens=300)).strip()
    except llm.LLMUnavailable:
        return base
