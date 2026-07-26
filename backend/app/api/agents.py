"""Routes de supervision des Agents (Phase 2) — état réel de la couche LLM."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.agents import llm
from app.core.config import get_settings
from app.core.deps import current_user
from app.models.entities import User

router = APIRouter(prefix="/api/agents", tags=["agents"])

_AGENTS = [
    {"name": "playbook", "role": "reasoning",
     "desc": "STRATÉGIE du desk : Mensuel+Journalier → 4h → entrée 15 min · R/R ≥ 1:2 · ≥ 100 pips (droit de veto)"},
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

    llm_on = llm.available()
    s = get_settings()
    agents = [
        {**a, "weight": DEFAULT_WEIGHTS.get(a["name"]),
         "model": (llm.route(a["role"]) if llm_on else None) or "déterministe (fallback)"}
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
                "4 — 15 min : déclencheur d'entrée (seule unité de temps d'entrée)",
            ],
            "min_risk_reward": s.playbook_min_rr,
            "max_risk_reward": s.playbook_max_rr,
            "min_target_pips": s.playbook_min_target_pips,
            "entry_timeframe": s.playbook_entry_timeframe,
            "daily_trades": s.daily_top_trades_count,
        },
        "session": sessions_mod.session_context(),
    }
