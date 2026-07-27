"""Routes de supervision des Agents (Phase 2) — état réel de la couche LLM."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.agents import llm
from app.core.config import get_settings
from app.core.deps import current_user, store_dep
from app.models.entities import User
from app.repositories.store import AppStore

router = APIRouter(prefix="/api/agents", tags=["agents"])

_AGENTS = [
    {"name": "playbook", "role": "reasoning",
     "desc": "STRATÉGIE du desk : Mensuel+Journalier → 4 h → 1 h → entrée 15 min · objectif "
             "≥ 200 pips · R/R 1:2–1:3 · stop sécurisé à +2R (droit de veto)"},
    {"name": "technical", "role": "fast", "desc": "Indicateurs (RSI14, MA20/MA50, MACD, VWAP, divergences)"},
    {"name": "volume", "role": "fast", "desc": "Volume relatif, OBV, tendance VWAP"},
    {"name": "sentiment", "role": "fast", "desc": "NLP news + Fear & Greed"},
    {"name": "pattern", "role": "vision", "desc": "Figures chartistes"},
    {"name": "fundamental", "role": "reasoning", "desc": "Ratios financiers (actions)"},
    {"name": "macro", "role": "grounding", "desc": "Régime de marché"},
    {"name": "risk", "role": "deterministic", "desc": "Contrainte de capital (sans LLM)"},
    {"name": "master", "role": "master", "desc": "Arbitrage & pondération dynamique (soumis au veto du playbook)"},
]


@router.get("/status")
async def status(_user: User = Depends(current_user)) -> dict:
    """État des agents et de la couche LLM (modèle routé par rôle)."""
    from app.agents.master import DEFAULT_WEIGHTS
    from app.data import sessions as sessions_mod

    from app.agents import expertise

    llm_on = llm.available()
    s = get_settings()
    agents = [
        {**a, "weight": DEFAULT_WEIGHTS.get(a["name"]),
         "model": (llm.route(a["role"]) if llm_on else None) or "déterministe (fallback)",
         # Ce que l'entraînement de la nuit a mesuré pour cet agent, et la fiche qui en découle.
         "competence": expertise.competence(a["name"]),
         "expertise": expertise.memo(a["name"]) or None}
        for a in _AGENTS
    ]
    return {
        "status": "online",
        "llm_enabled": llm_on,
        "providers": {"anthropic": bool(s.anthropic_api_key), "google": bool(s.google_api_key)},
        "agents": agents,
        "strategy": {
            "name": "Playbook MTF",
            "enabled": s.playbook_enabled,
            "veto": s.playbook_veto,
            "steps": [
                "1 — Mensuel + Journalier : tendance de fond et supports/résistances majeurs",
                "2 — Journalier : RSI14, MA20, MA50, volume, tendance VWAP, divergences RSI/MACD, Fibonacci si correction",
                "3 — 4 h : mêmes facteurs, confirmation du biais",
                "4 — 1 h : dernière confirmation avant de chercher l'entrée",
                "5 — 15 min : déclencheur d'entrée (seule unité de temps d'entrée)",
            ],
            "min_risk_reward": s.playbook_min_rr,
            "max_risk_reward": s.playbook_max_rr,
            "min_target_pips": s.playbook_min_target_pips,
            "entry_timeframe": s.playbook_entry_timeframe,
            "confirm_timeframe": s.playbook_confirm_timeframe,
            "stop_timeframe": s.playbook_stop_timeframe,
            "target_timeframe": s.playbook_target_timeframe,
            "secure_at_r": s.playbook_secure_at_r,
            "secure_profit": s.playbook_secure_profit_enabled,
            "trade_only_when_open": s.playbook_trade_only_when_open,
            "daily_trades": s.daily_top_trades_count,
            "auto_entry": s.playbook_auto_entry_enabled,
            "auto_entry_mode": "paper",
        },
        "training": _training_summary(),
        "session": sessions_mod.session_context(),
    }


def _training_summary() -> dict:
    """Résumé du dernier entraînement quotidien (sans le détail complet, réservé à /training)."""
    from app.services import training_service

    snap = training_service.snapshot()
    if not snap:
        return {"trained": False,
                "note": "Entraînement pas encore passé — les agents utilisent leurs poids de base."}
    return {
        "trained": True,
        "date": snap.get("date"),
        "trades_replayed": snap.get("trades"),
        "symbols": snap.get("symbols_trained"),
        "overall": snap.get("overall"),
        "agent_multipliers": snap.get("agent_multipliers"),
        "duration_s": snap.get("duration_s"),
    }


@router.get("/training")
async def training(_user: User = Depends(current_user)) -> dict:
    """Détail de l'ENTRAÎNEMENT QUOTIDIEN des agents sur la stratégie du desk.

    Contient le walk-forward complet : réussite mesurée par symbole, par type de déclencheur et par
    fenêtre de session, justesse de chaque facteur, multiplicateurs de poids qui en découlent, et
    les fiches d'expertise du jour.
    """
    from app.services import training_service

    snap = training_service.snapshot()
    if not snap:
        return {"trained": False, "note": "Aucun entraînement disponible pour l'instant."}
    return {"trained": True, **snap}


@router.post("/training/run")
async def training_run(
    _user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Lance un entraînement immédiat (walk-forward complet + fiches). Opération longue."""
    from app.services import training_service

    return await training_service.run_training(store)
